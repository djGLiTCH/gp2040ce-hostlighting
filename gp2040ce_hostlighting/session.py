"""Run the specification's full host flow: handshake, claim, settings, events, cache, frames and shutdown.

A Session is optional. It is built on the device, LightsClaim, ConfigurationCache and publish_frame(), and
stages whole frames only. A host that stages only the pixels that change, which needs the takeover number,
keeps that itself.

Like the device, a Session belongs to one thread. poll() and show() never sleep, but each request they send
blocks for its round trip.

SPDX-FileCopyrightText: © 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import functools
import logging
import time
from dataclasses import dataclass, field, fields
from typing import Callable, Iterable, Optional

from gp2040ce_hostlighting.cache import ConfigurationCache
from gp2040ce_hostlighting.claim import ClaimChange, ClaimChangeKind, ClaimState, LightsClaim
from gp2040ce_hostlighting.frames import Frame, FrameResult, publish_frame
from gp2040ce_hostlighting.protocol import (BRIGHTNESS_SAVED, CMD_CLAIM, CMD_CLEAR, CMD_CONFIGURE, CMD_GET_PAGE,
                                            CMD_HELLO, CMD_RELEASE, CMD_SET_BRIGHTNESS, CMD_STAGE, CMD_SUBSCRIBE,
                                            EVENT_STATE_CHANGED, FLAG_COMMIT_AFTER, FLAG_GENERATION, FLAG_HOLDER,
                                            FLAG_KEEPALIVE, FLAG_NO_REPLY, PAGE_CONTROLS, PAGE_GROUPS, PAGE_LIGHTS,
                                            PAGE_STATE, PAGE_SUMMARY, STATUS_CLAIMED, HostLightingClaimedError,
                                            HostLightingDevice, HostLightingDisconnectedError, HostLightingError,
                                            HostLightingIncompatibleError, HostLightingRejectedError,
                                            HostLightingTimeoutError, HostLightingUnansweredError, find_board,
                                            latest_state, require_capabilities)
from gp2040ce_hostlighting.records import Controller, Hello, Requirements, Settings, StateRecord

__all__ = ['Session', 'SessionUpdate', 'REFRESH_RETRY_SECONDS', 'STATE_POLL_SECONDS', 'UNANSWERED_LIMIT']

logger = logging.getLogger(__name__)

# how often at most page 1 is read, with events or without, and a third of the timeout page 1 shows when shorter,
# so that a change of holder or of the state token with a lost event is learned within each keepalive timeout
STATE_POLL_SECONDS = 1.0
# how long a refresh that failed waits before it is tried again
REFRESH_RETRY_SECONDS = 1.0
# how many of the session's requests unanswered in a row take the board as lost
UNANSWERED_LIMIT = 3
_STATE_MASK = (1 << len(PAGE_GROUPS[PAGE_STATE])) - 1
_UNCHANGED = ClaimChange(kind=ClaimChangeKind.UNCHANGED)


def _bits(*flags: int) -> frozenset[int]:
    """Name request flags by their bit numbers, as HELLO lists them."""
    return frozenset(bit for flag in flags for bit in range(8) if flag >> bit & 1)


def _applied_by_board(settings: Settings) -> Settings:
    """Give the settings a board applies for these. A timeout of 0 is 2000 ms, and any other is clamped to 100-10000."""
    keepalive = 2000 if settings.keepalive_ms == 0 else min(max(settings.keepalive_ms, 100), 10000)
    return Settings(takeover=settings.takeover, keepalive_ms=keepalive, brightness_applied=settings.brightness_applied)


def _defaults(settings: Settings) -> bool:
    """Say whether settings are the defaults as a board applies them, a timeout of 0 among them."""
    return _applied_by_board(settings) == Settings()


def _later(earlier: ClaimChange, later: ClaimChange) -> ClaimChange:
    """Combine two changes found in turn: the later unless it is unchanged, asking for a restage if either does."""
    chosen = earlier if later.kind is ClaimChangeKind.UNCHANGED else later
    if chosen.restage or not (earlier.restage or later.restage):
        return chosen
    return ClaimChange(kind=chosen.kind, holder=chosen.holder, restage=True)


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionUpdate:
    """What poll() found.

    `claim` is what changed in the claim. `board_state` is the latest page 1 record, and `changed` names its
    fields (State, Controller and Settings fields by name) that differ from the record the last update carried,
    every field read for the first. `pages_read` names the
    configuration pages read again, page 8 among them when the Summary changed. `restage` is true when the host
    has to stage the whole frame again, after the claim was taken again or taken after waiting, the Controls or Lights
    page was read again, or the takeover ended while the claim was held.
    """

    claim: ClaimChange
    board_state: Optional[StateRecord]
    pages_read: frozenset[int] = field(default_factory=frozenset)
    restage: bool = False
    changed: frozenset[str] = field(default_factory=frozenset)


def _changed_fields(before: Optional[StateRecord], after: Optional[StateRecord]) -> frozenset[str]:
    """Name the page 1 fields that differ between two records, a group read in only one counting as changed."""
    names: set[str] = set()
    if after is None:
        return frozenset()
    for group in ('state', 'controller', 'settings'):
        new = getattr(after, group) if after.has(group) else None
        old = getattr(before, group) if before is not None and before.has(group) else None
        if new is None:
            continue
        names.update(item.name for item in fields(new)
                     if old is None or getattr(old, item.name) != getattr(new, item.name))
    return frozenset(names)


class Session:
    """One host's session with one board."""

    def __init__(self, device: HostLightingDevice, name: Optional[str], settings: Settings = Settings(),
                 exclusive: bool = False, force: bool = False, wait: bool = False,
                 wait_when_taken_over: bool = True, require: Optional[Requirements] = None,
                 pages: Optional[Iterable[int]] = None, masks: Optional[dict[int, int]] = None,
                 state_poll_seconds: Optional[float] = None,
                 reopen: Callable[[str], Optional[HostLightingDevice]] = find_board,
                 clock: Optional[Callable[[], float]] = None):
        """Describe the session. Nothing is sent until start().

        :param device: an opened device, which close() closes
        :param name: the host's name for its claim, up to 16 bytes, or None for a host that does not claim
        :param settings: the CONFIGURE settings. A host that does not claim sends settings other than the
            defaults only while the board is unclaimed, and again after a change of holder returns them
        :param exclusive: claim exclusively
        :param force: take the claim over from an application that holds it
        :param wait: wait for an application that holds the claim at the start to give it up
        :param wait_when_taken_over: wait for an application that takes the claim over later to give it up
        :param require: the capabilities the host itself uses, checked with the session's own
        :param pages: the configuration pages the cache keeps, by default every one the board lists
        :param masks: the field groups the cache reads per page, by default every group the library decodes
        :param state_poll_seconds: how often page 1 is read on a board without events, by default
            STATE_POLL_SECONDS
        :param reopen: finds the board again by its whole board ID, for reconnect()
        :param clock: seconds from a monotonic clock, by default time.monotonic
        """
        self.device = device
        self.name = name
        self.settings = settings
        self.exclusive, self.force, self.wait = exclusive, force, wait
        self.wait_when_taken_over = wait_when_taken_over
        self.require = require or Requirements()
        self.pages = None if pages is None else frozenset(pages)
        self.masks = masks
        self.state_poll_seconds = state_poll_seconds
        self.reopen = reopen
        self.clock = clock or (lambda: time.monotonic())
        self.hello: Optional[Hello] = None
        self.board_id: Optional[str] = None
        self.cache: Optional[ConfigurationCache] = None
        self.claim: Optional[LightsClaim] = None
        self.board_state: Optional[StateRecord] = None
        self._reported: Optional[StateRecord] = None        # the page 1 record the last update carried
        self._events = False            # the board raises STATE_CHANGED
        self._drove = False             # a frame was sent, so a host that does not claim releases at close()
        self._reset()

    def _reset(self) -> None:
        """Forget what was shown and what is due, as at a start."""
        self._shown: Optional[frozenset[tuple[int, int]]] = None   # the addresses of the frame last acknowledged
        # the next frame clears first. A start under a token that already holds the claim, or with another
        # host's takeover live, keeps the frame staged before it
        self._clear_next = True
        self._fresh = False             # page 1 was seen since the frame last acknowledged
        self._unanswered = 0            # the session's requests unanswered in a row
        self._pending = _UNCHANGED      # what show() found, for the next poll()
        self._state_due = 0.0
        self._keepalive_due = 0.0
        self._refresh = False           # the configuration pages are to be read again
        self._refresh_due = 0.0         # not before then, after a refresh that failed
        self._applied: Optional[Settings] = None       # the settings other than the defaults a host that does
        self._applied_generation: Optional[int] = None  # not claim last sent, and the generation page 1 showed
        self._sent: Optional[Settings] = None          # what the board applied to the CONFIGURE last sent
        # a CONFIGURE that went unanswered: the settings the board would have applied, and the generation then
        self._unconfirmed: Optional[tuple[Settings, int]] = None
        # a CONFIGURE that went unanswered: the settings and generation page 1 showed before it, sent again while
        # page 1 shows both
        self._unlanded: Optional[tuple[Settings, int]] = None
        # page 1 showed other settings in place of _applied under its generation, so they are no longer this host's
        self._replaced = False
        # settings CONFIGURE applied when the page 1 read after it went unanswered, until the next page 1 read
        self._landed_unseen: Optional[Settings] = None

    def __enter__(self) -> 'Session':
        """Start the session for a with block, which closes it. A start that fails is closed as close() does."""
        started = False
        try:
            self.start()
            started = True
        finally:
            if not started:
                self.close()
        return self

    def __exit__(self, *exception: object) -> None:
        """Close the session."""
        self.close()

    def requirements(self) -> Requirements:
        """Return what the session uses together with what the host requires."""
        if self.name is None:
            commands = {CMD_HELLO, CMD_GET_PAGE, CMD_STAGE, CMD_CLEAR, CMD_RELEASE, CMD_SET_BRIGHTNESS}
            if not _defaults(self.settings):
                commands.add(CMD_CONFIGURE)
            flags = _bits(FLAG_COMMIT_AFTER, FLAG_NO_REPLY, FLAG_KEEPALIVE)
        else:
            commands = {CMD_HELLO, CMD_GET_PAGE, CMD_CLAIM, CMD_CONFIGURE, CMD_STAGE, CMD_CLEAR}
            flags = _bits(FLAG_COMMIT_AFTER, FLAG_NO_REPLY, FLAG_KEEPALIVE, FLAG_GENERATION,
                          FLAG_HOLDER if self.exclusive else 0)
        own = Requirements(commands=frozenset(commands), request_flags=flags,
                           pages=frozenset({PAGE_STATE, PAGE_SUMMARY}))
        return own.union(self.require)

    def start(self) -> 'Session':
        """Run the start of the host flow.

        HELLO and the capability check, the configuration pages, the claim and CONFIGURE, SUBSCRIBE when the
        board raises events, then page 1 once. A host that does not claim sends CONFIGURE after page 1, when the
        board is unclaimed. A failure after the claim is taken gives it up before the error is raised.

        :return: the session, for a with block
        :raises HostLightingIncompatibleError: for a board of another major or without what is required
        :raises HostLightingClaimedError: when another application holds the claim, and it is neither to be
            taken over nor waited for, or takes it over during the start and wait_when_taken_over is false
        :raises HostLightingTimeoutError: when a request is not answered
        :raises HostLightingRejectedError: when the board refuses a request with a status other than 4
        :raises HostLightingDisconnectedError: when the board has left the bus
        """
        self._reset()
        # a claim held on this device before may have lapsed unseen, so nothing goes under its generation
        self.device.generation, self.device.holding_exclusive = 0, False
        self.hello = self.device.hello()
        require_capabilities(self.hello, self.requirements())
        self.board_id = self.hello.board_id
        self._events = CMD_SUBSCRIBE in self.hello.commands and EVENT_STATE_CHANGED in self.hello.events
        self.cache = ConfigurationCache(self.hello, self.masks)
        if self.pages is not None:
            self.cache.pages = self.pages & self.hello.pages
        self.cache.refresh(self.device)
        if self.name is not None:
            token = self.claim.token if self.claim else None    # the same token after a reconnect
            self.claim = LightsClaim(self.device, self.name, self.settings, token=token,
                                     wait_when_taken_over=self.wait_when_taken_over, clock=self.clock)
            self.claim.take(self.exclusive, self.force, self.wait)
        started = False
        try:
            # take() dropped the events older than the claim's reply. Those since are newer, so they count
            self._take_events()
            if self._events:
                self._start_step(lambda: self.device.subscribe([EVENT_STATE_CHANGED]))
            # a change before the subscription raises no event, so page 1 is read once after it
            self._start_step(self._read_state)
            if self.claim is not None and self.claim.state not in (ClaimState.HELD, ClaimState.WAITING):
                raise HostLightingClaimedError(self.claim.holder or Controller(holder_token=0, application_name='',
                                                                               exclusive=False, claim_generation=0))
            self._guarded(functools.partial(self._configure_unclaimed, fresh=True))
            started = True
        finally:
            if not started and self.claim is not None:
                self.claim.give_up()
        return self

    def _start_step(self, step: Callable[[], object]) -> None:
        """Run a request of start(), applying a status 4 refusal to the claim, then sending it once more.

        Another application can take the claim over during start(). The refusal leaves the requests without the
        claim generation, and SUBSCRIBE and page 1 reads are open even under an exclusive claim.
        """
        try:
            step()
        except HostLightingRejectedError as error:
            if error.status != STATUS_CLAIMED or self.claim is None:
                raise
            self._after_claim(self.claim.refused(error))
            step()

    def poll(self) -> SessionUpdate:
        """Keep the session: the claim, the events, page 1 and the configuration pages.

        The events are taken and their holders applied to the claim, the claim is renewed when due, page 1 is
        read when due, with events or without, so that a change with a lost event is learned, and the
        configuration pages are read again after a change of the state token. A host that does not claim keeps
        its frame with HELLO and KEEPALIVE while the board is unclaimed and its takeover live, and sends its
        settings again after a change of holder returned them, or after a CONFIGURE that did not land.

        A request that goes unanswered ends the poll, but for the page 1 read after a CONFIGURE, which is not
        counted. A renewal is tried again a third of the keepalive timeout
        later, and a page read at the next poll. Any answer resets the count. A refresh that fails is tried again
        after REFRESH_RETRY_SECONDS. The whole frame is to be staged again after the Controls or Lights change
        counter moved, whether or not the cache keeps that page.

        :raises HostLightingUnansweredError: after three of the session's requests in a row go unanswered
        :raises HostLightingDisconnectedError: when the board has left the bus
        :raises HostLightingRejectedError: when the board refuses a request with a status other than 4
        :raises HostLightingError: before start()
        """
        self._started()
        change = _UNCHANGED
        pages: frozenset[int] = frozenset()
        moved: frozenset[int] = frozenset()
        try:
            change = self._take_events()
            if self.claim is not None:
                change = _later(change, self._maintain())
            read = False
            if self.clock() >= self._state_due:
                change = _later(change, self._guarded(self._observe_state))
                read = True
            if self.name is None:
                change = _later(change, self._guarded(functools.partial(self._configure_unclaimed, fresh=read)))
                self._keep_frame()
            if self._refresh and self.clock() >= self._refresh_due:
                pages, moved, found = self._refresh_pages()
                change = _later(change, found)
        except HostLightingTimeoutError:
            self._missed()
        change = _later(self._take_pending(), change)
        restage = (change.restage or bool((pages | moved) & {PAGE_CONTROLS, PAGE_LIGHTS})
                   or self._takeover_ended())
        if restage:
            self._shown, self._clear_next = None, True
        changed, self._reported = _changed_fields(self._reported, self.board_state), self.board_state
        return SessionUpdate(claim=change, board_state=self.board_state, pages_read=pages, restage=restage,
                             changed=changed)

    def show(self, frame: Frame) -> Optional[FrameResult]:
        """Publish a whole frame while the claim is held, or for a host that does not claim, the board unclaimed.

        The first frame after each start clears first, and so does one that no longer names an address the last
        frame named. After a second missed
        acknowledgement HELLO is sent. A status 4 refusal goes to the claim, and the next poll() returns what it
        found. A renewal the refusal leads to that goes unanswered is counted, as poll() counts its own. The
        events that arrive before the frame's acknowledgement are older than the frame, and are applied before
        it counts as shown. An acknowledged frame or an answered HELLO resets the count.

        :return: what became of the frame, or None when it was not sent
        :raises HostLightingUnansweredError: when HELLO goes unanswered after a missed frame
        :raises HostLightingRejectedError: when the board refuses the frame with a status other than 4
        :raises HostLightingDisconnectedError: when the board has left the bus
        :raises HostLightingError: before start()
        :raises ValueError: for an LED run reaching past the board's addressable LED limit, before anything is
            sent
        """
        self._started()
        if self.cache is not None and self.cache.summary is not None:
            frame.check_led_limit(self.cache.summary.led_limit)
        # the events received so far are older than the frame, so they are applied before it
        self._pending = _later(self._pending, self._counted(self._take_events))
        if not self._may_show():
            return None
        addresses = frame.addresses
        clear = self._clear_next or (self._shown is not None and bool(self._shown - addresses))
        try:
            result = publish_frame(self.device, frame, clear_first=clear)
        except HostLightingRejectedError as error:
            # part of the frame may be staged, so the next clears first
            self._drove, self._shown, self._clear_next = True, None, True
            self._state_due = 0.0       # page 1 shows the holder now
            self._pending = _later(self._pending, self._counted(functools.partial(self._refused, error)))
            return FrameResult(acknowledged=False, resent=False, refused=error.holder)
        except HostLightingError:
            self._drove, self._shown, self._clear_next = True, None, True
            raise
        self._drove = True
        if result.acknowledged:
            self._unanswered = 0
            # the events read while waiting for the acknowledgement came before it
            older, self.device.events = self.device.events, []
            self._pending = _later(self._pending, self._counted(functools.partial(self._take_events, older)))
            self._shown, self._clear_next, self._fresh = addresses, False, False
            return result
        # not shown, so the next frame is staged whole, and the board is lost only when HELLO goes unanswered too
        self._shown, self._clear_next = None, True
        try:
            reply = self.device.request(CMD_HELLO)
        except HostLightingTimeoutError:
            raise HostLightingUnansweredError("the board answers neither a frame nor HELLO") from None
        self._unanswered = 0
        if reply[2] == STATUS_CLAIMED:
            refusal = self.device.rejected(CMD_HELLO, reply)
            self._pending = _later(self._pending, self._counted(functools.partial(self._refused, refusal)))
        return result

    def configure(self, settings: Settings) -> Optional[Settings]:
        """Change the CONFIGURE settings, which a start after reconnect() sends too.

        A host that claims sends them through its claim while it holds it, and the claim sends them each time it
        takes the claim again. One that does not claim takes the events and reads page 1, then sends them while
        the board is unclaimed: settings other than the defaults, or the defaults in place of settings it sent
        that page 1 still shows. When that page 1 read goes unanswered, settings other than the defaults go over
        the settings page 1 showed only after a change of holder, or as the resend of an earlier CONFIGURE that
        did not land, so a caller that needs them now should call configure() again. Before start() the settings
        are kept for it.

        :param settings: the new settings
        :return: the settings the board applied, or None when none were sent
        :raises HostLightingTimeoutError: when CONFIGURE, the page 1 read before it, or a claiming host's renewal
            before it is not answered
        :raises HostLightingRejectedError: when the board refuses CONFIGURE with a status other than 4
        :raises HostLightingDisconnectedError: when the board has left the bus
        """
        self.settings = settings
        if self.claim is not None:
            held = self.claim.state is ClaimState.HELD
            applied = self.claim.configure(settings)
            if held:
                # the events read while the renewal's reply was awaited are older than it
                older, self.device.events = self.device.events, []
                self._note_state(older)
            return applied
        if self.name is not None or self.hello is None:
            return None
        self._sent = None
        # page 1 as it stands now: the events received, then a read
        self._take_events()
        last = self.board_state
        try:
            self._guarded(self._observe_state)
        except HostLightingTimeoutError:
            if (last is not None and not _defaults(settings)
                    and (self._applied is None or self._replaced
                         or self._applied_generation != last.controller.claim_generation)):
                # as if CONFIGURE was sent under the generation last seen and then replaced, so nothing goes over
                # page 1 before a change of holder. Its own settings still shown stay its own, for close()
                generation = last.controller.claim_generation
                self._applied, self._applied_generation, self._replaced = _applied_by_board(settings), generation, True
            raise
        self._guarded(functools.partial(self._configure_unclaimed, True, True))
        return self._sent

    def reconnect(self) -> bool:
        """Find the board again after HostLightingDisconnectedError, and start again, waiting for the claim.

        The device is closed, the board is looked for by its board ID through `reopen`, and the session starts
        again with a new cache. Nothing is given up on the board that left. A board that is not found, cannot be
        looked for, or does not answer yet is looked for again at the next call.

        :return: whether the board is back
        :raises HostLightingIncompatibleError: when the board came back without what the session requires
        """
        if self.claim is not None:
            self.claim.let_lapse()      # the claim on the board that left ends at its keepalive timeout
        try:
            self.device.close()
        except (HostLightingError, OSError):
            pass                # the board has gone
        device = self._find_again()
        if device is None:
            return False
        self.device = device
        self.force, self.wait = False, True
        try:
            self.start()
        except HostLightingIncompatibleError:
            raise
        except (HostLightingError, OSError):
            logger.debug("starting again failed", exc_info=True)
            self._end_failed_start(device)
            return False
        return True

    def _end_failed_start(self, device: HostLightingDevice) -> None:
        """Close a device after a failed start, first putting back settings a host that does not claim sent in it."""
        try:
            if self.name is None and (self._applied is not None or self._unconfirmed is not None):
                self.close()
            else:
                device.close()
        except (HostLightingError, OSError):
            pass

    def _find_again(self) -> Optional[HostLightingDevice]:
        """Look for the board by its board ID through `reopen`, None when it is not found or cannot be looked for."""
        if self.board_id is None:
            return None
        try:
            return self.reopen(self.board_id)
        except OSError:
            logger.debug("looking for the board failed", exc_info=True)
            return None

    def close(self) -> None:
        """End the session, then close the device.

        A host that claims gives the claim up. One that does not, unless another application holds the claim,
        puts back the defaults in place of settings it sent that page 1 still shows, and when it sent a frame,
        sends RELEASE and SET_BRIGHTNESS 0xFF. A board that has gone restores itself at the keepalive timeout, and
        nothing is sent once the device is closed, as after a reconnect() that did not find the board.
        """
        if self.device.closed:
            return
        try:
            if self.name is not None:
                if self.claim is not None:
                    self.claim.give_up()
            elif self.hello is not None:
                self._read_state()
                state = self.board_state
                if state is not None and not state.controller.holder_token:
                    settings, self.settings = self.settings, Settings()
                    try:
                        self._configure_unclaimed(fresh=True)
                    finally:
                        self.settings = settings        # kept for a later start()
                    latest = self.board_state           # the read after CONFIGURE may show a holder since
                    if self._drove and not (latest is not None and latest.controller.holder_token):
                        self.device.release()
                        self.device.set_brightness(BRIGHTNESS_SAVED)
        except HostLightingError as error:
            logger.warning(     # noqa: G200 the cause is what the warning tells
                "could not end the session on the board, %s. The keepalive timeout restores the frame and the "
                "brightness, and a change of holder the settings", str(error))
            logger.debug("ending the session failed", exc_info=True)
        finally:
            self.device.close()

    def _may_show(self) -> bool:
        """Say whether a frame may be sent: the claim held, or for a host that does not claim, no holder.

        A status 4 reply the claim did not see can leave the device under another generation than the claim's,
        and the claim is then renewed first to learn the holder.
        """
        claim = self.claim
        if claim is not None:
            if claim.state is ClaimState.HELD and self.device.generation != claim.generation:
                self._pending = _later(self._pending, self._counted(lambda: self._after_claim(claim.renew())))
            return claim.state is ClaimState.HELD
        return self.board_state is None or not self.board_state.controller.holder_token

    def _take_pending(self) -> ClaimChange:
        """Return what show() found, once."""
        pending, self._pending = self._pending, _UNCHANGED
        return pending

    def _missed(self) -> None:
        """Count a request unanswered.

        :raises HostLightingUnansweredError: once three go unanswered in a row
        """
        self._unanswered += 1
        if self._unanswered >= UNANSWERED_LIMIT:
            raise HostLightingUnansweredError(f"no reply to {UNANSWERED_LIMIT} of the session's requests in a row")

    def _counted(self, step: Callable[[], ClaimChange]) -> ClaimChange:
        """Run a step of the housekeeping, counting a request it sends that goes unanswered."""
        try:
            return step()
        except HostLightingTimeoutError:
            self._missed()
            return _UNCHANGED

    def _after_claim(self, change: ClaimChange) -> ClaimChange:
        """Follow a claim call. When it sent a request and it was answered, reset the count.

        The events read while its reply was awaited are older than that reply, which the claim has applied. Their
        page 1 is kept, but their holders are not passed to the claim, which would take a lapse they show as
        new.
        """
        if change.holder is not None:
            self._unanswered = 0
            older, self.device.events = self.device.events, []
            self._note_state(older)
        return change

    def _note_state(self, events: list[bytes]) -> None:
        """Keep the newest page 1 that STATE_CHANGED events carry."""
        newest = latest_state(events)
        if newest is not None:
            self._state(newest.record, newest.token)

    def _take_events(self, events: Optional[list[bytes]] = None) -> ClaimChange:
        """Take the events received, keep the newest page 1 they carry, and apply their holders to the claim.

        :param events: the events to apply, by default every one received so far
        """
        events = self.device.take_events() if events is None else events
        self._note_state(events)
        if self.claim is not None and events:
            return self._after_claim(self.claim.observe(events))
        return _UNCHANGED

    def _maintain(self) -> ClaimChange:
        """Renew the claim when due."""
        return self._after_claim(self.claim.maintain()) if self.claim is not None else _UNCHANGED

    def _refused(self, error: HostLightingRejectedError) -> ClaimChange:
        """Send a status 4 refusal to the claim, and raise any other. Page 1, which shows the holder, is due now."""
        if error.status != STATUS_CLAIMED:
            raise error
        self._state_due = 0.0
        return self._after_claim(self.claim.refused(error)) if self.claim is not None else _UNCHANGED

    def _guarded(self, read: Callable[[], ClaimChange]) -> ClaimChange:
        """Make a read, sending a status 4 refusal to the claim."""
        try:
            return read()
        except HostLightingRejectedError as error:
            return self._refused(error)

    def _read_state(self) -> StateRecord:
        """Read page 1 and keep it.

        The next read is due within a third of the timeout page 1 shows, by default, or at the next poll when this
        one goes unanswered.
        """
        self._state_due = self.clock() + (STATE_POLL_SECONDS if self.state_poll_seconds is None
                                          else self.state_poll_seconds)
        try:
            read = self.device.read_record(PAGE_STATE, _STATE_MASK)
        except HostLightingTimeoutError:
            self._state_due = 0.0
            raise
        self._unanswered = 0
        record = read.records[0]
        self._state(record, read.token)
        if record.has('settings'):
            self._poll_within(record.settings)
        return record

    def _poll_within(self, settings: Settings) -> None:
        """Have page 1 read again within a third of the timeout settings apply, unless state_poll_seconds is set."""
        if self.state_poll_seconds is None and settings.keepalive_ms:
            self._state_due = min(self._state_due, self.clock() + settings.keepalive_ms / 3000)

    def _observe_state(self) -> ClaimChange:
        """Read page 1, and apply the holder it shows to the claim."""
        record = self._read_state()
        return self._after_claim(self.claim.observe([record])) if self.claim is not None else _UNCHANGED

    def _state(self, record: StateRecord, token: int) -> None:
        """Keep a page 1 record, and note a change of the state token."""
        self.board_state, self._fresh = record, True
        if self.cache is not None and token != self.cache.token:
            self._refresh = True

    def _takeover_ended(self) -> bool:
        """Say whether page 1, seen since the frame was acknowledged, shows the takeover ended.

        For a host that claims, only while it holds the claim. For one that does not, only once no other
        application holds it, since until then show() sends nothing.
        """
        state = self.board_state
        if state is None or self._shown is None or not self._fresh or state.state.takeover_live:
            return False
        if self.claim is not None:
            return self.claim.state is ClaimState.HELD
        return self.name is None and not state.controller.holder_token

    def _refresh_pages(self) -> tuple[frozenset[int], frozenset[int], ClaimChange]:
        """Read the configuration pages again after a change of the state token.

        :return: the pages read again, the configuration pages with a moved change counter, and what a status 4
            refusal changed in the claim
        """
        try:
            pages = self.cache.refresh(self.device) if self.cache is not None else frozenset()
        except (HostLightingDisconnectedError, HostLightingTimeoutError):
            raise
        except HostLightingRejectedError as error:
            if error.status == STATUS_CLAIMED:
                return frozenset(), frozenset(), self._refused(error)
            return self._refresh_failed(error)
        except HostLightingError as error:
            return self._refresh_failed(error)
        self._refresh, self._unanswered = False, 0
        return pages, self.cache.moved if self.cache is not None else frozenset(), _UNCHANGED

    def _refresh_failed(self, error: HostLightingError) -> tuple[frozenset[int], frozenset[int], ClaimChange]:
        """Warn of a refresh that failed, and leave the next try for REFRESH_RETRY_SECONDS."""
        self._refresh_due = self.clock() + REFRESH_RETRY_SECONDS
        logger.warning("could not read the board's configuration again, %s. Trying again in %g s",
                       str(error), REFRESH_RETRY_SECONDS)
        return frozenset(), frozenset(), _UNCHANGED

    def _configure_unclaimed(self, changed: bool = False, fresh: bool = False) -> ClaimChange:
        """For a host that does not claim, send its settings when the board is unclaimed and shows others.

        Settings other than the defaults are sent at the start, after configure() and again after a change of
        holder, which page 1 shows as another claim generation, held or not. The defaults are sent only in place
        of settings this host sent that page 1 still shows under the generation they were sent under, and has not
        seen replaced. Either way two hosts that do not claim leave each other's settings alone, unless they send
        the same values, which neither can tell from its own. CONFIGURE goes only after a page 1 read in the same
        call. A CONFIGURE that goes unanswered may have landed, which a later page 1 shows. Settings another host
        sent after it count as applied, and are left as they would be after a reply.

        :param changed: configure() changed the settings, so settings other than the defaults are sent again
        :param fresh: page 1 was read just before, in this call
        """
        state = self.board_state
        if self.name is not None or state is None:
            return _UNCHANGED
        if fresh:
            self._note_page_1(state)
        if state.controller.holder_token:
            return _UNCHANGED
        if not fresh:
            # its values under another generation: a page 1 read tells whether another host sent them
            shown_again = (self._applied is not None and state.settings == self._applied
                           and state.controller.claim_generation != self._applied_generation)
            if shown_again or self._settings_due(state, changed):
                self._read_state()
                return self._configure_unclaimed(changed, fresh=True)
            return _UNCHANGED
        if self._settings_due(state, changed):
            self._send_unclaimed(state.settings, state.controller.claim_generation)
        return _UNCHANGED

    def _note_page_1(self, state: StateRecord) -> None:
        """For a host that does not claim, note what a page 1 read just now shows of its settings."""
        landed, self._landed_unseen = self._landed_unseen, None
        generation = state.controller.claim_generation
        if state.controller.holder_token:
            if self._applied is not None and generation != self._applied_generation:
                # a change of holder put the defaults in place of its settings: sent again after the claim
                self._applied_generation, self._replaced = generation, True
            return
        if self._unconfirmed is not None:
            self._note_unconfirmed(state.settings, generation, self._unconfirmed)
        if landed is not None and state.settings == landed:
            # its CONFIGURE was answered and the read after it was not: they landed, under this generation
            self._applied, self._applied_generation, self._replaced = landed, generation, False
            return
        if self._applied is None:
            return
        if generation == self._applied_generation and state.settings != self._applied:
            self._replaced = True           # the same values shown later are another host's
        elif generation != self._applied_generation and state.settings == self._applied:
            # the same values shown after a change of holder, its own or another host's: in force, not its own
            self._applied_generation, self._replaced = generation, True
        elif generation != self._applied_generation and _defaults(self.settings):
            # a change of holder put the defaults in place of its settings, so none of its own is shown
            self._applied_generation, self._replaced = generation, True

    def _note_unconfirmed(self, settings: Settings, generation: int, unconfirmed: tuple[Settings, int]) -> None:
        """Note what page 1 shows of a CONFIGURE that went unanswered."""
        shown = (settings, generation)
        if shown == unconfirmed or (generation == unconfirmed[1] and shown != self._unlanded):
            # it landed, or another host's settings followed it, which stay as they would after a reply
            self._applied, self._applied_generation = unconfirmed
            self._unlanded, self._replaced = None, False
        elif shown != self._unlanded:
            self._unlanded = None           # a change of holder followed, which sends the settings again anyway
        self._unconfirmed = None

    def _settings_due(self, state: StateRecord, changed: bool) -> bool:
        """Say whether a host that does not claim sends its settings over what page 1 shows."""
        generation = state.controller.claim_generation
        if _defaults(self.settings):        # only in place of its own settings, still shown
            return (self._applied is not None and not self._replaced and state.settings == self._applied
                    and generation == self._applied_generation)
        return (changed or self._applied is None or self._unlanded == (state.settings, generation)
                or (state.settings != self._applied and generation != self._applied_generation))

    def _send_unclaimed(self, before: Settings, generation: int) -> None:
        """Send the settings of a host that does not claim, and note what the board applied under which generation.

        :param before: the settings page 1 showed just before
        :param generation: the claim generation page 1 showed just before
        """
        try:
            self._sent = self.device.configure(self.settings.takeover, self.settings.keepalive_ms,
                                               self.settings.brightness_applied)
        except HostLightingTimeoutError:
            if not _defaults(self.settings):
                self._unconfirmed = (_applied_by_board(self.settings), generation)
                self._unlanded = (before, generation)   # sent again while page 1 shows what it showed before
            self._state_due = 0.0           # page 1 shows whether it landed
            raise
        self._unanswered, self._unlanded, self._replaced = 0, None, False
        self._applied = None if _defaults(self.settings) else self._sent
        self._applied_generation = generation
        # page 1 read now shows the generation the settings were applied under. The read before CONFIGURE can be
        # older than that, when the holder changed while its reply was on the way. Without an answer, the generation
        # read before stands
        try:
            after = self._read_state()
        except HostLightingTimeoutError:
            self._landed_unseen = self._applied
            return
        if after.has('settings') and after.settings == self._sent:
            self._applied_generation = after.controller.claim_generation

    def _started(self) -> None:
        """Refuse a call that needs start() first.

        :raises HostLightingError: before start()
        """
        if self.hello is None:
            raise HostLightingError("the session has not started. Call start() first")

    def _keep_frame(self) -> None:
        """For a host that does not claim, keep a frame shown with HELLO and KEEPALIVE at a third of the timeout.

        Nothing is sent while another application holds the claim, or once page 1 shows the takeover ended,
        since KEEPALIVE would keep another application's takeover.
        """
        state = self.board_state
        if self._shown is None or self.clock() < self._keepalive_due:
            return
        if state is not None and (state.controller.holder_token or (self._fresh and not state.state.takeover_live)):
            return
        timeout = self.board_state.settings.keepalive_ms if self.board_state is not None else 2000
        self._keepalive_due = self.clock() + timeout / 3000
        self.device.keepalive()
