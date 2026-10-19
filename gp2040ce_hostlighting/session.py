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
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from gp2040ce_hostlighting.cache import ConfigurationCache
from gp2040ce_hostlighting.claim import ClaimChange, ClaimChangeKind, ClaimState, LightsClaim
from gp2040ce_hostlighting.frames import Frame, FrameResult, publish_frame
from gp2040ce_hostlighting.protocol import (BRIGHTNESS_SAVED, CMD_CLAIM, CMD_CLEAR, CMD_CONFIGURE, CMD_GET_PAGE,
                                            CMD_HELLO, CMD_RELEASE, CMD_SET_BRIGHTNESS, CMD_STAGE, CMD_SUBSCRIBE,
                                            EVENT_STATE_CHANGED, FLAG_COMMIT_AFTER, FLAG_GENERATION, FLAG_HOLDER,
                                            FLAG_KEEPALIVE, FLAG_NO_REPLY, PAGE_CONTROLS, PAGE_GROUPS, PAGE_LIGHTS,
                                            PAGE_STATE, PAGE_SUMMARY, STATUS_CLAIMED, HostLightingDevice,
                                            HostLightingDisconnectedError, HostLightingError,
                                            HostLightingIncompatibleError, HostLightingRejectedError,
                                            HostLightingTimeoutError, HostLightingUnansweredError, find_board,
                                            latest_state, require_capabilities)
from gp2040ce_hostlighting.records import Hello, Requirements, Settings, StateRecord

__all__ = ['Session', 'SessionUpdate', 'REFRESH_RETRY_SECONDS', 'STATE_POLL_SECONDS', 'UNANSWERED_LIMIT']

logger = logging.getLogger(__name__)

# how often page 1 is read on a board without events, so that a change is learned within the default keepalive
STATE_POLL_SECONDS = 1.0
# how long a refresh that failed waits before it is tried again
REFRESH_RETRY_SECONDS = 1.0
# how many of the session's requests unanswered in a row take the board as lost
UNANSWERED_LIMIT = 3
_STATE_MASK = (1 << len(PAGE_GROUPS[PAGE_STATE])) - 1
_UNCHANGED = ClaimChange(kind=ClaimChangeKind.UNCHANGED)


def _bits(*flags: int) -> frozenset:
    """Name request flags by their bit numbers, as HELLO lists them."""
    return frozenset(bit for flag in flags for bit in range(8) if flag >> bit & 1)


def _later(earlier: ClaimChange, later: ClaimChange) -> ClaimChange:
    """Combine two changes found in turn: the later unless it is unchanged, asking for a restage if either does."""
    chosen = earlier if later.kind is ClaimChangeKind.UNCHANGED else later
    if chosen.restage or not (earlier.restage or later.restage):
        return chosen
    return ClaimChange(kind=chosen.kind, holder=chosen.holder, restage=True)


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionUpdate:
    """What poll() found.

    `claim` is what changed in the claim. `board_state` is the latest page 1 record. `pages_read` names the
    configuration pages read again, page 8 among them when the Summary changed. `restage` is true when the host
    has to stage the whole frame again: the claim was taken again or taken after waiting, the Controls or Lights
    page was read again, or the takeover ended while the claim was held.
    """

    claim: ClaimChange
    board_state: Optional[StateRecord]
    pages_read: frozenset = field(default_factory=frozenset)
    restage: bool = False


class Session:
    """One host's session with one board."""

    def __init__(self, device: HostLightingDevice, name: Optional[str], settings: Settings = Settings(),
                 exclusive: bool = False, force: bool = False, wait: bool = False,
                 wait_when_taken_over: bool = True, require: Optional[Requirements] = None,
                 pages: Optional[Iterable[int]] = None, masks: Optional[dict] = None,
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
        self._events = False            # the board raises STATE_CHANGED
        self._reset()

    def _reset(self) -> None:
        """Forget what was shown and what is due, as at a start."""
        self._shown: Optional[frozenset] = None     # the addresses of the frame last acknowledged
        self._clear_next = False        # the next frame clears first
        self._fresh = False             # page 1 was seen since the frame last acknowledged
        self._unanswered = 0            # the session's requests unanswered in a row
        self._pending = _UNCHANGED      # what show() found, for the next poll()
        self._state_due = 0.0
        self._keepalive_due = 0.0
        self._refresh = False           # the configuration pages are to be read again
        self._refresh_due = 0.0         # not before then, after a refresh that failed
        self._applied: Optional[Settings] = None       # the settings a host that does not claim last sent

    def __enter__(self) -> 'Session':
        """Start the session for a with block, which closes it."""
        return self.start()

    def __exit__(self, *exception) -> None:
        """Close the session."""
        self.close()

    def requirements(self) -> Requirements:
        """Return what the session uses together with what the host requires."""
        if self.name is None:
            commands = {CMD_HELLO, CMD_GET_PAGE, CMD_STAGE, CMD_CLEAR, CMD_RELEASE, CMD_SET_BRIGHTNESS}
            if self.settings != Settings():
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
            taken over nor waited for
        """
        self._reset()
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
            self.device.take_events()   # sent before the claim, so older than it
            if self._events:
                self.device.subscribe([EVENT_STATE_CHANGED])
            # a change before the subscription raises no event, so page 1 is read once after it
            self._read_state()
            self._configure_unclaimed()
            started = True
        finally:
            if not started and self.claim is not None:
                self.claim.give_up()
        return self

    def poll(self) -> SessionUpdate:
        """Keep the session: the claim, the events, page 1 and the configuration pages.

        The events are taken and their holders applied to the claim, the claim is renewed when due, page 1 is
        read when the board raises no events, and the configuration pages are read again after a change of the
        state token. A host that does not claim keeps its frame with HELLO and KEEPALIVE, and sends its settings
        again when page 1 shows the board unclaimed with others.

        A request that goes unanswered ends the poll, and is asked again at the next. Any answer resets the
        count. A refresh that fails is tried again after REFRESH_RETRY_SECONDS.

        :raises HostLightingUnansweredError: after three of the session's requests in a row go unanswered
        :raises HostLightingDisconnectedError: when the board has left the bus
        """
        change = _UNCHANGED
        pages: frozenset = frozenset()
        try:
            change = self._take_events()
            if self.claim is not None:
                change = _later(change, self._maintain())
            if not self._events and self.clock() >= self._state_due:
                change = _later(change, self._guarded(self._observe_state))
            if self.name is None:
                change = _later(change, self._guarded(self._configure_unclaimed))
                self._keep_frame()
            if self._refresh and self.clock() >= self._refresh_due:
                pages, found = self._refresh_pages()
                change = _later(change, found)
        except HostLightingTimeoutError:
            self._missed()
        change = _later(self._take_pending(), change)
        restage = change.restage or bool(pages & {PAGE_CONTROLS, PAGE_LIGHTS}) or self._takeover_ended()
        if restage:
            self._shown, self._clear_next = None, True
        return SessionUpdate(claim=change, board_state=self.board_state, pages_read=pages, restage=restage)

    def show(self, frame: Frame) -> Optional[FrameResult]:
        """Publish a whole frame while the claim is held, or for a host that does not claim, the board unclaimed.

        The frame clears first when it no longer names an address the last frame named. After a second missed
        acknowledgement HELLO is sent. A status 4 refusal goes to the claim, and the next poll() returns what it
        found. A renewal the refusal leads to that goes unanswered is counted, as poll() counts its own.

        :return: what became of the frame, or None when it was not sent
        :raises HostLightingUnansweredError: when HELLO goes unanswered after a missed frame
        """
        # the events received so far are older than the frame, so they are applied before it
        self._pending = _later(self._pending, self._counted(self._take_events))
        if not self._may_show():
            return None
        clear = self._clear_next or (self._shown is not None and bool(self._shown - frame.addresses))
        try:
            result = publish_frame(self.device, frame, clear_first=clear)
        except HostLightingRejectedError as error:
            self._pending = _later(self._pending, self._counted(functools.partial(self._refused, error)))
            self._state_due = 0.0       # page 1 shows the holder now
            self._shown, self._clear_next = None, True
            return FrameResult(acknowledged=False, resent=False, refused=error.holder)
        if result.acknowledged:
            self._shown, self._clear_next, self._fresh = frame.addresses, False, False
            return result
        # not shown: the next frame is staged whole, and the board is lost only when HELLO goes unanswered too
        self._shown, self._clear_next = None, True
        try:
            self.device.request(CMD_HELLO)
        except HostLightingTimeoutError:
            raise HostLightingUnansweredError("the board answers neither a frame nor HELLO") from None
        return result

    def reconnect(self) -> bool:
        """Find the board again after HostLightingDisconnectedError, and start again, waiting for the claim.

        The device is closed, the board is looked for by its board ID through `reopen`, and the session starts
        again with a new cache. Nothing is given up on the board that left. A board that is not found, cannot be
        looked for, or does not answer yet is looked for again at the next call.

        :return: whether the board is back
        :raises HostLightingIncompatibleError: when the board came back without what the session requires
        """
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
            try:
                device.close()
            except (HostLightingError, OSError):
                pass
            return False
        return True

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

        A host that claims gives the claim up. One that does not sends RELEASE and SET_BRIGHTNESS 0xFF, unless
        another application holds the claim. A board that has gone restores itself at the keepalive timeout.
        """
        try:
            if self.name is not None:
                if self.claim is not None:
                    self.claim.give_up()
            elif self.hello is not None:
                state = self.device.read_record(PAGE_STATE, _STATE_MASK).records[0]
                if not state.controller.holder_token:
                    self.device.release()
                    self.device.set_brightness(BRIGHTNESS_SAVED)
        except HostLightingError as error:
            logger.warning(     # noqa: G200 the cause is what the warning tells
                "could not end the session on the board, %s. The keepalive timeout restores it", str(error))
            logger.debug("ending the session failed", exc_info=True)
        finally:
            self.device.close()

    def _may_show(self) -> bool:
        """Say whether a frame may be sent: the claim held, or for a host that does not claim, no holder."""
        if self.claim is not None:
            return self.claim.state is ClaimState.HELD
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

    def _answered(self, change: ClaimChange) -> ClaimChange:
        """Reset the count when the claim sent a request and it was answered."""
        if change.holder is not None:
            self._unanswered = 0
        return change

    def _take_events(self) -> ClaimChange:
        """Take the events received, keep the newest page 1 they carry, and apply their holders to the claim."""
        events = self.device.take_events()
        newest = latest_state(events)
        if newest is not None:
            self._state(newest.record, newest.token)
        if self.claim is not None and events:
            return self._answered(self.claim.observe(events))
        return _UNCHANGED

    def _maintain(self) -> ClaimChange:
        """Renew the claim when due."""
        return self._answered(self.claim.maintain()) if self.claim is not None else _UNCHANGED

    def _refused(self, error: HostLightingRejectedError) -> ClaimChange:
        """Send a status 4 refusal to the claim, and raise any other."""
        if error.status != STATUS_CLAIMED:
            raise error
        return self.claim.refused(error) if self.claim is not None else _UNCHANGED

    def _guarded(self, read: Callable[[], ClaimChange]) -> ClaimChange:
        """Make a read, sending a status 4 refusal to the claim."""
        try:
            return read()
        except HostLightingRejectedError as error:
            return self._refused(error)

    def _read_state(self) -> StateRecord:
        """Read page 1 and keep it."""
        self._state_due = self.clock() + (STATE_POLL_SECONDS if self.state_poll_seconds is None
                                          else self.state_poll_seconds)
        read = self.device.read_record(PAGE_STATE, _STATE_MASK)
        self._unanswered = 0
        self._state(read.records[0], read.token)
        return read.records[0]

    def _observe_state(self) -> ClaimChange:
        """Read page 1, and apply the holder it shows to the claim."""
        record = self._read_state()
        return self.claim.observe([record]) if self.claim is not None else _UNCHANGED

    def _state(self, record: StateRecord, token: int) -> None:
        """Keep a page 1 record, and note a change of the state token."""
        self.board_state, self._fresh = record, True
        if self.cache is not None and token != self.cache.token:
            self._refresh = True

    def _takeover_ended(self) -> bool:
        """Say whether page 1, seen since the frame was acknowledged, shows the takeover ended under the claim."""
        held = self.claim is not None and self.claim.state is ClaimState.HELD
        return (held and self._shown is not None and self._fresh and self.board_state is not None
                and not self.board_state.state.takeover_live)

    def _refresh_pages(self) -> tuple:
        """Read the configuration pages again after a change of the state token.

        :return: the pages read again, and what a status 4 refusal changed in the claim
        """
        try:
            pages = self.cache.refresh(self.device) if self.cache is not None else frozenset()
        except (HostLightingDisconnectedError, HostLightingTimeoutError):
            raise
        except HostLightingRejectedError as error:
            if error.status == STATUS_CLAIMED:
                return frozenset(), self._refused(error)
            return self._refresh_failed(error)
        except HostLightingError as error:
            return self._refresh_failed(error)
        self._refresh, self._unanswered = False, 0
        return pages, _UNCHANGED

    def _refresh_failed(self, error: HostLightingError) -> tuple:
        """Warn of a refresh that failed, and leave the next try for REFRESH_RETRY_SECONDS."""
        self._refresh_due = self.clock() + REFRESH_RETRY_SECONDS
        logger.warning("could not read the board's configuration again, %s. Trying again in %g s",
                       str(error), REFRESH_RETRY_SECONDS)
        return frozenset(), _UNCHANGED

    def _configure_unclaimed(self) -> ClaimChange:
        """For a host that does not claim, send its settings when the board is unclaimed and shows others.

        Settings equal to the defaults are never sent, so that two hosts that do not claim leave each other's
        settings alone.
        """
        state = self.board_state
        if self.settings == Settings() or state is None or state.controller.holder_token:
            return _UNCHANGED
        if self._applied is None or state.settings != self._applied:
            self._applied = self.device.configure(self.settings.takeover, self.settings.keepalive_ms,
                                                  self.settings.brightness_applied)
        return _UNCHANGED

    def _keep_frame(self) -> None:
        """For a host that does not claim, keep a frame shown with HELLO and KEEPALIVE at a third of the timeout."""
        if self._shown is None or self.clock() < self._keepalive_due:
            return
        timeout = self.board_state.settings.keepalive_ms if self.board_state is not None else 2000
        self._keepalive_due = self.clock() + timeout / 3000
        self.device.keepalive()
