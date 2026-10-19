"""Check a GP2040-CE board's Host Lighting implementation against the protocol, and measure it.

The checks cover the HELLO capabilities, framing, every page and field mask,
the pages' cross-references, takeover and keepalive timing, the claim lease
and generation, shared and exclusive claims, unsaved brightness, staging,
events and board-feature argument checks, delivery of replies to a second
handle, then round trip and throughput. run_checks() yields each result as it
is made, and Checks collects them. The checks run under a LightsClaim the
caller holds, renewed without KEEPALIVE so that the takeover checks time the
takeover alone. The measurements maintain the claim between frames.
hlp-conformance and hlp-bench in gp2040ce_hostlighting.tools.conformance run
them. Like the device, the checks belong to one thread.

SPDX-FileCopyrightText: © 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import contextlib
import statistics
import struct
import time
from dataclasses import dataclass, fields
from typing import Callable, Generator, Iterator, Optional

from gp2040ce_hostlighting.claim import ClaimState, LightsClaim
from gp2040ce_hostlighting.protocol import (ACTION_NONE, ADDRESSING_ACTION, ADDRESSING_KIND, ADDRESSING_LED_RUN,
                                            ADDRESSING_LIGHT, ADDRESSING_LIGHT_RUN, ADDRESSING_PIN, ANIMATION_OFF,
                                            BRIGHTNESS_SAVED, CLAIM_EXCLUSIVE, CLAIM_GIVE_UP, CLAIM_TAKE,
                                            CLAIM_TAKE_OVER, CMD_CLAIM, CMD_CLEAR, CMD_COMMIT, CMD_CONFIGURE, CMD_FILL,
                                            CMD_GET_PAGE, CMD_HELLO, CMD_REBOOT, CMD_RELEASE, CMD_SET_ANIMATION,
                                            CMD_SET_ANIMATION_SPEED, CMD_SET_BRIGHTNESS, CMD_SET_INPUT_MODE,
                                            CMD_SET_PROFILE, CMD_STAGE, CMD_SUBSCRIBE, CMD_UNSTAGE, COMMAND_RANGES,
                                            CONTROLLER_SIZE, EVENT_STATE_CHANGED, FLAG_COMMIT_AFTER, FLAG_GENERATION,
                                            FLAG_GENERATION_SHIFT, FLAG_HOLDER, FLAG_KEEPALIVE, FLAG_NO_REPLY,
                                            GENERATION_MAX, HEADER_RECORDS, KEY_EXPANDER, KEY_TRIGGER, MAGIC_INPUT_MODE,
                                            MAGIC_REBOOT, PAGE_ANIMATIONS, PAGE_CONTROLS, PAGE_GROUPS, PAGE_IDENTITY,
                                            PAGE_INPUT_MODES, PAGE_LIGHTS, PAGE_PROFILES, PAGE_RANGES, PAGE_STATE,
                                            PAGE_SUMMARY, PIN_NONE, PIXEL_RGB, PIXEL_RGB565, PIXEL_RGBW, PIXEL_SIZES,
                                            PROTOCOL_MAJOR, RECORD_AREA, REPLY_FLAG, STATUS_CLAIMED, STATUS_INVALID,
                                            STATUS_OK, STATUS_STALE_TOKEN, STATUS_UNSUPPORTED, TAKEOVER_OVERLAY,
                                            HostLightingClaimedError, HostLightingDevice, HostLightingDisconnectedError,
                                            HostLightingError, HostLightingRejectedError, decode_controller, decode_key,
                                            decode_page_header, decode_records, decode_state_changed, is_event,
                                            key_name, stage_capacity)
from gp2040ce_hostlighting.records import (Controller, ControlLights, ControlRecord, Hello, Instances, LightRecord,
                                           Position, Settings, StateRecord)

__all__ = ['CheckResult', 'Checks', 'run_checks', 'measure_round_trips', 'measure_paced_frames',
           'measure_unthrottled_frames']

# what a protocol 2.0 board implements, from the protocol reference. A later 2.x board lists more
REQUEST_FLAGS = {FLAG_COMMIT_AFTER.bit_length() - 1, FLAG_NO_REPLY.bit_length() - 1, FLAG_KEEPALIVE.bit_length() - 1,
                 FLAG_HOLDER.bit_length() - 1} | {bit for bit in range(8) if FLAG_GENERATION >> bit & 1}
COMMANDS = {CMD_HELLO, CMD_GET_PAGE, CMD_CLAIM, CMD_CONFIGURE, CMD_SUBSCRIBE, CMD_STAGE, CMD_UNSTAGE, CMD_FILL,
            CMD_CLEAR, CMD_COMMIT, CMD_RELEASE, CMD_SET_PROFILE, CMD_SET_ANIMATION, CMD_SET_ANIMATION_SPEED,
            CMD_SET_BRIGHTNESS, CMD_SET_INPUT_MODE, CMD_REBOOT}
PAGES = {PAGE_IDENTITY} | set(PAGE_GROUPS)
ADDRESSING = {ADDRESSING_LED_RUN, ADDRESSING_LIGHT_RUN, ADDRESSING_LIGHT, ADDRESSING_PIN, ADDRESSING_ACTION,
              ADDRESSING_KIND}
PIXEL_FORMATS = set(PIXEL_SIZES)
EVENTS = {EVENT_STATE_CHANGED}
INPUT_MODE_CONFIG = 255
KEEPALIVE_MS = 300              # the timeout the timing checks run with


@dataclass(frozen=True, slots=True, kw_only=True)
class CheckResult:
    """One check's result, or a note: a measurement or an observation."""

    section: str
    what: str
    passed: Optional[bool]          # None for a note
    detail: str = ''


class Checks:
    """Collect check results, and count those passed and failed."""

    def __init__(self) -> None:
        """Start with nothing collected."""
        self.results: list[CheckResult] = []
        self.passed = 0
        self.failed = 0

    def add(self, result: CheckResult) -> CheckResult:
        """Collect one result, counting it unless it is a note.

        :return: the result
        """
        self.results.append(result)
        if result.passed:
            self.passed += 1
        elif result.passed is not None:
            self.failed += 1
        return result


class _Checker:
    """Make a run's results, each in the section it was made in."""

    def __init__(self) -> None:
        """Start outside any section."""
        self.name = ''

    def section(self, name: str) -> None:
        """Start a section."""
        self.name = name

    def check(self, ok, what: str, detail: str = '') -> CheckResult:
        """Make one check's result."""
        return CheckResult(section=self.name, what=what, passed=bool(ok), detail=detail)

    def note(self, text: str) -> CheckResult:
        """Make a note."""
        return CheckResult(section=self.name, what=text, passed=None)


def _held(claim: LightsClaim) -> None:
    """End the run once the claim is no longer held.

    :raises HostLightingClaimedError: naming the application that took it over
    """
    if claim.state is not ClaimState.HELD:
        if claim.holder is None or not claim.holder.holder_token:
            raise HostLightingError("the claim is no longer held")
        raise HostLightingClaimedError(claim.holder)


def _renew(claim: LightsClaim) -> None:
    """Renew the claim now."""
    claim.renew()
    _held(claim)


def _keep(claim: LightsClaim) -> None:
    """Renew the claim when a renewal is due."""
    claim.maintain()
    _held(claim)


def _wait(claim: LightsClaim, seconds: float) -> None:
    """Wait, renewing the claim as it goes."""
    claim.keep(seconds)
    _held(claim)


def _retake(claim: LightsClaim) -> None:
    """Take the claim again after a check gave it away, which sends the claim's settings again."""
    claim.take(claim.exclusive)


def _configured(claim: LightsClaim, settings: Settings) -> Settings:
    """Send CONFIGURE through the claim, which renews it first, and return the settings applied."""
    applied = claim.configure(settings)
    _held(claim)
    return applied or Settings()


def _status(reply: bytes) -> tuple:
    """Return a reply's status and, for an invalid argument, the request byte at fault."""
    return reply[2], reply[3]


def _state(device: HostLightingDevice) -> StateRecord:
    """Read page 1.

    It is read as an application that holds no claim would, which no claim refuses: the checks read it
    while another token holds the claim too.
    """
    with _other(device):
        return device.read_record(PAGE_STATE).records[0]


def _events(device: HostLightingDevice, settle: float) -> list:
    """Collect the page 1 records of the STATE_CHANGED events received so far and within settle seconds."""
    reports = device.take_events() + [report for report in device.collect(settle) if is_event(report)]
    return [decode_state_changed(report).record for report in reports if report[1] == EVENT_STATE_CHANGED]


def _claim(device: HostLightingDevice, action: int, token: int, name: bytes = b'') -> bytes:
    return device.request(CMD_CLAIM, bytes([action]) + struct.pack('<I', token) + name[:16].ljust(16, b'\x00'))


def _holder(reply: bytes) -> tuple:
    """Return the holder a CLAIM reply shows: (token, name)."""
    return struct.unpack_from('<I', reply, 8)[0], bytes(reply[12:28]).rstrip(b'\x00').decode('utf-8', 'replace')


def _echoes(reply: bytes, action: int, token: int) -> bool:
    """Tell whether a CLAIM reply echoes the action and token sent."""
    return reply[3:8] == bytes([action]) + struct.pack('<I', token)


def _configure(device: HostLightingDevice, overlay: int, timeout: int, brightness: int) -> bytes:
    return device.request(CMD_CONFIGURE, struct.pack('<BHB', overlay, timeout, brightness))


@contextlib.contextmanager
def _other(device: HostLightingDevice):
    """Send requests as an application that holds no claim.

    They carry no FLAG_HOLDER, and no generation but one a request's own flags carry.
    """
    held = device.generation, device.holding_exclusive
    device.generation, device.holding_exclusive = 0, False
    try:
        yield
    finally:
        device.generation, device.holding_exclusive = held


def _as_other(device: HostLightingDevice, command: int, payload: bytes = b'', flags: int = 0) -> bytes:
    """Send a request as an application that holds no claim."""
    with _other(device):
        return device.request(command, payload, flags)


def _act_as(device: HostLightingDevice, reply: bytes, token: int) -> None:
    """Have the device's requests carry what a token would send after its CLAIM reply.

    When the token holds the claim: the generation shown, and FLAG_HOLDER under an exclusive claim. When it
    does not: neither. The device takes the token as its claim token, so it reads status 4 replies as that
    token's.
    """
    holds = reply[2] == STATUS_OK and _holder(reply)[0] == token
    device.claim_token = token
    device.generation = reply[29] if holds else 0
    device.holding_exclusive = holds and bool(reply[28] & CLAIM_EXCLUSIVE)


def _next(generation: int) -> int:
    """Return the claim generation after a change of holder: 1 to 7, then 1 again."""
    return generation % GENERATION_MAX + 1


def _refused(reply: bytes) -> Optional[Controller]:
    """Return the holder a status 4 reply shows, or None for any other reply."""
    return decode_controller(bytes(reply[3:3 + CONTROLLER_SIZE])) if reply[2] == STATUS_CLAIMED else None


def _unlisted(listed: frozenset, ranges: tuple) -> list:
    """Pick numbers a board does not list: the lowest in each range and the highest of all."""
    free = [[number for number in range(low, high + 1) if number not in listed] for low, high in ranges]
    picks = {numbers[0] for numbers in free if numbers}
    if any(free):
        picks.add(max(number for numbers in free for number in numbers))
    return sorted(picks)


def _answered(device: HostLightingDevice, command: int, sequence: int, timeout: float = 1.0) -> bool:
    try:
        device.wait_reply(command, sequence, timeout)
        return True
    except HostLightingDisconnectedError:
        raise
    except HostLightingError:
        return False


# -- the conformance checks --------------------------------------------------------

def _check_hello(device: HostLightingDevice, checks: _Checker) -> Generator[CheckResult, None, Hello]:
    """Check HELLO and return it.

    A later 2.x board may list more than 2.0 does: additions take free numbers, and hosts gate on the bitmaps.
    """
    checks.section('HELLO')
    hello = device.hello()
    yield checks.check(hello.major == PROTOCOL_MAJOR, f"protocol major {hello.major}")
    yield checks.note(f"protocol {hello.major}.{hello.minor}, board ID {hello.board_id}, "
                      f"reply queue limit {hello.reply_queue_limit}")
    for key, name, wanted in (('request_flags', 'request flags', REQUEST_FLAGS), ('commands', 'commands', COMMANDS),
                              ('pages', 'pages', PAGES), ('addressing_modes', 'addressing modes', ADDRESSING),
                              ('pixel_formats', 'pixel formats', PIXEL_FORMATS), ('events', 'events', EVENTS)):
        listed = getattr(hello, key)
        yield checks.check(listed >= wanted, f"{name} bitmap lists all of 2.0's", f"missing {sorted(wanted - listed)}")
        if listed - wanted:
            yield checks.note(f"{name} beyond 2.0: {sorted(listed - wanted)}")
    yield checks.check(hello.reply_queue_limit >= 1, 'reply queue limit stated')
    return hello


def _check_framing(device: HostLightingDevice, checks: _Checker, hello: Hello) -> Iterator[CheckResult]:
    checks.section('Framing')
    for command in _unlisted(hello.commands, COMMAND_RANGES):
        yield checks.check(device.request(command)[2] == STATUS_UNSUPPORTED,
                           f"unlisted command 0x{command:02X}: status 1")
    # replies reach every application: count only reports with one of these sends' command and sequence;
    # sequence 0, which no other request uses, where a reply would not look like an event
    device.collect(0.05)
    sent = {(command | REPLY_FLAG, device.send(command)) for command in (0x00, 0x80)}
    sent |= {(command | REPLY_FLAG, device.send(command, sequence=0)) for command in (0x81, 0xFF)}
    sent.add((CMD_HELLO | REPLY_FLAG, device.send(CMD_HELLO, flags=FLAG_NO_REPLY, sequence=0)))
    seen = device.collect(0.1)
    stray = [report for report in seen if (report[0], report[1]) in sent]
    yield checks.check(not stray, 'no reply to [0] 0x00 or bit 7, nor to NO_REPLY', f"{len(stray)} replies")
    if len(seen) > len(stray):
        yield checks.note(f"{len(seen) - len(stray)} reports for another application ignored")
    # the queue limit is all the protocol guarantees: past it a request may be discarded, unanswered
    limit = hello.reply_queue_limit
    sequences = [device.send(CMD_HELLO) for _ in range(limit)]
    answered = sum(_answered(device, CMD_HELLO, sequence) for sequence in sequences)
    yield checks.check(answered == len(sequences), 'pipelined requests up to the queue limit all answered',
                       f"{answered} of {len(sequences)}")
    sequences = [device.send(CMD_HELLO) for _ in range(limit + 4)]
    answered = sum(_answered(device, CMD_HELLO, sequence, 0.1) for sequence in sequences)
    yield checks.note(f"{limit + 4} pipelined: {answered} answered")


def _mask_ok(device: HostLightingDevice, page: int, mask: int, widths: tuple, token: int) -> bool:
    """Check one field mask: the groups asked for, their stride, as many records as fit, and the token.

    Mask 0 asks for every group, so a later 2.x board also returns the groups it adds after 2.0's.
    """
    header = decode_page_header(device.get_page(page, 0, mask))
    known = (1 << len(widths)) - 1
    groups = (mask or known) & known
    stride = sum(width for bit, width in enumerate(widths) if groups & (1 << bit))
    later = 0 if mask else header.groups & ~known
    return (header.page == page and header.groups == groups | later
            and (header.stride > stride if later else header.stride == stride)
            and header.count == min(RECORD_AREA // header.stride, header.total) and header.token == token)


def _check_pages(device: HostLightingDevice, checks: _Checker, hello: Hello) -> Generator[CheckResult, None, int]:
    checks.section('Pages')
    token = decode_page_header(device.get_page(PAGE_SUMMARY)).token
    yield checks.check(token != 0, 'state token non-zero')
    for page, widths in PAGE_GROUPS.items():
        if len(widths) > 1:
            masks = list(range(1, 1 << len(widths))) + [0]
            yield checks.check(all(_mask_ok(device, page, mask, widths, token) for mask in masks),
                               f"page {page}: every mask, stride, count, echo and token")
        else:
            # group 0 alone: mask 0 would also return any group a later 2.x board adds
            stride = decode_page_header(device.get_page(page, 0, 1)).stride
            yield checks.check(stride == widths[0], f"page {page}: record of {widths[0]} bytes", f"{stride}")
    for page in _unlisted(hello.pages, PAGE_RANGES):
        reply = device.request(CMD_GET_PAGE, struct.pack('<BHIH', page, 0, 0, 0))
        yield checks.check(_status(reply) == (STATUS_INVALID, 3), f"unlisted page {page}: status 2 at [3]")
    total = decode_page_header(device.get_page(PAGE_LIGHTS)).total
    header = decode_page_header(device.get_page(PAGE_LIGHTS, 0, 1 << 15))
    yield checks.check((header.total, header.count, header.groups) == (total, 0, 0),
                       'a mask naming no defined group: the total, no records, an empty echo')
    header = decode_page_header(device.get_page(PAGE_LIGHTS, total))
    yield checks.check((header.total, header.count) == (total, 0), 'a start at the total: no records')
    reply = device.get_page(PAGE_LIGHTS, 1, 0b11, token ^ 1)
    header = decode_page_header(reply)
    echoed = (header.page, header.start, header.token) == (PAGE_LIGHTS, 1, token)
    yield checks.check(reply[2] == STATUS_STALE_TOKEN and echoed and (header.count, header.groups) == (0, 0),
                       'stale token: status 3, page and start echoed, no records, no groups')
    changes = device.read_record(PAGE_SUMMARY, 0b10).records[0].changes
    yield checks.check(all(changes.counter(page) == 0 for page in range(PAGE_SUMMARY, PAGE_SUMMARY + 8)
                           if page not in hello.pages),
                       'Summary change counters: 0 for the pages the board does not have', f"{changes.counters}")
    yield checks.check(device.read_record(PAGE_SUMMARY, 0b10).records[0].changes == changes, 'a read steps no counter')
    identity = device.read_identity()
    yield checks.check(all(getattr(identity, item.name) is not None for item in fields(identity)),
                       'page 0: every identity string')
    yield checks.note(f"{identity.board_label} ({identity.board_config}, {identity.platform}), "
                      f"firmware {identity.firmware_version}")
    return token


def _check_tables(device: HostLightingDevice, checks: _Checker, token: int) -> Generator[CheckResult, None, dict]:
    state = _state(device)
    modes = device.read_table(PAGE_INPUT_MODES).records
    names = {mode.mode.value: mode.name for mode in modes}
    shown = state.state
    yield checks.note(f"state: input mode {shown.input_mode} ({names.get(shown.input_mode, '?')}), profile "
                      f"{shown.profile}, player {shown.player}, animation {shown.animation}, brightness step "
                      f"{shown.brightness_step_shown}, takeover number {shown.takeover_number}")
    yield checks.note(f"input modes: {_listed_modes(modes)}")
    yield checks.check(state.state.input_mode in names, 'current input mode listed on page 2')
    yield checks.check(INPUT_MODE_CONFIG not in names, 'config mode not listed')
    summary = device.read_record(PAGE_SUMMARY).records[0].summary
    yield checks.note(', '.join(f"{item.name} {getattr(summary, item.name)}" for item in fields(summary)))
    tables = {page: device.read_table(page).records for page in (PAGE_PROFILES, PAGE_CONTROLS, PAGE_LIGHTS,
                                                                 PAGE_ANIMATIONS)}
    for page, key in ((PAGE_PROFILES, 'profiles'), (PAGE_CONTROLS, 'controls'), (PAGE_LIGHTS, 'lights'),
                      (PAGE_ANIMATIONS, 'animations')):
        yield checks.check(len(tables[page]) == getattr(summary, key), f"page {page} total = Summary {key}")
    controls, lights = tables[PAGE_CONTROLS], tables[PAGE_LIGHTS]
    yield checks.check(summary.lit_controls + summary.unlit_controls == summary.controls, 'lit + unlit = controls')
    yield checks.check(sum(control.input.lit for control in controls) == summary.lit_controls,
                       'lit count = Controls lit flags')
    yield checks.check(summary.brightness_maximum >= summary.brightness_steps, 'brightness maximum at least the steps')
    limit = summary.led_limit
    extent = max([min(light.leds.first_led + light.leds.led_count, limit) for light in lights
                  if _reached(light, limit)] or [0])
    yield checks.check(summary.led_extent == extent, 'extent = highest LED below the limit + 1',
                       f"{summary.led_extent} vs {extent}")
    grid = (max([light.grid.x for light in lights] or [-1]) + 1, max([light.grid.y for light in lights] or [-1]) + 1)
    yield checks.check((summary.grid_width, summary.grid_height) == grid, 'grid size = highest light X, Y + 1')
    yield from _check_controls(checks, controls, lights, _unlit_positions(device))
    yield from _check_lights(checks, controls, lights)
    yield checks.check(decode_page_header(device.get_page(PAGE_CONTROLS)).token == token,
                       'token unchanged across reads')
    result: dict = {'summary': summary, 'modes': modes, 'state': state}
    result.update(tables)
    return result


def _listed_modes(modes: tuple) -> str:
    """List page 2's input modes with the lighting interface each carries."""
    def lighting(mode) -> str:
        if not mode.mode.lighting:
            return ''
        return ' (lighting once a PC is detected)' if mode.mode.detected else ' (lighting)'
    return ', '.join(f"{mode.mode.value} {mode.name}{lighting(mode)}" for mode in modes)


def _unlit_positions(device: HostLightingDevice) -> list:
    """List the unlit controls with a position group other than first LED 0xFFFF and a zero rectangle.

    The decoder folds an unlit control's position to None, so page 10's groups 0 and 2 are read as bytes.
    """
    wrong: list = []
    start, total = 0, 1
    while start < total:
        reply = device.get_page(PAGE_CONTROLS, start, 0b101)
        header = decode_page_header(reply)
        if not header.count:
            break
        for index in range(header.count):
            record = reply[HEADER_RECORDS + index * header.stride:HEADER_RECORDS + (index + 1) * header.stride]
            if not record[3] & 0x02 and bytes(record[4:10]) != b'\xff\xff' + bytes(4):
                wrong.append(record[0])
        start, total = start + header.count, header.total
    return wrong


def _expected_control(control: ControlRecord, controls: tuple, lights: tuple) -> tuple:
    """Give what the Lights page says a control's flags, instances, position and lights are."""
    mine = [light for light in lights if light.owner.pin == control.input.key]
    same = [other.input.key for other in controls if other.input.action == control.input.action]
    position = Position(first_led=None, grid_rectangle=None)
    if mine:
        xs, ys = [light.grid.x for light in mine], [light.grid.y for light in mine]
        position = Position(first_led=mine[0].leds.first_led, grid_rectangle=(min(xs), min(ys), max(xs), max(ys)))
    return (len(same) > 1, bool(mine), Instances(instance=same.index(control.input.key) + 1, instances=len(same)),
            position, ControlLights(first_light=mine[0].ordinal if mine else None, light_count=len(mine)))


def _check_controls(checks: _Checker, controls: tuple, lights: tuple, unlit_positions: list) -> Iterator[CheckResult]:
    wrong = 0
    for control in controls:
        expected = _expected_control(control, controls, lights)
        got = (control.input.duplicated, control.input.lit, control.instances, control.position, control.lights)
        if got != expected:
            wrong += 1
            yield checks.note(f"control {key_name(control.input.key)}: {got} vs {expected}")
    for key in unlit_positions:
        wrong += 1
        yield checks.note(f"control {key_name(key)}: unlit, but its position group is not FFFF 0000 0000")
    yield checks.check(not wrong, 'Controls agree with the Lights page (flags, instances, position, lights)')
    keys = [control.input.key for control in controls]
    yield checks.check(keys == sorted(set(keys)), 'Controls in ascending key order, each key once')
    yield checks.check(PIN_NONE not in keys, 'no control key is 0xFF (none)')
    later = [key_name(key) for key in keys if decode_key(key)[0] == 'unknown' and key != PIN_NONE]
    if later:
        yield checks.note(f"keys beyond 2.0: {', '.join(later)}")
    lit_elsewhere = [control for control in controls
                     if control.input.lit and control.input.source not in ('gpio', 'hall_effect')]
    yield checks.check(not lit_elsewhere, 'only GPIO and Hall-effect controls are lit')
    owners = {light.owner.pin for light in lights} & set(keys)
    yield checks.check({control.input.key for control in controls if control.input.lit} == owners,
                       'lit control keys = the Lights owner pins that are controls')


def _check_lights(checks: _Checker, controls: tuple, lights: tuple) -> Iterator[CheckResult]:
    by_key = {control.input.key: control for control in controls}
    wrong = 0
    for light in lights:
        control = by_key.get(light.owner.pin)
        none = Instances(instance=None, instances=None)
        if light.owner.pin is None:
            expected = (None, False, none)
        elif control is not None:
            expected = (control.input.action, control.input.duplicated, control.instances)
        else:
            expected = (light.owner.action, False, none)
        got = (light.owner.action, light.kind.duplicated, light.owner_instances)
        if got != expected:
            wrong += 1
            yield checks.note(f"light {light.ordinal}: {got} vs {expected}")
    yield checks.check(not wrong, 'Lights owner action, flag and instances agree with Controls')
    owners = sorted({light.owner.pin for light in lights if light.owner.pin is not None
                     and decode_key(light.owner.pin)[0] not in ('gpio', 'hall_effect')})
    yield checks.check(not owners, 'Lights owner pins are GPIO pins or Hall-effect triggers',
                       ', '.join(f"0x{pin:02X}" for pin in owners))


def _check_subscribe(device: HostLightingDevice, checks: _Checker, events: frozenset) -> Iterator[CheckResult]:
    yield checks.check(EVENT_STATE_CHANGED in device.subscribe([EVENT_STATE_CHANGED]), 'SUBSCRIBE 1: event 0 enabled')
    yield checks.check(EVENT_STATE_CHANGED in device.subscribe([]),
                       'SUBSCRIBE 0 keeps event 0 (subscriptions only add)')
    yield checks.check(device.subscribe(range(16)) == events, 'SUBSCRIBE 0xFFFF: the events HELLO lists, no others')
    yield checks.check(EVENT_STATE_CHANGED in device.hello().events_enabled, 'HELLO [45..46] shows event 0')


def _check_configure(device: HostLightingDevice, checks: _Checker, claim: LightsClaim) -> Iterator[CheckResult]:
    """Check CONFIGURE's timeout rule, its reply against page 1's Settings, and its brightness check."""
    timeouts = [_configured(claim, Settings(keepalive_ms=sent)).keepalive_ms for sent in (0, 50, 20000)]
    yield checks.check(timeouts == [2000, 100, 10000], 'CONFIGURE timeout: 0 is 2000 ms, clamped to 100-10000',
                       f"{timeouts}")
    asked = Settings(takeover=TAKEOVER_OVERLAY, keepalive_ms=777, brightness_applied=False)
    applied = _configured(claim, asked)
    settings = _state(device).settings
    yield checks.check(applied == asked and settings == asked, 'page 1 Settings show what CONFIGURE applied',
                       f"{settings}")
    yield checks.check(_status(_configure(device, 0, 777, 2)) == (STATUS_INVALID, 6),
                       'CONFIGURE brightness 2: status 2 at [6]')


def _check_takeover(device: HostLightingDevice, checks: _Checker, tables: dict,
                    claim: LightsClaim) -> Iterator[CheckResult]:
    """Check the takeover's number, keepalive and expiry.

    Every wait renews the claim with a plain CLAIM, so the lease never lapses and the takeover expires on its own
    timer. A lapse would end it too, and hide a broken expiry.
    """
    checks.section('Takeover and keepalive')
    device.request_ok(CMD_RELEASE)
    yield from _check_configure(device, checks, claim)
    _configured(claim, Settings(keepalive_ms=KEEPALIVE_MS))
    yield from _check_subscribe(device, checks, tables['hello'].events)
    _events(device, 0.05)
    _renew(claim)
    after = (_state(device).state.takeover_number + 1) & 0xFF
    device.request_ok(CMD_COMMIT)
    state = _state(device)
    yield checks.check(state.state.takeover_live and state.state.takeover_number == after,
                       'COMMIT starts a takeover, number +1')
    device.request_ok(CMD_HELLO, flags=FLAG_COMMIT_AFTER)
    yield checks.check(_state(device).state.takeover_number == after, 'a publish into a live takeover keeps the number')
    for _ in range(6):
        time.sleep(KEEPALIVE_MS / 2000)
        _renew(claim)
        device.send(CMD_HELLO, flags=FLAG_KEEPALIVE | FLAG_NO_REPLY)
    yield checks.check(_state(device).state.takeover_live, 'HELLO with KEEPALIVE keeps it live past the timeout')
    for _ in range(3):
        time.sleep(KEEPALIVE_MS / 2000)
        _renew(claim)
        device.request_ok(CMD_HELLO)
    time.sleep(KEEPALIVE_MS / 3000)
    yield checks.check(not _state(device).state.takeover_live, 'HELLO alone does not: it expires')
    yield checks.check(any(not event.state.takeover_live for event in _events(device, 0.05)), 'STATE_CHANGED on expiry')
    _renew(claim)
    device.request_ok(CMD_COMMIT)
    for _ in range(3):
        time.sleep(KEEPALIVE_MS / 2000)
        _renew(claim)
    time.sleep(KEEPALIVE_MS / 3000)
    state = _state(device)
    yield checks.check(not state.state.takeover_live and state.controller.holder_token == claim.token,
                       "the holder's CLAIM alone does not keep it: it expires, the claim stays")
    _renew(claim)
    number = state.state.takeover_number
    device.request_ok(CMD_HELLO, flags=FLAG_KEEPALIVE)
    state = _state(device)
    yield checks.check(not state.state.takeover_live and state.state.takeover_number == number,
                       'KEEPALIVE starts no takeover')
    device.send(CMD_COMMIT, flags=FLAG_NO_REPLY)
    state = _state(device)
    yield checks.check(state.state.takeover_live and state.state.takeover_number == (number + 1) & 0xFF,
                       'a NO_REPLY request still executes: COMMIT starts a takeover')
    device.request_ok(CMD_RELEASE)
    reply = device.request(CMD_SET_PROFILE, bytes([tables['summary'].profiles + 1]), FLAG_COMMIT_AFTER)
    yield checks.check(reply[2] == STATUS_INVALID and not _state(device).state.takeover_live,
                       'flags on a failed command do nothing')
    _renew(claim)
    device.request_ok(CMD_STAGE, bytes([ADDRESSING_LED_RUN]) + struct.pack('<HB', 0, 1) + b'\x10\x20\x30',
                      FLAG_COMMIT_AFTER)
    start = time.perf_counter()
    while _state(device).state.takeover_live and time.perf_counter() - start < 2:
        time.sleep(0.01)
        _keep(claim)
    took = (time.perf_counter() - start) * 1000
    _renew(claim)               # a take over, not the timeout, may have ended it
    yield checks.check(KEEPALIVE_MS <= took + 20 and took < KEEPALIVE_MS + 200, 'the takeover ends at the timeout',
                       f"{took:.0f} ms")
    yield checks.note(f"expiry seen after {took:.0f} ms (timeout {KEEPALIVE_MS})")


def _check_claim(device: HostLightingDevice, checks: _Checker, claim: LightsClaim) -> Iterator[CheckResult]:
    """Check the claim's rules under the claim the conformance run holds, then take it again."""
    checks.section('Claim and lease')
    ours, name = claim.token, claim.name
    other = ours ^ 0xFFFFFFFF or 1          # any other non-zero token
    _renew(claim)
    generation = device.generation
    renamed = _claim(device, CLAIM_TAKE, ours, b'hlp-renamed')
    named = _state(device).controller.application_name
    yield checks.check(_holder(renamed) == (ours, 'hlp-renamed') and named == 'hlp-renamed',
                       'a renewal keeps the claim and records the new name')
    yield checks.check(1 <= generation <= GENERATION_MAX and renamed[29] == generation,
                       'the claim generation is 1-7, and a renewal keeps it', f"{renamed[29]} after {generation}")
    refused = _claim(device, CLAIM_TAKE, other, b'other')
    yield checks.check(_holder(refused)[0] == ours, 'another token is refused, reply OK')
    ignored = _claim(device, CLAIM_GIVE_UP, other)
    yield checks.check(_holder(ignored)[0] == ours, 'give up by another token changes nothing')
    reply = _claim(device, CLAIM_GIVE_UP + 1, other)
    yield checks.check(_status(reply) == (STATUS_INVALID, 3) and not any(reply[4:8]),
                       'action 3: status 2 at [3], no echo')
    yield checks.check(_status(_claim(device, CLAIM_TAKE, 0)) == (STATUS_INVALID, 4), 'token 0: status 2 at [4]')
    device.request_ok(CMD_COMMIT)
    device.request_ok(CMD_RELEASE)
    state = _state(device)
    yield checks.check(not state.state.takeover_live and state.controller.holder_token == ours,
                       'RELEASE ends the takeover, not the claim')
    device.request_ok(CMD_COMMIT)
    took = _claim(device, CLAIM_TAKE_OVER, other, b'other')
    _act_as(device, took, other)
    state = _state(device)
    yield checks.check(_holder(took)[0] == other and not state.state.takeover_live, 'take over ends the takeover')
    yield checks.check(took[29] == _next(generation) and state.controller.claim_generation == took[29],
                       'a change of holder steps the claim generation', f"{took[29]} after {generation}")
    yield checks.check(state.settings == Settings(), 'and puts the CONFIGURE settings back to their defaults',
                       f"{state.settings}")
    # the previous holder has not seen the change: its requests still carry its generation
    holder = Controller(holder_token=other, application_name='other', exclusive=False, claim_generation=took[29])
    previous = generation << FLAG_GENERATION_SHIFT
    stage = bytes([ADDRESSING_LED_RUN]) + struct.pack('<HB', 0, 1) + bytes(3)
    wrong = [f"0x{command:02X}: status {reply[2]}" for command, payload in (
        (CMD_STAGE, stage), (CMD_COMMIT, b''), (CMD_HELLO, b''), (CMD_CONFIGURE, struct.pack('<BHB', 1, 0, 0)),
        (CMD_GET_PAGE, struct.pack('<BHIH', PAGE_STATE, 0, 0, 0)), (CMD_SUBSCRIBE, bytes(2)))
        for reply in [device.request(command, payload, previous)]
        if _refused(reply) != holder or any(reply[3 + CONTROLLER_SIZE:])]
    yield checks.check(not wrong, "a request under the previous holder's generation is refused, reads included: status "
                       '4 with the holder now, the rest zero', ', '.join(wrong))
    device.send(CMD_COMMIT, flags=previous | FLAG_NO_REPLY)
    state = _state(device)
    yield checks.check(not state.state.takeover_live and state.settings.takeover != TAKEOVER_OVERLAY,
                       'and changes nothing, answered or not')
    yield checks.check(device.request(CMD_CLEAR)[2] == STATUS_OK and _as_other(device, CMD_CLEAR)[2] == STATUS_OK,
                       'the current generation acts, and so does a request that carries none')
    _configure(device, 0, KEEPALIVE_MS, 1)    # as the holder now, with a lease nothing renews
    _events(device, 0.05)
    time.sleep(KEEPALIVE_MS * 1.5 / 1000)
    state = _state(device)
    yield checks.check(state.controller.holder_token == 0, 'the lease lapses after the timeout')
    yield checks.check(state.controller.claim_generation == _next(took[29]) and state.settings.keepalive_ms == 2000,
                       'a lapse is a change of holder too: the generation steps, the settings return',
                       f"generation {state.controller.claim_generation}, keepalive {state.settings.keepalive_ms} ms")
    yield checks.check(any(event.controller.holder_token == 0 for event in _events(device, 0.05)),
                       'STATE_CHANGED on the lapse')
    lapsed = _refused(device.request(CMD_COMMIT))    # still under the lapsed claim's generation
    yield checks.check(lapsed is not None and lapsed.holder_token == 0 and
                       lapsed.claim_generation == state.controller.claim_generation,
                       'with no claim held an outdated generation is refused too: status 4, holder 0', f"{lapsed}")
    taken = _claim(device, CLAIM_TAKE, ours, name.encode())
    _act_as(device, taken, ours)
    yield checks.check(_holder(taken) == (ours, name), 'claim taken when unclaimed, holder shown: CLAIM ignores the '
                       'outdated generation it was sent under')
    given = _claim(device, CLAIM_GIVE_UP, ours)
    _act_as(device, given, ours)
    yield checks.check(_holder(given)[0] == 0, 'give up clears the claim')
    # a holder that stalled past the timeout: its renewal, which shows nothing, takes the claim again
    device.send(CMD_CLAIM, bytes([CLAIM_TAKE]) + struct.pack('<I', ours) + name.encode()[:16].ljust(16, b'\x00'),
                FLAG_NO_REPLY)
    retaken = _refused(device.request(CMD_COMMIT, b'', taken[29] << FLAG_GENERATION_SHIFT))
    yield checks.check(retaken is not None and retaken.holder_token == ours and
                       retaken.claim_generation == _next(given[29]),
                       "a renewal without a reply takes a lapsed claim again: the holder's next request, under its old "
                       'generation, is refused and shows its own token and the new generation', f"{retaken}")
    _act_as(device, _claim(device, CLAIM_GIVE_UP, ours), ours)
    # the first claim and the give up are changes of holder too. Eight of them pass 7
    steps, defaults = [_state(device).controller.claim_generation], []
    for _ in range(4):
        for action in (CLAIM_TAKE, CLAIM_GIVE_UP):
            _configure(device, 1, 777, 0)
            reply = _claim(device, action, ours, name.encode())
            _act_as(device, reply, ours)
            state = _state(device)
            steps.append(reply[29])
            defaults.append(state.settings)
    yield checks.check(all(after == _next(before) for before, after in zip(steps, steps[1:])) and 1 in steps[1:],
                       'the generation steps at a first claim and at a give up, and from 7 back to 1', f"{steps}")
    yield checks.check(set(defaults) == {Settings()}, 'and each puts the CONFIGURE settings back to their '
                       'defaults', f"{set(defaults)}")
    yield checks.check(all(_echoes(reply, action, token) for reply, action, token in (
              (renamed, CLAIM_TAKE, ours), (refused, CLAIM_TAKE, other), (ignored, CLAIM_GIVE_UP, other),
              (took, CLAIM_TAKE_OVER, other), (taken, CLAIM_TAKE, ours), (given, CLAIM_GIVE_UP, ours))),
              'every CLAIM reply echoes the action and token')
    _retake(claim)


def _check_exclusive(device: HostLightingDevice, checks: _Checker, tables: dict,
                     claim: LightsClaim) -> Iterator[CheckResult]:
    """Check the exclusive claim: what it refuses, what it lets through, and CLAIM under it.

    It refuses a change sent without both HOLDER and the current generation.
    """
    checks.section('Exclusive claim')
    kind = claim.exclusive
    other = claim.token ^ 0xFFFFFFFF or 1          # any other non-zero token
    claim.exclusive = False
    _renew(claim)
    _events(device, 0.05)
    claim.exclusive = True
    _renew(claim)
    state = _state(device)
    yield checks.check(state.controller.exclusive and state.controller.holder_token == claim.token,
                       'page 1 shows the exclusive claim')
    yield checks.check(any(event.controller.exclusive for event in _events(device, 0.05)),
                       "STATE_CHANGED when the holder's renewal changes the claim flags")
    generation = device.generation
    holder = struct.pack('<I', claim.token) + claim.name.encode()[:16].ljust(16, b'\x00') + bytes([CLAIM_EXCLUSIVE,
                                                                                                  generation])
    device.request_ok(CMD_RELEASE)
    number = _state(device).state.takeover_number
    # harmless were they executed: the current settings, an empty run, the saved step, a profile and a REBOOT target
    # that do not exist
    changes = [(CMD_CONFIGURE, struct.pack('<BHB', 0, KEEPALIVE_MS, 1)),
               (CMD_STAGE, bytes([ADDRESSING_LED_RUN]) + struct.pack('<HB', 0, 1) + bytes(3)), (CMD_CLEAR, b''),
               (CMD_COMMIT, b''), (CMD_RELEASE, b''), (CMD_SET_BRIGHTNESS, bytes([BRIGHTNESS_SAVED])),
               (CMD_SET_PROFILE, bytes([tables['summary'].profiles + 1])),
               (CMD_REBOOT, bytes([0xFF]) + MAGIC_REBOOT)]
    wrong = []
    for command, payload in changes:
        reply = _as_other(device, command, payload)
        if reply[2] != STATUS_CLAIMED or bytes(reply[3:3 + CONTROLLER_SIZE]) != holder:
            wrong.append(f"0x{command:02X}: status {reply[2]}")
    yield checks.check(not wrong, 'without HOLDER, changes are refused before their arguments are checked: '
                       'status 4 with the holder', ', '.join(wrong))
    partial = (FLAG_HOLDER, generation << FLAG_GENERATION_SHIFT,
               FLAG_HOLDER | _next(generation) << FLAG_GENERATION_SHIFT)
    yield checks.check(all(_as_other(device, CMD_COMMIT, b'', flags)[2] == STATUS_CLAIMED for flags in partial),
                       'HOLDER without the current generation, and the generation without HOLDER, are refused too')
    state = _state(device)
    yield checks.check(not state.state.takeover_live and state.state.takeover_number == number,
                       'a refused request changes nothing')
    with _other(device):
        device.send(CMD_COMMIT, flags=FLAG_NO_REPLY)
    yield checks.check(not _state(device).state.takeover_live, 'a refused NO_REPLY request is not executed either')
    page_1 = struct.pack('<BHIH', PAGE_STATE, 0, 0, 0)
    reads = [_as_other(device, CMD_HELLO), _as_other(device, CMD_GET_PAGE, page_1),
             _as_other(device, CMD_SUBSCRIBE, struct.pack('<H', 1 << EVENT_STATE_CHANGED))]
    yield checks.check(all(reply[2] == STATUS_OK for reply in reads), 'HELLO, GET_PAGE and SUBSCRIBE are not refused')
    flagged = [_as_other(device, CMD_HELLO, flags=FLAG_KEEPALIVE), _as_other(device, CMD_GET_PAGE, page_1,
                                                                             FLAG_COMMIT_AFTER)]
    yield checks.check(all(reply[2] == STATUS_CLAIMED for reply in flagged), 'with KEEPALIVE or COMMIT_AFTER they are')
    unlisted = _unlisted(tables['hello'].commands, COMMAND_RANGES)[0]
    yield checks.check(_as_other(device, unlisted)[2] == STATUS_UNSUPPORTED, 'an unlisted command is still status 1')
    yield checks.check(device.request(CMD_COMMIT)[2] == STATUS_OK and _state(device).state.takeover_live,
                       "the holder's requests, with HOLDER and the generation, act")
    other_claim = bytes([CLAIM_TAKE]) + struct.pack('<I', other) + b'other'.ljust(16, b'\x00')
    replies = []
    for _ in range(4):          # the holder renews its lease only
        time.sleep(KEEPALIVE_MS / 2000)
        _renew(claim)
        replies.append(_as_other(device, CMD_CLAIM, other_claim, FLAG_KEEPALIVE))
    time.sleep(KEEPALIVE_MS / 3000)
    yield checks.check(all(reply[2] == STATUS_OK and _holder(reply)[0] == claim.token for reply in replies),
                       "another application's CLAIM is never refused")
    yield checks.check(not _state(device).state.takeover_live,
                       "its KEEPALIVE does not act: the holder's takeover expires")
    take_over = bytes([CLAIM_TAKE_OVER]) + struct.pack('<I', other) + b'other'.ljust(16, b'\x00')
    took = _as_other(device, CMD_CLAIM, take_over, FLAG_COMMIT_AFTER)
    state = _state(device)
    yield checks.check(took[2] == STATUS_OK and _holder(took)[0] == other and not state.controller.exclusive,
                       "a take over records the taker's claim flags")
    yield checks.check(state.state.takeover_live, 'without HOLDER, a take over has its COMMIT_AFTER act for the taker')
    _act_as(device, took, other)
    yield checks.check(_as_other(device, CMD_SET_BRIGHTNESS, bytes([BRIGHTNESS_SAVED]))[2] == STATUS_OK,
                       'a shared claim refuses nothing')
    yield checks.check(device.request(CMD_SET_BRIGHTNESS, bytes([BRIGHTNESS_SAVED]), FLAG_HOLDER)[2] == STATUS_OK,
                       'under a shared claim HOLDER is ignored: a request carrying it acts')
    reply = _as_other(device, CMD_SET_BRIGHTNESS, bytes([BRIGHTNESS_SAVED]), generation << FLAG_GENERATION_SHIFT)
    refusal = _refused(reply)
    yield checks.check(refusal is not None and refusal.holder_token == other,
                       "but not a request under the previous holder's generation: status 4 with the holder now")
    _act_as(device, _claim(device, CLAIM_GIVE_UP, other), other)
    claim.exclusive = kind
    _retake(claim)


def _check_unsaved(device: HostLightingDevice, checks: _Checker, tables: dict,
                   claim: LightsClaim) -> Iterator[CheckResult]:
    """Check the unsaved brightness step: shown at once, nothing written, and what ends it."""
    checks.section('Unsaved brightness')
    yield checks.check(_status(device.request(CMD_SET_BRIGHTNESS, bytes([0, 2]))) == (STATUS_INVALID, 4),
                       'SET_BRIGHTNESS save 2: status 2 at [4]')
    if not tables['summary'].brightness_steps:
        ok = all(device.request(CMD_SET_BRIGHTNESS, bytes([step]))[2] == STATUS_OK for step in (0, BRIGHTNESS_SAVED))
        yield checks.check(ok and not _state(device).state.brightness_unsaved,
                           'no LED output: steps 0 and 0xFF, none unsaved')
        return
    device.request_ok(CMD_SET_BRIGHTNESS, bytes([BRIGHTNESS_SAVED]))    # from the saved step
    saved = _state(device).state.brightness_step_shown
    step = saved - 1 if saved > 0 else 1
    _events(device, 0.05)
    device.request_ok(CMD_SET_BRIGHTNESS, bytes([step]))
    state = _state(device)
    yield checks.check(state.state.brightness_step_shown == step and state.state.brightness_unsaved,
                       'an unsaved step is shown at once, flagged unsaved')
    yield checks.check(state.state.saved_brightness_step == saved, 'page 1 still shows the saved step',
                       f"{state.state.saved_brightness_step} vs {saved}")
    yield checks.check(any(event.state.brightness_unsaved for event in _events(device, 0.05)),
                       'STATE_CHANGED for the unsaved step')
    longest, start = 0.0, time.monotonic()
    while time.monotonic() - start < 1.5:       # a saved step pauses the replies 175-190 ms a second later
        sent = time.perf_counter()
        device.request_ok(CMD_HELLO, flags=FLAG_KEEPALIVE)
        longest = max(longest, time.perf_counter() - sent)
        _keep(claim)
    yield checks.check(longest < 0.15, 'it writes nothing to flash: no reply pause', f"longest {longest * 1000:.0f} ms")
    yield checks.check(_state(device).state.brightness_unsaved, 'KEEPALIVE keeps it past the keepalive timeout')
    for _ in range(4):          # 400 ms past the 300 ms timeout, refreshed only by CLEAR, a staging command
        time.sleep(KEEPALIVE_MS / 3000)
        _keep(claim)
        device.request_ok(CMD_CLEAR)
    yield checks.check(_state(device).state.brightness_unsaved, 'a staging command (CLEAR) keeps it too')
    device.request_ok(CMD_RELEASE)
    yield checks.check(_state(device).state.brightness_unsaved, 'RELEASE does not end it')
    _wait(claim, KEEPALIVE_MS * 1.5 / 1000)       # the plain CLAIM renewals refresh nothing
    state = _state(device)
    yield checks.check(state.state.brightness_step_shown == saved and not state.state.brightness_unsaved,
                       'it ends a keepalive timeout after the last refresh: the saved step again')
    device.request_ok(CMD_SET_BRIGHTNESS, bytes([step]))
    device.request_ok(CMD_SET_BRIGHTNESS, bytes([BRIGHTNESS_SAVED]))
    state = _state(device)
    yield checks.check(state.state.brightness_step_shown == saved and not state.state.brightness_unsaved,
                       '0xFF shows the saved step at once')
    other = claim.token ^ 0xFFFFFFFF or 1
    device.request_ok(CMD_SET_BRIGHTNESS, bytes([step]))
    _claim(device, CLAIM_TAKE_OVER, other, b'other')
    state = _state(device)
    yield checks.check(state.state.brightness_step_shown == saved and not state.state.brightness_unsaved,
                       'a change of holder ends it')
    _claim(device, CLAIM_GIVE_UP, other)
    _retake(claim)


def _reached(light: LightRecord, limit: int) -> bool:
    """Tell whether STAGE reaches a light: it has an LED below the addressable LED limit."""
    return light.leds.first_led < limit and light.leds.led_count > 0


def _check_runs(device: HostLightingDevice, checks: _Checker, limit: int, lights: tuple) -> Iterator[CheckResult]:
    for name, pixel_format in (('RGB', PIXEL_RGB), ('RGBW', PIXEL_RGBW), ('RGB565', PIXEL_RGB565)):
        capacity = stage_capacity(ADDRESSING_LED_RUN, pixel_format)
        head = bytes([ADDRESSING_LED_RUN | (pixel_format << 4)])
        pixels = bytes(capacity * PIXEL_SIZES[pixel_format])
        full = device.request(CMD_STAGE, head + struct.pack('<HB', 0, capacity) + pixels)
        over = device.request(CMD_STAGE, head + struct.pack('<HB', 0, capacity + 1))
        yield checks.check(full[2] == STATUS_OK and full[3] == capacity and struct.unpack_from('<I', full, 5)[0] ==
                           (1 << capacity) - 1 and _status(over) == (STATUS_INVALID, 6),
                           f"{name} LED run: {capacity} per report, one more rejected")
    run = bytes([ADDRESSING_LED_RUN])
    reply = device.request(CMD_STAGE, run + struct.pack('<HB', limit, 1) + b'\x01' * 3)
    yield checks.check(_status(reply) == (STATUS_INVALID, 4), 'LED run at the limit: status 2 at [4]')
    reply = device.request(CMD_STAGE, run + struct.pack('<HB', limit - 1, 2) + b'\x01' * 6)
    yield checks.check(_status(reply) == (STATUS_INVALID, 6), 'LED run past the limit: status 2 at [6]')
    count = min(len(lights), stage_capacity(ADDRESSING_LIGHT_RUN, PIXEL_RGB))
    if count:
        reply = device.request(CMD_STAGE, bytes([ADDRESSING_LIGHT_RUN]) + struct.pack('<HB', 0, count)
                               + b'\x01' * 3 * count)
        reached = sum(_reached(light, limit) for light in lights[:count])
        yield checks.check(reply[2] == STATUS_OK and (reply[3], reply[4]) == (reached, count - reached),
                           'light run: every light within the limit applies, the rest are skipped')
    reply = device.request(CMD_STAGE, bytes([ADDRESSING_LIGHT_RUN]) + struct.pack('<HB', len(lights), 2) + b'\x01' * 6)
    yield checks.check(reply[2] == STATUS_OK and (reply[3], reply[4]) == (0, 2), 'light run past the lights: skipped')


def _check_entries(device: HostLightingDevice, checks: _Checker, controls: tuple, lights: tuple,
                   limit: int) -> Iterator[CheckResult]:
    """Check STAGE by action, pin and kind, and UNSTAGE, on addresses with a light within the limit."""
    reached = [light for light in lights if _reached(light, limit)]
    actions = sorted({control.input.action for control in controls if control.input.lit}
                     & {light.owner.action for light in reached})
    actions = actions[:stage_capacity(ADDRESSING_ACTION, PIXEL_RGB)]
    if actions:
        body = b''.join(struct.pack('<h', action) + b'\x02' * 3 for action in actions)
        reply = device.request(CMD_STAGE, bytes([ADDRESSING_ACTION, len(actions)]) + body)
        yield checks.check(reply[2] == STATUS_OK and reply[3] == len(actions),
                           'STAGE by action: every lit action within the limit applies')
    body = struct.pack('<h', ACTION_NONE) + b'\x01' * 3 + struct.pack('<h', 999) + b'\x01' * 3
    reply = device.request(CMD_STAGE, bytes([ADDRESSING_ACTION, 2]) + body)
    yield checks.check((reply[3], reply[4]) == (0, 2), 'action 0x8000 and an absent action are skipped')
    reply = device.request(CMD_STAGE, bytes([ADDRESSING_PIN, 1, 0xFF, 1, 1, 1]))
    yield checks.check(reply[3] == 0, 'pin 0xFF matches no light')
    # A Hall-effect trigger can own a light, so take a trigger key that no light carries
    owned = {light.owner.pin for light in lights}
    unowned = [key for key in range(KEY_TRIGGER, KEY_EXPANDER) if key not in owned][:1] + [KEY_EXPANDER]
    body = b''.join(bytes([key, 1, 1, 1]) for key in unowned)
    reply = device.request(CMD_STAGE, bytes([ADDRESSING_PIN, len(unowned)]) + body)
    yield checks.check((reply[3], reply[4]) == (0, len(unowned)), 'add-on keys that own no light match no light')
    reply = device.request(CMD_UNSTAGE, bytes([ADDRESSING_PIN, len(unowned)] + unowned))
    yield checks.check((reply[3], reply[4]) == (0, len(unowned)),
                       'UNSTAGE: add-on keys that own no light match no light')
    pins = sorted({control.input.key for control in controls if control.input.lit}
                  & {light.owner.pin for light in reached})
    pins = pins[:stage_capacity(ADDRESSING_PIN, PIXEL_RGB)]
    if pins:
        body = b''.join(bytes([pin, 4, 4, 4]) for pin in pins)
        reply = device.request(CMD_STAGE, bytes([ADDRESSING_PIN, len(pins)]) + body)
        yield checks.check(reply[2] == STATUS_OK and reply[3] == len(pins),
                           'STAGE by pin: every lit pin within the limit applies')
    kinds = sorted({light.kind.value for light in reached})
    if kinds:
        body = b''.join(bytes([kind, 3, 3, 3]) for kind in kinds)
        reply = device.request(CMD_STAGE, bytes([ADDRESSING_KIND, len(kinds)]) + body)
        yield checks.check(reply[3] == len(kinds), 'STAGE by kind: every kind within the limit applies')
    if reached:
        reply = device.request(CMD_UNSTAGE, bytes([ADDRESSING_LIGHT, 1]) + struct.pack('<H', reached[0].ordinal))
        yield checks.check(reply[2] == STATUS_OK and reply[3] == 1, 'UNSTAGE a light')


def _check_stage_arguments(device: HostLightingDevice, checks: _Checker, hello: Hello) -> Iterator[CheckResult]:
    unlisted = [mode for mode in range(16) if mode not in hello.addressing_modes]
    rejected = [mode for mode in unlisted
                if _status(device.request(CMD_STAGE, bytes([mode, 1]))) != (STATUS_INVALID, 3)]
    yield checks.check(not rejected, 'STAGE unlisted addressing: status 2 at [3]', f"not for {rejected}")
    run = struct.pack('<HB', 0, 1) + bytes(3)
    replies = {pixel_format: device.request(CMD_STAGE, bytes([ADDRESSING_LED_RUN | pixel_format << 4]) + run)
               for pixel_format in _unlisted(hello.pixel_formats, ((0, 15),))}
    wrong = [pixel_format for pixel_format, reply in replies.items() if _status(reply) != (STATUS_INVALID, 3)]
    yield checks.check(not wrong, 'STAGE unlisted pixel formats: status 2 at [3]', f"not for {wrong}")
    entries = (ADDRESSING_LIGHT, ADDRESSING_PIN, ADDRESSING_ACTION, ADDRESSING_KIND)
    counts = [(addressing, count) for addressing in entries
              for count in (0, stage_capacity(addressing, PIXEL_RGB) + 1)
              if _status(device.request(CMD_STAGE, bytes([addressing, count]))) != (STATUS_INVALID, 4)]
    yield checks.check(not counts, 'STAGE entries: n of 0 or above the capacity, status 2 at [4]',
                       f"not for (addressing, n) {counts}")
    rejected = [mode for mode in unlisted
                if _status(device.request(CMD_UNSTAGE, bytes([mode, 1]))) != (STATUS_INVALID, 3)]
    yield checks.check(not rejected, 'UNSTAGE unlisted addressing: status 2 at [3]', f"not for {rejected}")
    wrong = []
    for addressing, at in ((ADDRESSING_LIGHT_RUN, 6), (ADDRESSING_LIGHT, 4), (ADDRESSING_PIN, 4),
                           (ADDRESSING_ACTION, 4), (ADDRESSING_KIND, 4)):
        capacity = stage_capacity(addressing, None)
        head = bytes([addressing]) + (struct.pack('<H', 0) if at == 6 else b'')
        full = device.request(CMD_UNSTAGE, head + bytes([capacity]))
        over = device.request(CMD_UNSTAGE, head + bytes([capacity + 1]))
        if full[2] != STATUS_OK or full[3] + full[4] != capacity or _status(over) != (STATUS_INVALID, at):
            wrong.append(addressing)
    yield checks.check(not wrong, 'UNSTAGE: runs 32, light and action 29, pin and kind 32 per report, one more '
                       'rejected', f"not for addressing {wrong}")


def _check_staging(device: HostLightingDevice, checks: _Checker, tables: dict,
                   claim: LightsClaim) -> Iterator[CheckResult]:
    checks.section('Staging')
    _configured(claim, Settings(brightness_applied=False))
    yield from _check_runs(device, checks, tables['summary'].led_limit, tables[PAGE_LIGHTS])
    yield from _check_entries(device, checks, tables[PAGE_CONTROLS], tables[PAGE_LIGHTS], tables['summary'].led_limit)
    yield from _check_stage_arguments(device, checks, tables['hello'])
    yield checks.check(device.request(CMD_FILL, bytes([PIXEL_RGBW << 4, 1, 2, 3, 4]))[2] == STATUS_OK, 'FILL RGBW')
    wrong = [pixel_format for pixel_format in _unlisted(tables['hello'].pixel_formats, ((0, 15),))
             if _status(device.request(CMD_FILL, bytes([pixel_format << 4, 1, 2, 3]))) != (STATUS_INVALID, 3)]
    yield checks.check(not wrong, 'FILL unlisted pixel formats: status 2 at [3]', f"not for {wrong}")
    yield checks.check(all(device.request(command)[2] == STATUS_OK for command in (CMD_CLEAR, CMD_COMMIT, CMD_RELEASE)),
                       'CLEAR, COMMIT, RELEASE')


def _check_features(device: HostLightingDevice, checks: _Checker, tables: dict) -> Iterator[CheckResult]:
    checks.section('Board features')
    summary, state = tables['summary'], tables['state']
    rejected = (STATUS_INVALID, 3)
    yield checks.check(device.request(CMD_SET_PROFILE, bytes([state.state.profile]))[2] == STATUS_OK,
                       'SET_PROFILE current: OK')
    yield checks.check(_status(device.request(CMD_SET_PROFILE, bytes([summary.profiles + 1]))) == rejected,
                       'SET_PROFILE past the last: status 2 at [3]')
    if summary.brightness_steps < 255:
        reply = device.request(CMD_SET_BRIGHTNESS, bytes([summary.brightness_steps + 1]))
        yield checks.check(_status(reply) == rejected, 'SET_BRIGHTNESS above the steps: status 2 at [3]')
    yield checks.check(_status(device.request(CMD_SET_ANIMATION, struct.pack('<H', summary.animations))) == rejected,
                       'SET_ANIMATION past the total: status 2 at [3]')
    if summary.animation_speed_steps < 254:     # 0xFF means unchanged
        speeds = bytes([summary.animation_speed_steps + 1, 0xFF, 0xFF])
        reply = device.request(CMD_SET_ANIMATION_SPEED, struct.pack('<H', 0) + speeds)
        # with no animations the index is refused first
        yield checks.check(_status(reply) == ((STATUS_INVALID, 5) if summary.animations else rejected),
                           'SET_ANIMATION_SPEED out of range: status 2')
    reply = device.request(CMD_SET_INPUT_MODE, bytes([INPUT_MODE_CONFIG]) + MAGIC_INPUT_MODE)
    yield checks.check(_status(reply) == rejected, 'SET_INPUT_MODE config mode: status 2 at [3]')
    # the current mode: were the guard broken, the board would save and reboot into the mode it is in
    reply = device.request(CMD_SET_INPUT_MODE, bytes([state.state.input_mode]) + MAGIC_INPUT_MODE[:2] + b'X' +
                           MAGIC_INPUT_MODE[3:])
    yield checks.check(_status(reply) == (STATUS_INVALID, 6), 'SET_INPUT_MODE wrong magic: status 2 at [6]')
    # a target far from the next free one: were it defined, the magic would reboot the board
    reply = device.request(CMD_REBOOT, bytes([0xFF]) + MAGIC_REBOOT)
    yield checks.check(_status(reply) == rejected, 'REBOOT target 0xFF: status 2 at [3]')
    yield from _check_lowest_offset(device, checks, tables)


def _check_lowest_offset(device: HostLightingDevice, checks: _Checker, tables: dict) -> Iterator[CheckResult]:
    """Check that a request with several invalid bytes is refused at the lowest one."""
    summary, mode = tables['summary'], tables['state'].state.input_mode
    probes = [(CMD_CLAIM, bytes([CLAIM_GIVE_UP + 1]) + bytes(4), 3),              # action and token
              (CMD_CONFIGURE, bytes([2, 0, 0, 2]), 3),                           # takeover and brightness
              # addressing and n
              (CMD_STAGE, bytes([_unlisted(tables['hello'].addressing_modes, ((0, 15),))[0], 0]), 3),
              (CMD_STAGE, bytes([ADDRESSING_LED_RUN]) + struct.pack('<HB', summary.led_limit, 0), 4),
              (CMD_SET_INPUT_MODE, bytes([INPUT_MODE_CONFIG]) + b'XXXX', 3),      # mode and magic
              # the current mode: were the guard broken, the board would reboot into the mode it is in
              (CMD_SET_INPUT_MODE, bytes([mode]) + MAGIC_INPUT_MODE[:1] + b'XX' + MAGIC_INPUT_MODE[3:], 5),
              (CMD_REBOOT, bytes([0xFF]) + b'XXXX', 3)]                           # target and magic
    if summary.animation_speed_steps < 254:     # 0xFF means unchanged
        speeds = bytes([summary.animation_speed_steps + 1] * 3)
        probes.append((CMD_SET_ANIMATION_SPEED, struct.pack('<H', summary.animations) + speeds, 3))
        if summary.animations:
            probes.append((CMD_SET_ANIMATION_SPEED, struct.pack('<H', 0) + speeds, 5))
    if summary.brightness_steps < 254:     # 0xFF is the saved step
        probes.append((CMD_SET_BRIGHTNESS, bytes([summary.brightness_steps + 1, 2]), 3))
    wrong = []
    for command, payload, at in probes:
        reply = device.request(command, payload)
        if _status(reply) != (STATUS_INVALID, at):
            wrong.append(f"0x{command:02X} {payload.hex()}: status {reply[2]} at [{reply[3]}]")
    yield checks.check(not wrong, 'several invalid bytes: status 2 at the lowest', ', '.join(wrong))


def _check_settings(device: HostLightingDevice, checks: _Checker, tables: dict,
                    claim: LightsClaim) -> Iterator[CheckResult]:
    checks.section('Settings (writes the settings flash)')
    summary, state, animations = tables['summary'], tables['state'], tables[PAGE_ANIMATIONS]
    if not summary.brightness_steps:
        yield checks.note('no LED output: nothing to change')
        return
    _events(device, 0.05)
    step = state.state.brightness_step_shown
    other = step - 1 if step > 0 else 1
    ok = device.request(CMD_SET_BRIGHTNESS, bytes([other, 1]))[2] == STATUS_OK
    state = _state(device)
    yield checks.check(ok and state.state.brightness_step_shown == other and not state.state.brightness_unsaved,
                       'a saved SET_BRIGHTNESS applies at once, not unsaved')
    _wait(claim, 0.1)
    yield checks.check(any(event.state.brightness_step_shown == other for event in _events(device, 0.05)),
                       'STATE_CHANGED for the brightness')
    device.request(CMD_SET_BRIGHTNESS, bytes([step, 1]))
    animation = ANIMATION_OFF if state.state.animation is None else state.state.animation
    device.request(CMD_SET_ANIMATION, struct.pack('<H', ANIMATION_OFF))
    yield checks.check(_state(device).state.animation is None, 'SET_ANIMATION off')
    device.request(CMD_SET_ANIMATION, struct.pack('<H', animation))
    yield checks.check(_state(device).state.animation == state.state.animation, 'SET_ANIMATION back')
    if animations:
        speeds = animations[0].speeds
        before = decode_page_header(device.get_page(PAGE_ANIMATIONS)).token
        counted = device.read_record(PAGE_SUMMARY, 0b10).records[0].changes.counters
        new = (speeds.idle + 1) % (summary.animation_speed_steps + 1)
        ok = device.request(CMD_SET_ANIMATION_SPEED, struct.pack('<H', 0) + bytes([new, 0xFF, 0xFF]))[2] == STATUS_OK
        reply = device.get_page(PAGE_ANIMATIONS)
        yield checks.check(ok and decode_records(reply)[0].speeds.idle == new and
                           decode_page_header(reply).token != before,
                           'SET_ANIMATION_SPEED: page 12 and the token change')
        animation_counter = PAGE_ANIMATIONS - PAGE_SUMMARY
        stepped = list(counted)
        stepped[animation_counter] = (counted[animation_counter] + 1) & 0xFF
        changes = device.read_record(PAGE_SUMMARY, 0b10).records[0].changes.counters
        yield checks.check(changes == tuple(stepped), "page 12's change counter steps, and no other",
                           f"{counted} to {changes}")
        device.request(CMD_SET_ANIMATION_SPEED,
                       struct.pack('<H', 0) + bytes([speeds.idle, speeds.pressed, speeds.case]))
        yield checks.check(decode_page_header(device.get_page(PAGE_ANIMATIONS)).token == before,
                           'speeds restored: the token returns')
        stepped[animation_counter] = (stepped[animation_counter] + 1) & 0xFF
        yield checks.check(device.read_record(PAGE_SUMMARY, 0b10).records[0].changes.counters == tuple(stepped),
                           'the counter steps again: it counts changes, the token follows the content')
    _wait(claim, 1.5)             # the board writes its settings flash shortly after a change


def _check_timing(device: HostLightingDevice, checks: _Checker, tables: dict, quick: bool,
                  claim: LightsClaim) -> Iterator[CheckResult]:
    checks.section('Timing')
    count = 50 if quick else 200
    trips = measure_round_trips(device, count)
    _keep(claim)
    yield checks.check(len(trips) == count, 'every HELLO answered', f"{len(trips)} of {count}")
    if trips:
        yield checks.note(f"round trip: median {statistics.median(trips):.2f} ms, min {min(trips):.2f}, "
                          f"max {max(trips):.2f}")
    leds = tables['summary'].led_extent or 1
    _configured(claim, Settings(brightness_applied=False))
    seconds = 1.0 if quick else 3.0
    frames = int(seconds * 60)
    acknowledged = measure_paced_frames(device, claim, leds, frames)
    yield checks.check(acknowledged == frames, '60 fps: every frame acknowledged', f"{acknowledged} of {frames}")
    yield checks.note(f"60 fps: {acknowledged} of {frames} frames acknowledged ({_reports(leds)} reports per frame)")
    yield checks.note(f"unthrottled: {measure_unthrottled_frames(device, claim, leds, seconds, True):.0f} frames/s "
                      "acknowledged")
    device.request_ok(CMD_CLEAR)
    device.request_ok(CMD_RELEASE)


def _check_delivery(device: HostLightingDevice, checks: _Checker, open_second) -> Iterator[CheckResult]:
    """Check that the operating system delivers each reply to every application with the interface open.

    The protocol's sharing rules rest on it. A second handle is opened on the board. Each handle must read
    the reply to the other's request.
    """
    checks.section('Delivery to every application')
    try:
        second = open_second()
    except (HostLightingError, OSError) as error:
        yield checks.note(f"not checked: a second handle could not be opened ({error})")
        return
    try:
        for sender, reader, what in ((device, second, "a second handle reads the reply to the first's request"),
                                     (second, device, "and the first the reply to the second's")):
            reader.collect(0.05)
            sequence = sender.send(CMD_HELLO)
            sender.wait_reply(CMD_HELLO, sequence)
            copies = [report for report in reader.collect(0.2)
                      if (report[0], report[1]) == (CMD_HELLO | REPLY_FLAG, sequence)]
            yield checks.check(len(copies) == 1, what, f"{len(copies)} copies")
    finally:
        second.close()


def run_checks(device: HostLightingDevice, claim: LightsClaim, settings: bool = False, quick: bool = False,
               open_second: Optional[Callable[[], HostLightingDevice]] = None) -> Iterator[CheckResult]:
    """Run every check under the claim, yielding each result as it is made.

    The claim's renewals go without KEEPALIVE while the checks run, so that the takeover and unsaved brightness
    checks time their own keepalive. An interface error, or the claim lost to another application, ends the run
    with a failed check. The claim is still held at the end, for the caller to give up.

    :param device: the opened device
    :param claim: a LightsClaim on the device that holds the claim
    :param settings: also change and restore brightness, animation and speeds, which writes the settings flash
    :param quick: shorter timing runs
    :param open_second: opens a second handle on the board, for the delivery checks
    """
    checks, keepalive, waits = _Checker(), claim.keepalive, claim.wait_after_takeover
    claim.keepalive, claim.wait_after_takeover = False, False
    try:
        yield from _run_checks(device, checks, claim, settings, quick, open_second)
    except HostLightingClaimedError as error:
        yield checks.check(False, 'the checks ran to the end', f"'{error.holder.application_name}' took the lights "
                           "over")
    except HostLightingError as error:
        yield checks.check(False, 'the checks ran to the end', str(error))
    finally:
        claim.keepalive, claim.wait_after_takeover = keepalive, waits


def _run_checks(device: HostLightingDevice, checks: _Checker, claim: LightsClaim, settings: bool, quick: bool,
                open_second) -> Iterator[CheckResult]:
    hello = yield from _check_hello(device, checks)
    yield from _check_framing(device, checks, hello)
    if open_second is not None:
        yield from _check_delivery(device, checks, open_second)
    _keep(claim)
    token = yield from _check_pages(device, checks, hello)
    _keep(claim)
    tables = yield from _check_tables(device, checks, token)
    tables['hello'] = hello
    _keep(claim)
    yield from _check_takeover(device, checks, tables, claim)
    yield from _check_claim(device, checks, claim)
    _keep(claim)
    yield from _check_exclusive(device, checks, tables, claim)
    _keep(claim)
    yield from _check_unsaved(device, checks, tables, claim)
    yield from _check_staging(device, checks, tables, claim)
    _keep(claim)
    yield from _check_features(device, checks, tables)
    if settings:
        _keep(claim)
        yield from _check_settings(device, checks, tables, claim)
    _keep(claim)
    yield from _check_timing(device, checks, tables, quick, claim)


# -- round trip and frames -------------------------------------------------------------

def _reports(leds: int) -> int:
    capacity = stage_capacity(ADDRESSING_LED_RUN, PIXEL_RGB)
    return (leds + capacity - 1) // capacity


def measure_round_trips(device: HostLightingDevice, count: int) -> list:
    """Time HELLOs one at a time.

    :param count: how many to send
    :return: each answered one's round trip in ms. A refusal or a missing reply is not counted
    """
    trips = []
    for _ in range(count):
        start = time.perf_counter()
        try:
            device.request_ok(CMD_HELLO)
        except HostLightingDisconnectedError:
            raise
        except HostLightingError:
            continue
        trips.append((time.perf_counter() - start) * 1000)
    return trips


def _frame(device: HostLightingDevice, claim: LightsClaim, leds: int, index: int, pipelined: bool) -> bool:
    """Send one whole frame of LED runs, and say whether it was acknowledged.

    Pipelined, only the last report is answered. Otherwise every report is. A status 4 refusal goes to the
    claim.
    """
    colours = [((index * 7) & 0xFF, 0x20, 0x40)] * leds
    try:
        if pipelined:
            device.stage_run(ADDRESSING_LED_RUN, 0, colours, commit=True)
            return True
        capacity = stage_capacity(ADDRESSING_LED_RUN, PIXEL_RGB)
        for first in range(0, leds, capacity):
            count = min(capacity, leds - first)
            payload = bytes([ADDRESSING_LED_RUN]) + struct.pack('<HB', first, count) + bytes(colours[0]) * count
            device.request_ok(CMD_STAGE, payload, FLAG_COMMIT_AFTER if first + count >= leds else 0)
        return True
    except HostLightingDisconnectedError:
        raise
    except HostLightingRejectedError as error:
        claim.refused(error)
        _held(claim)
        return False
    except HostLightingError:
        return False


def measure_paced_frames(device: HostLightingDevice, claim: LightsClaim, leds: int, frames: int,
                         rate: float = 60) -> int:
    """Send whole frames at a rate, maintaining the claim between them, and count those acknowledged.

    :param claim: the LightsClaim that holds the claim
    :param leds: LEDs per frame, from LED 0
    :param frames: how many to send
    :param rate: frames per second
    :raises HostLightingClaimedError: once the claim is lost
    """
    acknowledged, start = 0, time.perf_counter()
    for index in range(frames):
        while time.perf_counter() < start + index / rate:
            time.sleep(0.0005)
        acknowledged += _frame(device, claim, leds, index, True)
        _keep(claim)
    return acknowledged


def measure_unthrottled_frames(device: HostLightingDevice, claim: LightsClaim, leds: int, seconds: float,
                               pipelined: bool) -> float:
    """Send whole frames back to back for a while, maintaining the claim between them.

    :param claim: the LightsClaim that holds the claim
    :param leds: LEDs per frame, from LED 0
    :param seconds: how long to send
    :param pipelined: answer only each frame's last report
    :return: the acknowledged frames per second
    :raises HostLightingClaimedError: once the claim is lost
    """
    acknowledged, start = 0, time.perf_counter()
    while time.perf_counter() - start < seconds:
        acknowledged += _frame(device, claim, leds, acknowledged, pipelined)
        _keep(claim)
    return acknowledged / (time.perf_counter() - start)
