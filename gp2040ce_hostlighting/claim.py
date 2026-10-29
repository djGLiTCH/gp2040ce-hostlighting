# gp2040ce-hostlighting (GP2040-CE Host Lighting library and tools)
# Filename: claim.py
# Folder: gp2040ce_hostlighting
# https://github.com/djGLiTCH/gp2040ce-hostlighting
# SPDX-FileCopyrightText: (C) 2026 Jacob Simpson (DJ GLiTCH)
# SPDX-License-Identifier: GPL-3.0-or-later
# See LICENSES/GPL-3.0-or-later.txt at the repository root.

"""Keep a claim on a board's lights: take it, renew it, follow what changes it and give it up.

Applications sharing a board coordinate through CLAIM. The claim is a lease that only the holder's own CLAIM
renews, so LightsClaim renews it at a third of the keepalive timeout the board applied. When the claim lapses or
another application takes it over, the takeover ends, the staged frame is cleared and the CONFIGURE settings
return to their defaults. LightsClaim sends its settings again whenever it holds the claim again, and its calls
return a ClaimChange that tells the host to stage the whole frame again. Each change is logged at INFO.

A LightsClaim belongs to one thread, as the device it uses does. No call sleeps but keep(), and each request
blocks for its round trip.
"""
import enum
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator, Optional, Union

from gp2040ce_hostlighting.protocol import (CLAIM_GIVE_UP, CLAIM_TAKE, CLAIM_TAKE_OVER, EVENT_STATE_CHANGED,
                                            FLAG_KEEPALIVE, STATUS_CLAIMED, HostLightingClaimedError,
                                            HostLightingDevice, HostLightingError, HostLightingRejectedError,
                                            HostLightingTimeoutError, _check_claim_name, decode_state_changed, is_event)
from gp2040ce_hostlighting.records import Controller, Settings, StateChanged, StateRecord

__all__ = ['ClaimChange', 'ClaimChangeKind', 'ClaimState', 'LightsClaim']

logger = logging.getLogger(__name__)


class ClaimState(enum.Enum):
    """Where a LightsClaim stands."""

    HELD = 'held'               # this application holds the claim
    WAITING = 'waiting'         # another application holds it, and each renewal asks for it
    LOST = 'lost'               # another application took it over, and wait_when_taken_over is off
    ENDED = 'ended'             # not taken yet, given up or let lapse


class ClaimChangeKind(enum.Enum):
    """What a call found changed in the claim."""

    UNCHANGED = 'unchanged'
    RETAKEN = 'retaken'         # held again after a lapse or another holder, the settings sent again
    LOST = 'lost'               # another application holds the claim now
    TAKEN = 'taken'             # taken after waiting, the settings sent


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaimChange:
    """What a LightsClaim call found.

    `holder` is the holder the board showed, None when nothing was sent. `restage` is true when the host has to
    stage the whole frame again, since the claim it now holds started with nothing staged.
    """

    kind: ClaimChangeKind
    holder: Optional[Controller] = None
    restage: bool = False


_UNCHANGED = ClaimChange(kind=ClaimChangeKind.UNCHANGED)


def _timeout(keepalive_ms: int) -> float:
    """Return in seconds the keepalive timeout CONFIGURE applies for one asked for: 0 is 2000 ms, clamped."""
    return min(max(keepalive_ms or 2000, 100), 10000) / 1000


def _monotonic() -> float:
    """Read time.monotonic as this module sees it when called."""
    return time.monotonic()


class LightsClaim:
    """A claim on a board's lights, and the CONFIGURE settings that go with it."""

    def __init__(self, device: HostLightingDevice, name: str, settings: Settings = Settings(),
                 token: Optional[int] = None, keepalive: bool = True, wait_when_taken_over: bool = True,
                 clock: Optional[Callable[[], float]] = None):
        """Describe the claim. Nothing is sent until take().

        :param device: an opened HostLightingDevice, which this claim alone claims through
        :param name: the application's name, up to 16 bytes, shown in page 1 while it holds the claim
        :param settings: the CONFIGURE settings, sent each time the claim is taken
        :param token: the claim token, 1 to 0xFFFFFFFF, by default a random one
        :param keepalive: renewals carry KEEPALIVE, which keeps a takeover and an unsaved brightness step live.
            The specification puts it on renewals while a frame or an unsaved step is shown
        :param wait_when_taken_over: when another application takes the claim over after this one held it, leave
            the state WAITING, so that each renewal asks for the claim and takes it once the board is unclaimed.
            Without it the state is LOST and nothing more is asked
        :param clock: seconds from a monotonic clock, by default time.monotonic
        :raises ValueError: for a name over 16 bytes or a token out of range
        """
        _check_claim_name(name.encode())
        if token is not None and not 0 < token <= 0xFFFFFFFF:
            raise ValueError(f"a claim token is 1 to 0xFFFFFFFF, not {token}")
        self.device = device
        self.name = name
        self.settings = settings
        self.token = (secrets.randbits(32) or 1) if token is None else token
        self.keepalive = keepalive
        self.clock = clock or _monotonic
        self.exclusive = False          # each renewal claims exclusively when set
        self.wait_when_taken_over = wait_when_taken_over  # a claim taken over after it was held is waited for
        self.state = ClaimState.ENDED
        self.holder: Optional[Controller] = None       # the holder the board last showed
        self.applied: Optional[Settings] = None        # the settings the board last applied for this claim
        self._generation = 0            # the claim generation this application last held the claim under
        self._configured = False        # the settings were applied since the claim was last taken
        self._interval = _timeout(settings.keepalive_ms) / 3
        self._due = 0.0
        self._pending: Optional[ClaimChange] = None    # a change configure() found, for the next call to return
        self._unsure = False            # a renewal went unanswered since the last answer, so it may hold the claim

    def __enter__(self) -> 'LightsClaim':
        """Use the claim in a with block, which gives it up."""
        return self

    def __exit__(self, *exception: object) -> None:
        """Give the claim up when held."""
        self.give_up()

    def take(self, exclusive: bool = False, force: bool = False, wait: bool = False) -> 'LightsClaim':
        """Claim the lights, then send the settings.

        A failure after the claim is taken gives it up before the error is raised, as does a CLAIM without a reply,
        which may have taken it. The events received until the claim's reply are older than it, so they are
        dropped. Under a token that already holds the claim, the claim and the staged frame are kept, so a host
        that starts again under a fixed token clears first.

        :param exclusive: claim exclusively: until the claim ends, the board refuses other applications'
            changes
        :param force: take the claim over from an application that holds it
        :param wait: where another application holds the claim now, or takes it over before the settings are
            applied, leave the state WAITING, so that each renewal asks for the claim and takes it once the board
            is unclaimed. A claim taken over later follows wait_when_taken_over
        :return: the claim, for a with block
        :raises HostLightingClaimedError: when another application holds the claim, and it was neither to be
            taken over nor waited for
        :raises HostLightingTimeoutError: when CLAIM or CONFIGURE is not answered
        :raises HostLightingRejectedError: when the board refuses a request with a status other than 4
        :raises HostLightingDisconnectedError: when the board has left the bus
        """
        self.exclusive, self._unsure = exclusive, False
        try:
            holder = self.device.claim(CLAIM_TAKE_OVER if force else CLAIM_TAKE, self.token, self.name.encode(),
                                       exclusive=exclusive)
        except HostLightingTimeoutError:
            self.state, self._unsure = ClaimState.WAITING, True
            self.give_up()                  # a CLAIM with its reply lost may hold the claim
            raise
        self.device.events.clear()      # received by the claim's reply, so older than it
        self.holder = holder
        self._schedule()
        if holder.held_by(self.token):
            self.state, self._generation, self._configured = ClaimState.HELD, holder.claim_generation, False
            try:
                self._send_settings()
            except HostLightingError:
                if self.state is ClaimState.WAITING:
                    self.give_up()              # a renewal with its reply lost may hold the claim
                raise
            finally:
                if not self._configured and self.state is ClaimState.HELD:
                    self.give_up()
            if self.state is ClaimState.HELD:
                return self
        holder = self.holder or holder
        if not wait:
            self.state = ClaimState.ENDED
            raise HostLightingClaimedError(holder)
        self.state = ClaimState.WAITING
        logger.info("'%s' holds the lights. Waiting for it to give them up", holder.application_name)
        return self

    def configure(self, settings: Settings) -> Optional[Settings]:
        """Change the settings, which are sent each time the claim is taken.

        While held, the claim is renewed first, since a shorter timeout applies at once to the lease, then
        CONFIGURE is sent and the renewals are paced by the timeout applied. A change that renewal found is
        returned by the next call that returns one. When the renewal goes unanswered, the next renewal is due at
        once and sends the settings.

        :param settings: the new settings
        :return: the settings the board applied, or None when the claim is not held or they were refused
        """
        self.settings = settings
        if self.state is not ClaimState.HELD:
            self._interval = _timeout(settings.keepalive_ms) / 3
            return None
        try:
            change = self._renew()
        except HostLightingTimeoutError:
            self._configured, self._due = False, self.clock()     # the next renewal sends the settings
            raise
        if change.kind is not ClaimChangeKind.RETAKEN and self.state is ClaimState.HELD:
            change = self._send_settings() or change
        self._pending = change if change.kind is not ClaimChangeKind.UNCHANGED else self._pending
        return self.applied if self.state is ClaimState.HELD and self._configured else None

    @property
    def generation(self) -> int:
        """The claim generation this application last held the claim under, 0 before it first held it."""
        return self._generation

    @property
    def seconds_to_renewal(self) -> Optional[float]:
        """Seconds until the next renewal is due, or None while the claim is neither held nor waited for."""
        if self.state not in (ClaimState.HELD, ClaimState.WAITING):
            return None
        return max(0.0, self._due - self.clock())

    def maintain(self) -> ClaimChange:
        """Renew the claim when a renewal is due, and otherwise send nothing.

        :raises HostLightingTimeoutError: when the renewal is not answered
        """
        if self.state in (ClaimState.HELD, ClaimState.WAITING) and self.clock() >= self._due:
            return self.renew()
        return self._result(_UNCHANGED)

    def renew(self) -> ClaimChange:
        """Renew the claim now, or ask for it while waiting.

        One CLAIM answers every case. Held and unchanged, it renews the lease, and with KEEPALIVE the takeover.
        Lapsed, it takes the claim again under a new generation, and the settings are sent again. Taken over,
        it shows the holder. While waiting, it takes the claim once the board is unclaimed.

        :raises HostLightingTimeoutError: when the renewal is not answered
        """
        if self.state not in (ClaimState.HELD, ClaimState.WAITING):
            return self._result(_UNCHANGED)
        return self._result(self._renew())

    def observe(self, events: Iterable[Union[bytes, StateChanged, StateRecord]]) -> ClaimChange:
        """Apply the holders that STATE_CHANGED events show, every event and not only the newest.

        Held, an event that shows another holder, none, or another generation means the claim changed holder
        since, and the settings and the staged frame went with it. The claim is renewed at once, which sends
        the settings again or finds it lost. The generation has seven values, so a claim that changed holder
        and came back can show the generation held here, which is why every event counts. Waiting, an event
        that shows the board unclaimed has the claim asked for at once. Events received before take() are not
        passed.

        :param events: oldest first, event reports as take_events() gives them, decoded StateChanged events,
            or page 1 records as a host without events reads them. Other events are passed over
        """
        if self.state not in (ClaimState.HELD, ClaimState.WAITING):
            return self._result(_UNCHANGED)
        changed, unclaimed = False, False
        for controller in _controllers(events):
            changed = changed or not controller.held_by(self.token) or controller.claim_generation != self._generation
            unclaimed = not controller.holder_token
        if self.state is ClaimState.HELD and changed:
            return self._result(self._renew(reconfigure=True))
        if self.state is ClaimState.WAITING and unclaimed:
            return self._result(self._renew())
        return self._result(_UNCHANGED)

    def refused(self, error: HostLightingRejectedError) -> ClaimChange:
        """Apply the rest of the status 4 table to a refusal the device has already acted on.

        A refusal that shows this application's token under a new generation means a renewal took the claim
        again after a lapse. The settings are sent again and the frame is staged again. One that shows token 0
        has the claim renewed at once, held or waited for, and so does one that shows this application's token
        while waiting, since a CLAIM with its reply lost took it. One that shows another holder leaves the claim
        lost, or waited for. Any other error changes nothing.

        :param error: the refusal, with the holder the device has already applied to its generation and HOLDER
        """
        holder = error.holder
        if error.status != STATUS_CLAIMED or holder is None:
            return self._result(_UNCHANGED)
        if self.state is ClaimState.WAITING and (not holder.holder_token or holder.held_by(self.token)):
            return self._result(self._renew())
        if self.state is not ClaimState.HELD:
            return self._result(_UNCHANGED)
        return self._result(self._refusal(holder))

    def keep(self, seconds: float, restage: Optional[Callable[[], object]] = None) -> bool:
        """Hold the lights for a while, sleeping between renewals, as a simple script does.

        :param seconds: how long to hold them
        :param restage: called when a renewal took the claim again, which cleared the staged frame and ended an
            unsaved brightness step, so that the script stages them again
        :return: whether the claim is still held
        :raises HostLightingTimeoutError: when a renewal is not answered
        """
        end = self.clock() + seconds
        while self.state is ClaimState.HELD:
            if self.maintain().restage and restage is not None and self.state is ClaimState.HELD:
                restage()
            left = end - self.clock()
            if left <= 0 or self.state is not ClaimState.HELD:
                break
            time.sleep(min(left, self.seconds_to_renewal or 0.0))
        return self.state is ClaimState.HELD

    def give_up(self) -> bool:
        """Give the claim up when held, which ends the takeover and restores the on-board animations.

        Giving up also ends an unsaved brightness step and puts the CONFIGURE settings back to their defaults.
        Nothing changes when another application holds the claim. The give up is sent while the claim is held, and
        while it is waited for after a CLAIM went unanswered, which may have taken it. Otherwise nothing is sent. A
        board that has gone, or does not answer, restores its animations itself at the keepalive timeout.

        :return: whether the claim was given up. False after an unanswered CLAIM, since the reply does not say
            whether that CLAIM took the claim
        """
        if self.state is not ClaimState.HELD and not (self.state is ClaimState.WAITING and self._unsure):
            self.state = ClaimState.ENDED
            return False
        waiting = self.state is ClaimState.WAITING
        self.state = ClaimState.ENDED
        try:
            holder = self.device.claim(CLAIM_GIVE_UP, self.token)
        except HostLightingError as error:
            # the claim ends at the keepalive timeout, so the requests carry no generation and no HOLDER from now on
            self.device.generation, self.device.holding_exclusive = 0, False
            logger.warning(     # noqa: G200 the cause is what the warning tells
                "could not give the claim up, %s. The keepalive timeout restores the animations", str(error))
            logger.debug("the give up failed", exc_info=True)
            return False
        self.holder = holder
        if holder.holder_token:
            if not waiting:
                logger.info("'%s' took the lights over", holder.application_name)
            return False
        if waiting:
            return False
        logger.info("claim given up, on-board animations restored")
        return True

    def let_lapse(self) -> None:
        """Stop holding without giving up, for the keepalive timeout to end the takeover and the claim.

        The device's requests then carry neither the claim generation nor FLAG_HOLDER, as an application's that
        holds no claim. Once the claim has lapsed, its generation would be refused.
        """
        if self.state is ClaimState.HELD:
            self.device.generation, self.device.holding_exclusive = 0, False
        self.state = ClaimState.ENDED

    def _schedule(self) -> None:
        """Set the next renewal a third of the keepalive timeout from now."""
        self._due = self.clock() + self._interval

    def _configure(self) -> Settings:
        """Send CONFIGURE, and pace the renewals by the timeout the board applied."""
        self._configured = False
        try:
            self.applied = self.device.configure(self.settings.takeover, self.settings.keepalive_ms,
                                                 self.settings.brightness_applied)
        except HostLightingTimeoutError:
            # whether it landed is unknown, and the board's timeout may be the default, the new one or the old, so
            # the next renewal goes at once, sends CONFIGURE again, and the pace is the shortest of them meanwhile
            self._interval = min(self._interval, _timeout(0) / 3, _timeout(self.settings.keepalive_ms) / 3)
            self._due = self.clock()
            raise
        self._configured = True
        self._interval = _timeout(self.applied.keepalive_ms) / 3
        self._schedule()
        return self.applied

    def _send_settings(self) -> Optional[ClaimChange]:
        """Send CONFIGURE while held, applying a status 4 refusal.

        Another application can take the claim over between a renewal and its CONFIGURE.

        :return: what the refusal changed, or None when the board applied the settings
        """
        try:
            self._configure()
        except HostLightingRejectedError as error:
            if error.status != STATUS_CLAIMED or error.holder is None:
                raise
            return self._refusal(error.holder)
        return None

    def _refusal(self, holder: Controller) -> ClaimChange:
        """Apply the holder a status 4 refusal showed while the claim was held, as the specification's table says."""
        if holder.held_by(self.token) and holder.claim_generation == self._generation:
            return ClaimChange(kind=ClaimChangeKind.UNCHANGED, holder=holder)
        if not holder.holder_token:
            self.holder = holder
            try:
                return self._renew(reconfigure=True)
            except HostLightingError:
                # a renewal unanswered leaves the device sending no generation, so nothing is held until one is
                # answered. One that showed this token holds the claim, though its CONFIGURE went unanswered
                if self.state is ClaimState.HELD and not (self.holder is not None and self.holder.held_by(self.token)):
                    self.state = ClaimState.WAITING
                raise
        return self._shown(holder)

    def _renew(self, reconfigure: bool = False) -> ClaimChange:
        """Send the renewal and apply the holder it shows.

        :param reconfigure: send the settings again even under the same generation
        """
        held = self.state is ClaimState.HELD
        self._schedule()
        try:
            holder = self.device.claim(CLAIM_TAKE, self.token, self.name.encode(),
                                       FLAG_KEEPALIVE if held and self.keepalive else 0, self.exclusive)
        except HostLightingTimeoutError:
            self._unsure = True
            raise
        self._unsure = False
        return self._shown(holder, reconfigure)

    def _shown(self, holder: Controller, reconfigure: bool = False) -> ClaimChange:
        """Apply the holder the board showed, sending the settings when the claim was taken again."""
        self.holder = holder
        if holder.held_by(self.token):
            was = self.state
            retaken = reconfigure or not self._configured or holder.claim_generation != self._generation
            self.state, self._generation = ClaimState.HELD, holder.claim_generation
            if was is ClaimState.HELD and not retaken:
                return ClaimChange(kind=ClaimChangeKind.UNCHANGED, holder=holder)
            refusal = self._send_settings()
            if refusal is not None:
                return refusal
            if was is ClaimState.HELD:
                logger.info("claim taken again under generation %d. The settings were sent again, and the frame "
                            "has to be staged again", holder.claim_generation)
                return ClaimChange(kind=ClaimChangeKind.RETAKEN, holder=holder, restage=True)
            logger.info("the lights are unclaimed again, so the claim was taken")
            return ClaimChange(kind=ClaimChangeKind.TAKEN, holder=holder, restage=True)
        if self.state is not ClaimState.HELD:
            return ClaimChange(kind=ClaimChangeKind.UNCHANGED, holder=holder)
        self.state = ClaimState.WAITING if self.wait_when_taken_over else ClaimState.LOST
        logger.info("'%s' took the lights over%s", holder.application_name,
                    ". Waiting for it to give them up" if self.wait_when_taken_over else '')
        return ClaimChange(kind=ClaimChangeKind.LOST, holder=holder)

    def _result(self, change: ClaimChange) -> ClaimChange:
        """Return a change, or the one configure() found when nothing has changed since."""
        pending, self._pending = self._pending, None
        return pending if pending is not None and change.kind is ClaimChangeKind.UNCHANGED else change


def _controllers(events: Iterable[Union[bytes, StateChanged, StateRecord]]) -> Iterator[Controller]:
    """Pick the holder out of each STATE_CHANGED event, decoded or not, or page 1 record, oldest first."""
    for item in events:
        if isinstance(item, StateChanged):
            item = item.record
        if isinstance(item, StateRecord):
            if item.has('controller'):
                yield item.controller
        elif (isinstance(item, (bytes, bytearray)) and is_event(item) and len(item) > 1
              and item[1] == EVENT_STATE_CHANGED):
            try:
                record = decode_state_changed(item).record
            except HostLightingError:
                continue        # cut short
            if record.has('controller'):
                yield record.controller
