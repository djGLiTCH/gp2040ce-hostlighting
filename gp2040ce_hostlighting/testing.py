"""Fake GP2040-CE boards and build Host Lighting reports, to test host software without a board.

FakeBoard answers requests as the firmware does. ModelBoard also follows the
protocol's timing, claim and staging rules, on a clock the caller controls.
fake_device gives a HostLightingDevice connected to either, and attach()
puts fake boards on a fake bus, so discovery finds them. The record builders
make the pages' records, the replies and the events.

SPDX-FileCopyrightText: © 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import struct
import zlib
from typing import Any, Iterable, Optional, Sequence, Union

from gp2040ce_hostlighting import protocol as hl
# bound at import, so a test that replaces protocol.HostLightingDevice still gets real devices here
from gp2040ce_hostlighting.protocol import HostLightingDevice

__all__ = ['BOARD_ID', 'FakeBoard', 'ModelBoard', 'attach', 'control_record', 'fake_device', 'hello_reply',
           'identity_reply', 'light_record', 'other_event', 'page_reply', 'state_event', 'state_record',
           'summary_record']

BOARD_ID = bytes.fromhex('433030343237362E')


def page_reply(page: int, records: Sequence[Sequence[bytes]], start: int = 0, mask: int = 0, token: int = 0x1234ABCD,
               sequence: int = 1, status: int = hl.STATUS_OK, widths: tuple[int, ...] = ()) -> bytes:
    """Build a GET_PAGE reply as the firmware does: groups in bit order while one record fits in 48 bytes.

    widths, when given, replaces the page's 2.0 group widths, for a later 2.x board's groups.
    """
    widths = widths or hl.PAGE_GROUPS.get(page, ())                                # page 0 only when stale
    wanted = (mask or (1 << len(widths)) - 1) if status == hl.STATUS_OK else 0     # a stale reply echoes no groups
    groups = stride = 0
    for group, width in enumerate(widths):
        if wanted & (1 << group) and stride + width <= hl.RECORD_AREA:
            groups |= 1 << group
            stride += width
    total = len(records)
    count = 0 if status != hl.STATUS_OK or not stride else max(0, min(hl.RECORD_AREA // stride, total - start))
    body = b''.join(b''.join(record[group] for group in range(len(widths)) if groups & (1 << group))
                    for record in records[start:start + count])
    header = bytes([hl.CMD_GET_PAGE | hl.REPLY_FLAG, sequence, status, page])
    header += struct.pack('<HHBBH', start, total, count, stride, groups)
    return header + body.ljust(hl.RECORD_AREA, b'\x00') + struct.pack('<I', token)


def state_record(takeover_live: int = 0, input_mode: int = 0, profile: int = 1, player: int = 1,
                 animation: int = 0, brightness_step_shown: int = 5, takeover_number: int = 3,
                 holder_token: int = 0, application_name: bytes = b'', takeover: int = 0,
                 keepalive_ms: int = 2000, brightness_applied: int = 1, brightness_unsaved: int = 0,
                 exclusive: int = 0, saved_brightness_step: Optional[int] = None, claim_generation: int = 1,
                 animations_idle: int = 0) -> list[bytes]:
    """One page 1 record as its three groups, with the saved step the step shown unless given.

    Each keyword is the field of the same name in records.State, Controller or Settings.
    """
    saved = brightness_step_shown if saved_brightness_step is None else saved_brightness_step
    flags = takeover_live | brightness_unsaved << 1 | animations_idle << 2
    return [bytes([flags, input_mode, profile, player]) + struct.pack('<H', animation) +
            bytes([brightness_step_shown, takeover_number, saved]),
            struct.pack('<I', holder_token) + application_name.ljust(16, b'\x00') +
            bytes([exclusive, claim_generation]),
            struct.pack('<BHB', takeover, keepalive_ms, brightness_applied)]


def summary_record(led_extent: int = 19, led_limit: int = 100, render_hz: int = 40, colour_format: int = 0,
                   white_channel: int = 0, brightness_maximum: int = 255, brightness_steps: int = 10,
                   animation_speed_steps: int = 10, button_layout: int = 0, button_layout_right: int = 0,
                   button_layout_orientation: int = 0, grid_width: int = 9, grid_height: int = 6,
                   profiles: int = 3, controls: int = 12, lit_controls: int = 10, unlit_controls: int = 2,
                   lights: int = 16, animations: int = 3, counters: bytes = bytes(8)) -> list[bytes]:
    """One page 8 record as its two groups, the Summary and the configuration pages' change counters.

    Each keyword is the field of the same name in records.Summary or Changes.
    """
    return [struct.pack('<HHHBBBBBBBBHHHHHHHH', led_extent, led_limit, render_hz, colour_format, white_channel,
                        brightness_maximum, brightness_steps, animation_speed_steps, button_layout,
                        button_layout_right, button_layout_orientation, grid_width, grid_height, profiles, controls,
                        lit_controls, unlit_controls, lights, animations),
            bytes(counters)]


def control_record(key: int, action: int, duplicated: int = 0, lit: int = 1, instance: int = 1,
                   instances: int = 1, first_led: int = 0,
                   grid_rectangle: tuple[int, int, int, int] = (1, 0, 1, 0), first_light: int = 0,
                   light_count: int = 1) -> list[bytes]:
    """One page 10 record. Each keyword is the field of the same name in its group's record class."""
    flags = duplicated | (lit << 1)
    return [struct.pack('<BhB', key, action, flags), struct.pack('<HH', instance, instances),
            struct.pack('<H', first_led if lit else 0xFFFF) + (bytes(grid_rectangle) if lit else bytes(4)),
            struct.pack('<HH', first_light if lit else 0xFFFF, light_count)]


def light_record(first_led: int, led_count: int = 1, kind: int = 0, duplicated: int = 0, pin: int = 0xFF,
                 action: int = hl.ACTION_NONE, instance: int = 0, instances: int = 0, x: int = 0, y: int = 0,
                 slot: int = 0xFF) -> list[bytes]:
    """One page 11 record. Each keyword is the field of the same name in its group's record class."""
    return [struct.pack('<HH', first_led, led_count), bytes([kind, duplicated]), struct.pack('<Bh', pin, action),
            struct.pack('<HH', instance, instances), bytes([x, y]), bytes([slot])]


def identity_reply(strings: list[str], mask: int = 0xFF, sequence: int = 1, start: int = 0) -> bytes:
    """Build a page 0 reply: strings in bit order while they fit in the 48-byte area, and none past start 0."""
    area = b''
    groups = 0
    for group, text in enumerate(strings):
        if start or not mask & (1 << group):
            continue
        raw = text.encode()[:hl.IDENTITY_STRING_MAX]
        piece = raw + (b'\x00' if len(raw) < hl.IDENTITY_STRING_MAX else b'')
        if len(area) + len(piece) > hl.RECORD_AREA:
            continue
        area += piece
        groups |= 1 << group
    header = bytes([hl.CMD_GET_PAGE | hl.REPLY_FLAG, sequence, hl.STATUS_OK, hl.PAGE_IDENTITY])
    header += struct.pack('<HHBBH', start, 1, 1 if groups else 0, len(area), groups)
    return header + area[:hl.RECORD_AREA].ljust(hl.RECORD_AREA, b'\x00') + struct.pack('<I', 0x1234ABCD)


def hello_reply(sequence: int = 1, major: int = 2, board_id: bytes = BOARD_ID, events_enabled: int = 0,
                reply_queue_limit: int = 16) -> bytes:
    """Build a HELLO reply with this firmware's capabilities."""
    commands = sum(1 << c for c in (0x01, 0x02, 0x03, 0x04, 0x05, 0x10, 0x11, 0x12, 0x13, 0x20, 0x21,
                                    0x30, 0x31, 0x32, 0x33, 0x70, 0x71))
    pages = sum(1 << p for p in (0, 1, 2, 8, 9, 10, 11, 12))
    body = b'GPHL' + bytes([major, 0]) + board_id + bytes([reply_queue_limit, 0x7F]) + commands.to_bytes(16, 'little')
    body += struct.pack('<IHHHH', pages, 0xF3, 0x07, 0x01, events_enabled)
    return (bytes([hl.CMD_HELLO | hl.REPLY_FLAG, sequence, 0]) + body).ljust(hl.REPORT_SIZE, b'\x00')


# where the fakes read the Summary group's fields
_SUMMARY_LIMIT = 2
_SUMMARY_BRIGHTNESS_STEPS = 9
_SUMMARY_SPEED_STEPS = 10
_SUMMARY_CONTROLS = 20
_SUMMARY_LIT = 22
_SUMMARY_UNLIT = 24
_SUMMARY_LIGHTS = 26
# the configuration pages' change counters the fakes step: pages 10 and 11
_CHANGES_CONTROLS = 2
_CHANGES_LIGHTS = 3


def _pixel(data: bytes, pixel_format: int) -> tuple[int, ...]:
    """Decode one staged pixel as the colour it carries: RGB, RGBW, or RGB565 widened to 8 bits."""
    if pixel_format == hl.PIXEL_RGB565:
        value = struct.unpack_from('<H', data)[0]
        red, green, blue = value >> 11, (value >> 5) & 0x3F, value & 0x1F
        return (red << 3 | red >> 2, green << 2 | green >> 4, blue << 3 | blue >> 2)
    return tuple(data[:hl.PIXEL_SIZES[pixel_format]])


def _check_written(data: bytes) -> None:
    """Refuse a report not framed as hidapi writes it: report ID 0, then 64 bytes."""
    if len(data) != hl.REPORT_SIZE + 1 or data[0] != 0:
        raise ValueError(f"a report is written as report ID 0 then {hl.REPORT_SIZE} bytes, not {len(data)} bytes "
                         f"starting 0x{data[0] if data else 0:02X}")


class _Outbox(list[bytes]):
    """A fake board's reports waiting for its first handle.

    Each report queued is copied to every other open handle, as the operating system delivers each input
    report to every application with the interface open.
    """

    def __init__(self, board: 'FakeBoard') -> None:
        """Start empty, for one board."""
        super().__init__()
        self.board = board

    def append(self, report: bytes) -> None:
        """Queue one report."""
        super().append(report)
        self.deliver([report])

    def extend(self, reports: Iterable[bytes]) -> None:
        """Queue several reports."""
        reports = list(reports)
        super().extend(reports)
        self.deliver(reports)

    def deliver(self, reports: Iterable[bytes]) -> None:
        """Give the other open handles their copies."""
        for handle in self.board.handles:
            handle.inbox.extend(bytes(report) for report in reports)


class FakeBoard:
    """Answer HLP 2.0 requests the way the firmware does.

    Each request is answered and checked as the firmware does: the commands it lists, the arguments, the staging
    capacities and the addressable LED limit, page 0's start, CONFIGURE's settings on page 1, the takeover and its
    number, and what a change of holder resets. ModelBoard adds the timing, the reply queue limit, the claim
    generation's and the exclusive claim's refusals, events and the change counters.
    """

    COMMANDS = {hl.CMD_HELLO, hl.CMD_GET_PAGE, hl.CMD_CLAIM, hl.CMD_CONFIGURE, hl.CMD_SUBSCRIBE, hl.CMD_STAGE,
                hl.CMD_UNSTAGE, hl.CMD_FILL, hl.CMD_CLEAR, hl.CMD_COMMIT, hl.CMD_RELEASE, hl.CMD_SET_PROFILE,
                hl.CMD_SET_ANIMATION, hl.CMD_SET_ANIMATION_SPEED, hl.CMD_SET_BRIGHTNESS, hl.CMD_SET_INPUT_MODE,
                hl.CMD_REBOOT}

    def __init__(self) -> None:
        """Set up a small board: three controls (one an expander pin), a button light and a case strip."""
        self.state = state_record()
        self.summary = summary_record()
        self.controls = [control_record(2, 1), control_record(12, 13, lit=0),
                         control_record(hl.KEY_EXPANDER + 3, 5, lit=0, light_count=0)]
        self.lights = [light_record(0, pin=2, action=1, instance=1, instances=1),
                       light_record(1, 3, kind=1, slot=0)]
        self.modes = [[bytes([0, 0x03]), b'XINPUT'.ljust(22, b'\x00')],
                      [bytes([3, 0x01]), b'KEYBOARD'.ljust(22, b'\x00')],
                      [bytes([4, 0x00]), b'PS4'.ljust(22, b'\x00')]]
        self.profiles = [[bytes([1, 1]) + b'Base'.ljust(16, b'\x00')]]
        self.animations = [[bytes([1, 2, 1, 2]), bytes([5, 5, 5])]]
        self.identity = ['Haute42 COSMOX M Ultra', 'Haute42COSMOXMUltra', 'rp2040', 'v0.7.12-88-ga83f820',
                         '0.7.12', 'a83f820', 'Release', 'GP2040-CE_0.7.12_Haute42COSMOXMUltra']
        self.token = 0x1234ABCD
        self.requests: list[bytes] = []
        self.outbox = _Outbox(self)
        # other applications' handles, beside the first
        self.handles: list[_SecondHandle] = []
        self.opened = False
        self.stale_once_at = None       # a GET_PAGE start at which the token changes once
        # commands, or (command, [3]) pairs, at which the board drops off the bus
        self.disconnect_on: set[Union[int, tuple[int, int]]] = set()
        self.departed = False           # the board has left the bus, so a write fails and a read raises
        self.holder_token = 0
        self.holder_name = bytes(16)
        self.holder_flags = 0
        self.generation = 1             # steps 1 to 7 and back to 1 at every change of holder
        self.saved_step = self.state[0][6]
        self.subscribed = 0
        self.queue_limit = 16
        # command -> reports that arrive between it and its reply, and reports queued after its reply (events)
        self.before: dict[int, list[bytes]] = {}
        self.after: dict[int, list[bytes]] = {}
        # page -> a group a later 2.x board adds, the same bytes in every record
        self.later_groups: dict[int, bytes] = {}
        self.board_id = BOARD_ID
        # LED index -> colour, the staged frame, and every frame published
        self.staged: dict[int, tuple[int, ...]] = {}
        self.frames: list[dict[int, tuple[int, ...]]] = []

    def write(self, data: bytes) -> int:
        """Take one report as hidapi writes it (report ID 0 first) and queue the reply."""
        _check_written(data)
        if self.departed:
            return -1                   # as hidapi reports a write the device did not take
        request = bytes(data[1:])
        self.requests.append(request)
        command, sequence, flags = request[0], request[1], request[2]
        if command in self.disconnect_on or (command, request[3]) in self.disconnect_on:
            raise OSError("device disconnected")
        reply = self.answer(command, sequence, request)
        publishes = command == hl.CMD_COMMIT or flags & hl.FLAG_COMMIT_AFTER
        if reply is not None and reply[2] == hl.STATUS_OK and publishes:
            self._record_frame()
            self._start_takeover()
        self.outbox.extend(self.before.pop(command, []))
        if reply is not None and not flags & hl.FLAG_NO_REPLY:
            self.outbox.append(reply)
        self.outbox.extend(self.after.pop(command, []))
        return len(data)

    def answer(self, command: int, sequence: int, request: bytes) -> Optional[bytes]:
        """Build the reply to one request as the firmware does, or None when the request gets none."""
        if command == 0 or command & hl.REPLY_FLAG:
            return None
        ok = bytes([command | hl.REPLY_FLAG, sequence, hl.STATUS_OK])
        if command not in self.COMMANDS:
            return ok[:2] + bytes([hl.STATUS_UNSUPPORTED]) + bytes(61)
        if command == hl.CMD_HELLO:
            return hello_reply(sequence, board_id=self.board_id, events_enabled=self.subscribed,
                               reply_queue_limit=self.queue_limit)
        if command == hl.CMD_GET_PAGE:
            return self._page(sequence, request)
        handlers = {hl.CMD_CLAIM: self._claim, hl.CMD_CONFIGURE: self._configure, hl.CMD_SUBSCRIBE: self._subscribe,
                    hl.CMD_STAGE: self._stage, hl.CMD_UNSTAGE: self._unstage, hl.CMD_FILL: self._fill,
                    hl.CMD_CLEAR: self._lifecycle, hl.CMD_COMMIT: self._lifecycle, hl.CMD_RELEASE: self._lifecycle,
                    hl.CMD_SET_INPUT_MODE: self._guarded, hl.CMD_REBOOT: self._guarded}
        if command in handlers:
            return handlers[command](ok, request)
        return self._feature(ok, command, request)

    @staticmethod
    def _wrong_magic(request: bytes, magic: bytes) -> int:
        """Return the offset of the first magic byte ([4..7]) that differs from the magic, or 0."""
        return next((4 + index for index, byte in enumerate(magic) if request[4 + index] != byte), 0)

    @staticmethod
    def _invalid(ok: bytes, offset: int) -> bytes:
        """Build a status 2 reply naming the request byte at fault."""
        return ok[:2] + bytes([hl.STATUS_INVALID, offset]) + bytes(60)

    def _configure(self, ok: bytes, request: bytes) -> bytes:
        """Set the takeover mode, the timeout (0 = 2000, clamped to 100-10000) and brightness scaling, on page 1."""
        overlay, timeout, brightness = request[3], struct.unpack_from('<H', request, 4)[0], request[6]
        if overlay > 1:
            return self._invalid(ok, 3)
        if brightness > 1:
            return self._invalid(ok, 6)
        settings = struct.pack('<BHB', overlay, 2000 if timeout == 0 else min(max(timeout, 100), 10000), brightness)
        self.state = self.state[:2] + [settings]
        return (ok + settings).ljust(hl.REPORT_SIZE, b'\x00')

    def _subscribe(self, ok: bytes, request: bytes) -> bytes:
        """Add the events asked for that the board has."""
        return self._feature(ok, hl.CMD_SUBSCRIBE, request)

    def _lifecycle(self, ok: bytes, request: bytes) -> bytes:
        """CLEAR unstages every pixel and RELEASE ends the takeover. COMMIT publishes, which write() records."""
        if request[0] == hl.CMD_RELEASE:
            self._end_takeover()
        elif request[0] == hl.CMD_CLEAR:
            self.staged.clear()
        return ok.ljust(hl.REPORT_SIZE, b'\x00')

    def _fill(self, ok: bytes, request: bytes) -> bytes:
        """Apply FILL with a known pixel format in bits 4-7. Bits 0-3 are ignored."""
        if request[3] >> 4 not in hl.PIXEL_SIZES:
            return self._invalid(ok, 3)
        self._fill_staged(request)
        return ok.ljust(hl.REPORT_SIZE, b'\x00')

    def _guarded(self, ok: bytes, request: bytes) -> bytes:
        """SET_INPUT_MODE and REBOOT: a listed mode or a known target, then the magic, byte by byte."""
        if request[0] == hl.CMD_SET_INPUT_MODE:
            valid, magic = request[3] != 255 and request[3] in [mode[0][0] for mode in self.modes], hl.MAGIC_INPUT_MODE
        else:
            valid, magic = request[3] in (hl.REBOOT_GAMEPAD, hl.REBOOT_WEBCONFIG, hl.REBOOT_BOOTSEL), hl.MAGIC_REBOOT
        if not valid:
            return self._invalid(ok, 3)
        wrong = self._wrong_magic(request, magic)
        return self._invalid(ok, wrong) if wrong else ok.ljust(hl.REPORT_SIZE, b'\x00')

    def _start_takeover(self) -> None:
        """Show a takeover live from its first publish, with the next takeover number."""
        if not self.state[0][0] & hl.STATE_LIVE:
            self.set_state(0, bytes([self.state[0][0] | hl.STATE_LIVE]))
            self.set_state(7, bytes([(self.state[0][7] + 1) & 0xFF]))

    def _end_takeover(self) -> None:
        """End the takeover, which clears the staged frame as CLEAR does."""
        self.staged.clear()
        self.set_state(0, bytes([self.state[0][0] & ~hl.STATE_LIVE]))

    def _change_holder(self) -> None:
        """Do what every change of holder does.

        It ends the takeover and an unsaved step, returns the CONFIGURE settings to their defaults and steps the
        claim generation.
        """
        self._end_takeover()
        self._show_step(self.saved_step)
        self.state = self.state[:2] + [struct.pack('<BHB', hl.TAKEOVER_WHOLE_FRAME, 2000, 1)]
        self.generation = self.generation % hl.GENERATION_MAX + 1

    def _claim(self, ok: bytes, request: bytes) -> bytes:
        """Answer CLAIM as the firmware keeps the holder."""
        action, token = request[3], struct.unpack_from('<I', request, 4)[0]
        if action > hl.CLAIM_GIVE_UP or token == 0:
            return self._invalid(ok, 3 if action > hl.CLAIM_GIVE_UP else 4)
        given_up = action == hl.CLAIM_GIVE_UP and token == self.holder_token
        taken = action != hl.CLAIM_GIVE_UP and token != self.holder_token and (
            action == hl.CLAIM_TAKE_OVER or not self.holder_token)
        if given_up or taken:
            self._change_holder()
        if action == hl.CLAIM_GIVE_UP:
            if token == self.holder_token:
                self.holder_token, self.holder_name, self.holder_flags = 0, bytes(16), 0
        elif action == hl.CLAIM_TAKE_OVER or self.holder_token in (0, token):
            self.holder_token, self.holder_name = token, request[8:24]
            self.holder_flags = request[24] & hl.CLAIM_EXCLUSIVE
        return (ok + request[3:8] + self._controller()).ljust(hl.REPORT_SIZE, b'\x00')

    def _controller(self) -> bytes:
        """Page 1's Controller group: the holder's token, name and claim flags, and the claim generation."""
        return struct.pack('<I', self.holder_token) + self.holder_name + bytes([self.holder_flags, self.generation])

    def _feature(self, ok: bytes, command: int, request: bytes) -> bytes:
        """Answer SUBSCRIBE and the board-feature commands as the firmware checks them."""
        if command == hl.CMD_SUBSCRIBE:
            self.subscribed |= struct.unpack_from('<H', request, 3)[0] & (1 << hl.EVENT_STATE_CHANGED)
            return (ok + struct.pack('<H', self.subscribed)).ljust(hl.REPORT_SIZE, b'\x00')
        setters = {hl.CMD_SET_PROFILE: self._set_profile, hl.CMD_SET_ANIMATION: self._set_animation,
                   hl.CMD_SET_ANIMATION_SPEED: self._set_speed, hl.CMD_SET_BRIGHTNESS: self._set_brightness}
        offset = setters[command](request)
        if offset:
            return ok[:2] + bytes([hl.STATUS_INVALID, offset]) + bytes(60)
        return ok.ljust(hl.REPORT_SIZE, b'\x00')

    def set_state(self, offset: int, value: bytes) -> None:
        """Change page 1's State group from offset."""
        state = bytearray(self.state[0])
        state[offset:offset + len(value)] = value
        self.state = [bytes(state)] + self.state[1:]

    def _set_profile(self, request: bytes) -> int:
        """Apply SET_PROFILE, and return the byte at fault, or 0."""
        if request[3] not in [record[0][0] for record in self.profiles if record[0][1] & 1]:
            return 3
        self.set_state(2, request[3:4])
        return 0

    def _set_animation(self, request: bytes) -> int:
        """Apply SET_ANIMATION, and return the byte at fault, or 0. A change to an animation wakes the animations."""
        index = struct.unpack_from('<H', request, 3)[0]
        if index != hl.ANIMATION_OFF and (index >= len(self.animations) or not self.animations[index][0][0] & 1):
            return 3
        changed = index != struct.unpack_from('<H', self.state[0], 4)[0]
        self.set_state(4, request[3:5])
        if changed and index != hl.ANIMATION_OFF:
            self.set_state(0, bytes([self.state[0][0] & ~hl.STATE_IDLE]))
        return 0

    def _set_speed(self, request: bytes) -> int:
        """Apply SET_ANIMATION_SPEED up to the Summary's speed steps, and return the byte at fault, or 0."""
        index = struct.unpack_from('<H', request, 3)[0]
        if index >= len(self.animations):
            return 3
        new = request[5:8]
        for place, speed in enumerate(new):
            if speed != hl.SPEED_UNCHANGED and speed > self.summary[0][_SUMMARY_SPEED_STEPS]:
                return 5 + place
        old = self.animations[index][1]
        speeds = bytes(was if speed == hl.SPEED_UNCHANGED else speed for was, speed in zip(old, new))
        self.animations[index] = [self.animations[index][0], speeds]
        return 0

    def _set_brightness(self, request: bytes) -> int:
        """Apply SET_BRIGHTNESS, and return the byte at fault, or 0.

        A step up to the Summary's steps is saved with [4] = 1 and otherwise only shown. 0xFF shows the saved step.
        """
        step = request[3]
        if step != hl.BRIGHTNESS_SAVED and step > self.summary[0][_SUMMARY_BRIGHTNESS_STEPS]:
            return 3
        if request[4] > 1:
            return 4
        if request[4] and step != hl.BRIGHTNESS_SAVED:
            self.saved_step = step
        self._show_step(self.saved_step if step == hl.BRIGHTNESS_SAVED else step)
        return 0

    def _show_step(self, step: int) -> None:
        """Show a brightness step in page 1, flagged unsaved while it differs from the saved step."""
        self.set_state(6, bytes([step]))
        self.set_state(8, bytes([self.saved_step]))
        self.set_state(0, bytes([self.state[0][0] & ~0x02 | (0x02 if step != self.saved_step else 0)]))

    def _stage(self, ok: bytes, request: bytes) -> bytes:
        """Answer STAGE with each run member's or entry's outcome, staging the LEDs each reaches."""
        addressing, pixel_format, count = request[3] & 0x0F, request[3] >> 4, request[4]
        if pixel_format not in hl.PIXEL_SIZES:
            return self._invalid(ok, 3)
        if addressing in (hl.ADDRESSING_LED_RUN, hl.ADDRESSING_LIGHT_RUN):
            return self._run(ok, request, addressing)
        if addressing not in hl.ADDRESS_SIZES:
            return self._invalid(ok, 3)
        if not 0 < count <= hl.stage_capacity(addressing, pixel_format):
            return self._invalid(ok, 4)
        address_size = hl.ADDRESS_SIZES[addressing]
        size = address_size + hl.PIXEL_SIZES[pixel_format]
        applied, mask = 0, 0
        for index in range(count):
            entry = request[5 + index * size:5 + (index + 1) * size]
            leds = self._leds_of(addressing, entry)
            if leds:
                applied += 1
                mask |= 1 << index
                colour = _pixel(entry[address_size:], pixel_format)
                self.staged.update((led, colour) for led in leds)
        return (ok + bytes([applied, count - applied]) + struct.pack('<I', mask)).ljust(hl.REPORT_SIZE, b'\x00')

    def _unstage(self, ok: bytes, request: bytes) -> bytes:
        """Answer UNSTAGE with each run member's or entry's outcome, unstaging the LEDs each reaches."""
        addressing = request[3] & 0x0F
        if addressing in (hl.ADDRESSING_LED_RUN, hl.ADDRESSING_LIGHT_RUN):
            return self._run(ok, request, addressing)
        if addressing not in hl.ADDRESS_SIZES:
            return self._invalid(ok, 3)
        count, size = request[4], hl.ADDRESS_SIZES[addressing]
        if not 0 < count <= hl.stage_capacity(addressing, None):
            return self._invalid(ok, 4)
        reached = [self._leds_of(addressing, request[5 + i * size:5 + (i + 1) * size]) for i in range(count)]
        for leds in reached:
            for led in leds:
                self.staged.pop(led, None)
        return self._outcome(ok, [bool(leds) for leds in reached])

    def _run(self, ok: bytes, request: bytes, addressing: int) -> bytes:
        """Stage or unstage a run of LEDs or lights, within its capacity and, for LEDs, the addressable LED limit."""
        capacity = hl.stage_capacity(addressing, request[3] >> 4 if request[0] == hl.CMD_STAGE else None)
        first, count = struct.unpack_from('<H', request, 4)[0], request[6]
        led_run, limit = addressing == hl.ADDRESSING_LED_RUN, self.led_limit()
        if led_run and first >= limit:
            return self._invalid(ok, 4)
        if not 0 < count <= capacity or (led_run and first + count > limit):
            return self._invalid(ok, 6)
        members = [[first + i] if led_run else self._light_leds(first + i) for i in range(count)]
        if request[0] == hl.CMD_STAGE:
            pixel_format = request[3] >> 4
            size = hl.PIXEL_SIZES[pixel_format]
            for index, leds in enumerate(members):
                colour = _pixel(request[7 + index * size:7 + (index + 1) * size], pixel_format)
                self.staged.update((led, colour) for led in leds)
        else:
            for leds in members:
                for led in leds:
                    self.staged.pop(led, None)
        return self._outcome(ok, [bool(leds) for leds in members])

    @staticmethod
    def _outcome(ok: bytes, applied: list[bool]) -> bytes:
        """Build a STAGE or UNSTAGE reply: the entries applied and skipped, and the outcome mask."""
        mask = sum(1 << i for i, done in enumerate(applied) if done)
        return (ok + bytes([sum(applied), len(applied) - sum(applied)]) + struct.pack('<I', mask)).ljust(
            hl.REPORT_SIZE, b'\x00')

    def _fill_staged(self, request: bytes) -> None:
        """Stage FILL's pixel on every LED below the addressable LED limit."""
        if request[3] >> 4 in hl.PIXEL_SIZES:
            colour = _pixel(request[4:], request[3] >> 4)
            self.staged.update((led, colour) for led in range(self.led_limit()))

    def _record_frame(self) -> None:
        """Publish the staged frame, as COMMIT and COMMIT_AFTER do."""
        self.frames.append(dict(self.staged))

    def _page(self, sequence: int, request: bytes) -> bytes:
        """Answer GET_PAGE, with the expected-token check."""
        page, start, token, mask = struct.unpack_from('<BHIH', request, 3)
        if self.stale_once_at is not None and start == self.stale_once_at:
            self.stale_once_at = None
            self.token += 1
        if token and token != self.token:
            return page_reply(page, [], start, mask, self.token, sequence, hl.STATUS_STALE_TOKEN)
        if page == hl.PAGE_IDENTITY:
            return identity_reply(self.identity, mask or 0xFF, sequence, start)
        state = [self.state[0], self._controller()] + self.state[2:]
        tables = {hl.PAGE_STATE: [state], hl.PAGE_INPUT_MODES: self.modes, hl.PAGE_SUMMARY: [self.summary],
                  hl.PAGE_PROFILES: self.profiles, hl.PAGE_CONTROLS: self.controls, hl.PAGE_LIGHTS: self.lights,
                  hl.PAGE_ANIMATIONS: self.animations}
        if page not in tables:
            return bytes([hl.CMD_GET_PAGE | hl.REPLY_FLAG, sequence, hl.STATUS_INVALID, 3]) + bytes(60)
        records, widths = tables[page], hl.PAGE_GROUPS[page]
        if page in self.later_groups:
            later = self.later_groups[page]
            records, widths = [list(record) + [later] for record in records], widths + (len(later),)
        return page_reply(page, records, start, mask, self.token, sequence, widths=widths)

    def led_limit(self) -> int:
        """Give the Summary's addressable LED limit."""
        limit: int = struct.unpack_from('<H', self.summary[0], _SUMMARY_LIMIT)[0]
        return limit

    def _light_leds(self, ordinal: int) -> list[int]:
        """List one light's LEDs below the addressable LED limit, none for a light that does not exist."""
        if ordinal >= len(self.lights):
            return []
        first, count = struct.unpack_from('<HH', self.lights[ordinal][0])
        return [led for led in range(first, first + count) if led < self.led_limit()]

    def _leds_of(self, addressing: int, entry: bytes) -> list[int]:
        """List the LEDs a STAGE or UNSTAGE entry's address reaches, none when no light with LEDs is behind it."""
        if addressing == hl.ADDRESSING_LIGHT:
            return self._light_leds(struct.unpack_from('<H', entry)[0])
        records = [hl.decode_records(page_reply(hl.PAGE_LIGHTS, [light]))[0] for light in self.lights]
        if addressing == hl.ADDRESSING_PIN:
            matches = [record.owner.pin == entry[0] for record in records]
        elif addressing == hl.ADDRESSING_ACTION:
            matches = [record.owner.action == struct.unpack_from('<h', entry)[0] for record in records]
        else:
            matches = [record.kind.value == entry[0] for record in records]
        return [led for ordinal, match in enumerate(matches) if match for led in self._light_leds(ordinal)]

    # -- what another application, the user or the bus does --------------------

    def take_over(self, token: int = 0xB2, name: bytes = b'Other', exclusive: bool = False) -> None:
        """Have another application take the claim over, a change of holder."""
        if token != self.holder_token:
            self._change_holder()
        self.holder_token, self.holder_name = token, name.ljust(16, b'\x00')
        self.holder_flags = hl.CLAIM_EXCLUSIVE if exclusive else 0

    def let_go(self) -> None:
        """Have the holder give the claim up, a change of holder."""
        if self.holder_token:
            self._change_holder()
        self.holder_token, self.holder_name, self.holder_flags = 0, bytes(16), 0

    def idle(self, idle: bool = True) -> None:
        """Have the LED idle timeout turn the animations off, or a button press wake them."""
        self.set_state(0, bytes([self.state[0][0] & ~hl.STATE_IDLE | (hl.STATE_IDLE if idle else 0)]))

    def reconfigure(self, lights: Optional[list[list[bytes]]] = None,
                    controls: Optional[list[list[bytes]]] = None) -> None:
        """Change the Lights or Controls page, as a profile switch or a change of add-on input does.

        The Summary's totals follow, the state token changes and the changed pages' counters step.
        """
        changes = bytearray(self.summary[1])
        summary = bytearray(self.summary[0])
        if lights is not None:
            self.lights = list(lights)
            struct.pack_into('<H', summary, _SUMMARY_LIGHTS, len(self.lights))
            changes[_CHANGES_LIGHTS] = (changes[_CHANGES_LIGHTS] + 1) & 0xFF
        if controls is not None:
            self.controls = list(controls)
            lit = sum(1 for control in self.controls if control[0][3] & 0x02)
            struct.pack_into('<HHH', summary, _SUMMARY_CONTROLS, len(self.controls), lit, len(self.controls) - lit)
            changes[_CHANGES_CONTROLS] = (changes[_CHANGES_CONTROLS] + 1) & 0xFF
        self.summary = [bytes(summary), bytes(changes)]
        self.token = (self.token + 1) & 0xFFFFFFFF or 1
        if self.subscribed & (1 << hl.EVENT_STATE_CHANGED):
            self.outbox.append(state_event(self.token))

    def unplug(self) -> None:
        """Take the board off the bus, so that a write fails and a read raises."""
        self.departed = True

    def replug(self) -> None:
        """Put the board back on the bus after a power cycle: a new USB session, unclaimed and unsubscribed.

        The configuration and its token are the same. The change counters start again at 0. The next
        fake_device() opens it again.
        """
        self.departed = False
        self.opened = False
        self.handles.clear()
        self.outbox.clear()
        self.staged.clear()
        self.subscribed = 0
        self.holder_token, self.holder_name, self.holder_flags = 0, bytes(16), 0
        self.generation = 1
        self.summary = [self.summary[0], bytes(8)]

    def _serve(self) -> None:
        """Send what waits on the board to every open handle. FakeBoard keeps nothing waiting."""

    def read(self, size: int, timeout_ms: int = 0) -> list[int]:
        """Give hidapi's next input report, or nothing."""
        if self.departed:
            raise OSError("read error")
        return list(self.outbox.pop(0)) if self.outbox else []

    def close(self) -> None:
        """Close, as hidapi does."""
        self.opened = False

    def sent(self, command: int) -> list[bytes]:
        """Every request with this command."""
        return [request for request in self.requests if request[0] == command]


def state_event(token: int = 0x1234ABCD, **fields: Any) -> bytes:
    """Build a STATE_CHANGED event, [3..63] of a page 1 reply with mask 0, under a state token.

    The other keywords are state_record's.
    """
    reply = page_reply(hl.PAGE_STATE, [state_record(**fields)], token=token)
    return bytes([hl.EVENT_MARKER, 0, 0]) + reply[3:]


def other_event() -> bytes:
    """Build an event other than STATE_CHANGED (event 1)."""
    event = bytearray(state_event(profile=9))
    event[1] = 1
    return bytes(event)


class _SecondHandle:
    """Another application's handle on a fake board that is already open.

    It writes to the board and reads its own copy of every report.
    """

    def __init__(self, board: FakeBoard):
        """Open a handle beside the board's first."""
        self.board = board
        self.inbox: list[bytes] = []
        board.handles.append(self)

    def write(self, data: bytes) -> int:
        """Send one report to the board."""
        return self.board.write(data)

    def read(self, size: int, timeout_ms: int = 0) -> list[int]:
        """Give this handle's next report, or with none, wait the timeout on the board's clock when it has one."""
        self.board._serve()
        if self.inbox:
            return list(self.inbox.pop(0))
        if timeout_ms and hasattr(self.board, 'sleep'):
            self.board.sleep(timeout_ms / 1000)
        return []

    def close(self) -> None:
        """Close this handle."""
        if self in self.board.handles:          # a replug() closed every handle already
            self.board.handles.remove(self)


def fake_device(board: FakeBoard) -> hl.HostLightingDevice:
    """Connect a HostLightingDevice to a FakeBoard instead of hidapi, or to a second handle while the board is open.

    Its sequence numbers start at 1, so replies can be built with known sequences.
    """
    device = HostLightingDevice(handle=_SecondHandle(board) if board.opened else board)
    board.opened = True
    device.sequence = 0
    return device


class ModelBoard(FakeBoard):
    """A fake board that follows the protocol's rules, on a clock the caller controls.

    It models what gp2040ce_hostlighting.conformance checks: unknown and unanswerable commands, the reply queue
    limit (a request that needs a reply while the queue is full is discarded), the takeover and its number,
    publishing, the keepalive and the claim lease (both CONFIGURE's timeout), the claim generation and the refusal
    of an outdated one, the CONFIGURE defaults at every change of holder, the exclusive claim's refusals, the
    unsaved brightness step and its keepalive, events sent only while no reply waits, run and entry staging with the
    capacity and limit rules, FILL, UNSTAGE, the magic-guarded commands, and a state token and change counters that
    follow the configuration pages. Each report written takes 1 ms. A read with nothing to read waits its timeout.
    Reports wait on the board until a handle reads, where a host's operating system takes them as they come.
    """

    def __init__(self) -> None:
        """Set up a small, self-consistent board.

        It has three controls, one lit, with a Hall-effect trigger that shares the lit control's action, a button
        light and a case strip.
        """
        super().__init__()
        self.controls = [control_record(2, 1, duplicated=1, instances=2, grid_rectangle=(1, 0, 1, 0), first_led=0,
                                        first_light=0, light_count=1),
                         control_record(12, 13, lit=0, light_count=0),
                         control_record(hl.KEY_TRIGGER, 1, duplicated=1, lit=0, instance=2, instances=2, light_count=0)]
        self.lights = [light_record(0, pin=2, action=1, duplicated=1, instance=1, instances=2, x=1, y=0),
                       light_record(1, 3, kind=1, slot=0, x=0, y=1)]
        self.summary = summary_record(led_extent=4, grid_width=2, grid_height=2, profiles=1, controls=3, lit_controls=1,
                                      unlit_controls=2, lights=2, animations=1)
        self.now = 0.0
        self.live = False
        self.number = 3
        self.overlay, self.keepalive_ms, self.brightness = 0, 2000, 1
        self.refreshed = self.leased = self.unsaved_refreshed = 0.0
        self.pending = False
        self.queue: list[bytes] = []    # the reports waiting on the board until a handle reads
        self.steal_at = None            # a time at which another application takes the claim over
        self.queue_limit = 16
        self._hashes: list[int] = []         # each configuration page as last hashed, for the change counters
        self._refresh()
        self._seen = (tuple(self.state), self.token)

    # -- the clock ---------------------------------------------------------------

    def time(self) -> float:
        """Read the board's clock, for time.monotonic and time.perf_counter."""
        return self.now

    def sleep(self, seconds: float) -> None:
        """Let time pass, for time.sleep."""
        self.now += max(seconds, 0.0)
        self._tick()

    # -- hidapi ------------------------------------------------------------------

    def write(self, data: bytes) -> int:
        """Take one report, answer it and apply its flags when it succeeds."""
        _check_written(data)
        if self.departed:
            return -1                   # as hidapi reports a write the device did not take
        request = bytes(data[1:])
        self.requests.append(request)
        command, sequence, flags = request[0], request[1], request[2]
        if command in self.disconnect_on or (command, request[3]) in self.disconnect_on:
            raise OSError("device disconnected")
        self.now += 0.001
        self._tick()
        queued = [report for report in self.queue if report[0] != hl.EVENT_MARKER]
        if not flags & hl.FLAG_NO_REPLY and len(queued) >= self.queue_limit:
            return len(data)            # discarded, neither executed nor answered
        generation = (flags & hl.FLAG_GENERATION) >> hl.FLAG_GENERATION_SHIFT
        # a request other than CLAIM under a generation that is no longer current, since the holder has changed
        outdated = (command in self.COMMANDS and command != hl.CMD_CLAIM and
                    generation not in (0, self.generation))
        guarded = self.holder_token and self.holder_flags & hl.CLAIM_EXCLUSIVE and not (
            flags & hl.FLAG_HOLDER and generation == self.generation)
        reply: Optional[bytes]
        if outdated or (guarded and self._refused(command, flags)):
            reply = self._claimed(command, sequence)
        else:
            reply = self.answer(command, sequence, request)
        acts = self._flags_act(command, request, bool(guarded))
        if reply is not None and reply[2] == hl.STATUS_OK and acts:
            self._apply_flags(command, flags)
        self.queue.extend(self.before.pop(command, []))
        if reply is not None and not flags & hl.FLAG_NO_REPLY:
            self.queue.append(reply)
        self._note()
        self.queue.extend(self.after.pop(command, []))
        return len(data)

    def _serve(self) -> None:
        """Send the reports waiting, then a pending event, to every open handle, as a read of any handle has USB do.

        An event goes only once no reply waits, as the firmware sends it.
        """
        self._tick()
        if self.queue:
            self.outbox.extend(self.queue)
            self.queue.clear()
        if self.pending:
            self.pending = False
            self.outbox.append(self._event())

    def read(self, size: int, timeout_ms: int = 0) -> list[int]:
        """Give the next report, after the board sends what waits, else wait the timeout (none when non-blocking)."""
        if self.departed:
            raise OSError("read error")
        self._serve()
        if self.outbox:
            return list(self.outbox.pop(0))
        if timeout_ms:
            self.now += timeout_ms / 1000
            self._tick()
        return []

    # -- rules -------------------------------------------------------------------

    def _refused(self, command: int, flags: int) -> bool:
        """Say whether an exclusive claim refuses a request without HOLDER and the current generation.

        It refuses every command but HELLO, GET_PAGE, CLAIM and SUBSCRIBE, and those with COMMIT_AFTER or
        KEEPALIVE but CLAIM, which is never refused. An unknown command is status 1.
        """
        if command not in self.COMMANDS or command == hl.CMD_CLAIM:
            return False
        reads = (hl.CMD_HELLO, hl.CMD_GET_PAGE, hl.CMD_SUBSCRIBE)
        return command not in reads or bool(flags & (hl.FLAG_COMMIT_AFTER | hl.FLAG_KEEPALIVE))

    def _claimed(self, command: int, sequence: int) -> bytes:
        """Build a status 4 reply: the holder at [3..24], as page 1's Controller group."""
        return (bytes([command | hl.REPLY_FLAG, sequence, hl.STATUS_CLAIMED]) + self._controller()).ljust(
            hl.REPORT_SIZE, b'\x00')

    def _flags_act(self, command: int, request: bytes, guarded: bool) -> bool:
        """Say whether a request's COMMIT_AFTER and KEEPALIVE act.

        Under an exclusive claim, a request without HOLDER and the current generation has them act only when
        it is a CLAIM that leaves the sender holding the claim.
        """
        return not guarded or (command == hl.CMD_CLAIM and self.holder_token == struct.unpack_from('<I', request, 4)[0])

    def _page(self, sequence: int, request: bytes) -> bytes:
        """Answer GET_PAGE from the pages as they stand now."""
        self._refresh()
        return super()._page(sequence, request)

    def _claim(self, ok: bytes, request: bytes) -> bytes:
        """Claim, take over or give up. Each claim or take over by the holder renews the lease."""
        action, token = request[3], struct.unpack_from('<I', request, 4)[0]
        if action > hl.CLAIM_GIVE_UP:
            return self._invalid(ok, 3)
        if token == 0:
            return self._invalid(ok, 4)
        if action == hl.CLAIM_GIVE_UP:
            if token == self.holder_token:
                self._set_holder(0, bytes(16))
        elif action == hl.CLAIM_TAKE_OVER or self.holder_token in (0, token):
            self._set_holder(token, request[8:24], request[24] & hl.CLAIM_EXCLUSIVE)
            self.leased = self.now
        return (ok + request[3:8] + self._controller()).ljust(hl.REPORT_SIZE, b'\x00')

    def take_over(self, token: int = 0xB2, name: bytes = b'Other', exclusive: bool = False) -> None:
        """Have another application take the claim over, which also renews its lease."""
        self._set_holder(token, name.ljust(16, b'\x00'), hl.CLAIM_EXCLUSIVE if exclusive else 0)
        self.leased = self.now
        self._note()

    def let_go(self) -> None:
        """Have the holder give the claim up."""
        self._set_holder(0, bytes(16))
        self._note()

    def idle(self, idle: bool = True) -> None:
        """Have the LED idle timeout turn the animations off, or a button press wake them."""
        super().idle(idle)
        self._note()

    def reconfigure(self, lights: Optional[list[list[bytes]]] = None,
                    controls: Optional[list[list[bytes]]] = None) -> None:
        """Change the Lights or Controls page. The token and the counters follow the pages, and an event is raised."""
        summary = bytearray(self.summary[0])
        if lights is not None:
            self.lights = list(lights)
            struct.pack_into('<H', summary, _SUMMARY_LIGHTS, len(self.lights))
        if controls is not None:
            self.controls = list(controls)
            lit = sum(1 for control in self.controls if control[0][3] & 0x02)
            struct.pack_into('<HHH', summary, _SUMMARY_CONTROLS, len(self.controls), lit, len(self.controls) - lit)
        self.summary = [bytes(summary), self.summary[1]]
        self._note()

    def replug(self) -> None:
        """Put the board back on the bus after a power cycle, with the CONFIGURE defaults and no takeover."""
        super().replug()
        self.live = False
        self.overlay, self.keepalive_ms, self.brightness = 0, 2000, 1
        self.pending = False
        self.queue.clear()
        self._hashes = []
        self._refresh()
        self._seen = (tuple(self.state), self.token)

    def _set_brightness(self, request: bytes) -> int:
        """Apply SET_BRIGHTNESS. An unsaved step starts its keepalive."""
        offset = super()._set_brightness(request)
        if not offset and self._unsaved():
            self.unsaved_refreshed = self.now
        return offset

    def _configure(self, ok: bytes, request: bytes) -> bytes:
        """Set the takeover mode, the timeout (0 = 2000, clamped to 100-10000) and brightness scaling."""
        overlay, timeout, brightness = request[3], struct.unpack_from('<H', request, 4)[0], request[6]
        if overlay > 1:
            return self._invalid(ok, 3)
        if brightness > 1:
            return self._invalid(ok, 6)
        self.overlay, self.brightness = overlay, brightness
        self.keepalive_ms = 2000 if timeout == 0 else min(max(timeout, 100), 10000)
        settings = struct.pack('<BHB', self.overlay, self.keepalive_ms, self.brightness)
        return (ok + settings).ljust(hl.REPORT_SIZE, b'\x00')

    def _subscribe(self, ok: bytes, request: bytes) -> bytes:
        """Add event 0 when asked. The first subscription does not report changes made before it."""
        first = not self.subscribed
        reply = self._feature(ok, hl.CMD_SUBSCRIBE, request)
        if first and self.subscribed:
            self._refresh()
            self._seen = (tuple(self.state), self.token)
        return reply

    def _lifecycle(self, ok: bytes, request: bytes) -> bytes:
        """CLEAR, COMMIT (starts a takeover when none is live) and RELEASE (ends it)."""
        if request[0] == hl.CMD_COMMIT:
            self._publish()
        elif request[0] == hl.CMD_RELEASE:
            self._end_takeover()
        else:
            self.staged.clear()
        return ok.ljust(hl.REPORT_SIZE, b'\x00')

    # -- state -------------------------------------------------------------------

    def _apply_flags(self, command: int, flags: int) -> None:
        if flags & hl.FLAG_COMMIT_AFTER:
            self._publish()
        if 0x10 <= command <= 0x2F or flags & (hl.FLAG_COMMIT_AFTER | hl.FLAG_KEEPALIVE):
            self.refreshed = self.now
            if self._unsaved():
                self.unsaved_refreshed = self.now

    def _publish(self) -> None:
        if not self.live:
            self.live = True
            self.number = (self.number + 1) & 0xFF
        self.refreshed = self.now
        self._record_frame()

    def _end_takeover(self) -> None:
        """End the takeover, which clears the staged frame as CLEAR does."""
        self.live = False
        self.staged.clear()

    def _set_holder(self, token: int, name: bytes, flags: int = 0) -> None:
        if token != self.holder_token:
            # a change of holder ends the takeover and an unsaved step, puts the CONFIGURE settings back to
            # their defaults and steps the generation
            self._end_takeover()
            self._show_step(self.saved_step)
            self.overlay, self.keepalive_ms, self.brightness = 0, 2000, 1
            self.generation = self.generation % hl.GENERATION_MAX + 1
        self.holder_token, self.holder_name, self.holder_flags = token, bytes(name), flags

    def _unsaved(self) -> bool:
        """Say whether the brightness step shown differs from the saved step."""
        return bool(self.state[0][6] != self.saved_step)

    def _tick(self) -> None:
        timeout = self.keepalive_ms / 1000
        if self.steal_at is not None and self.now >= self.steal_at:
            self.steal_at = None
            self._set_holder(0xB2, b'Other'.ljust(16, b'\x00'))
            self.leased = self.now
        if self.live and self.now - self.refreshed > timeout:
            self._end_takeover()
        if self.holder_token and self.now - self.leased > timeout:
            self._set_holder(0, bytes(16))
        if self._unsaved() and self.now - self.unsaved_refreshed > timeout:
            self._show_step(self.saved_step)
        self._note()

    def _refresh(self) -> None:
        state = bytearray(self.state[0])
        state[0], state[7] = int(self.live) | state[0] & (hl.STATE_UNSAVED | hl.STATE_IDLE), self.number
        self.state = [bytes(state), self._controller(),
                      struct.pack('<BHB', self.overlay, self.keepalive_ms, self.brightness)]
        # the token follows what the configuration pages report, the change counters apart. A page that
        # changed since it was last hashed steps its counter (pages 8 to 12 here)
        pages = [self.summary[0], self.profiles, self.controls, self.lights, self.animations]
        hashes = [zlib.crc32(repr(page).encode()) for page in pages]
        changes = bytearray(self.summary[1])
        for index, (was, now) in enumerate(zip(self._hashes, hashes)):
            if was != now:
                changes[index] = (changes[index] + 1) & 0xFF
        self._hashes = hashes
        self.summary = [self.summary[0], bytes(changes)]
        self.token = zlib.crc32(repr(hashes).encode()) or 1

    def _note(self) -> None:
        self._refresh()
        now = (tuple(self.state), self.token)
        if now != self._seen:
            self._seen = now
            if self.subscribed & (1 << hl.EVENT_STATE_CHANGED):
                self.pending = True

    def _event(self) -> bytes:
        self._refresh()
        reply = page_reply(hl.PAGE_STATE, [self.state], token=self.token)
        return bytes([hl.EVENT_MARKER, hl.EVENT_STATE_CHANGED, 0]) + reply[3:]


class _FakeHid:
    """A stand-in for the hidapi module that enumerates and opens fake boards, for attach()."""

    def __init__(self, boards: tuple['FakeBoard', ...], other_majors: tuple[int, ...]) -> None:
        """Hold the boards on the fake bus and the usages of interfaces that speak another major."""
        self.boards = boards
        self.other_majors = other_majors

    def enumerate(self) -> list[dict[str, Any]]:         # noqa: A003 hidapi's name
        """List the Host Lighting interfaces present, as hidapi enumeration dicts."""
        infos = [{'path': b'board-%d' % index, 'usage_page': hl.USAGE_PAGE, 'usage': hl.USAGE}
                 for index, board in enumerate(self.boards) if not board.departed]
        infos += [{'path': b'other-%d' % index, 'usage_page': hl.USAGE_PAGE, 'usage': usage}
                  for index, usage in enumerate(self.other_majors)]
        return infos

    def device(self) -> '_FakeHidDevice':
        """Make an unopened device, as hidapi does."""
        return _FakeHidDevice(self)


class _FakeHidDevice:
    """An unopened fake hidapi device, bound to its board by open_path()."""

    def __init__(self, hid: _FakeHid):
        """Remember the fake bus."""
        self.hid = hid
        self.handle: Any = None

    def open_path(self, path: bytes) -> None:
        """Open a board by its fake path, the first handle or another application's."""
        if not path.startswith(b'board-'):
            raise OSError(f"cannot open {path!r}")
        board = self.hid.boards[int(path[6:])]
        if board.departed:
            raise OSError("open failed")
        self.handle = _SecondHandle(board) if board.opened else board
        board.opened = True

    def set_nonblocking(self, nonblocking: bool) -> None:
        """Accept the non-blocking mode HostLightingDevice asks for."""

    def write(self, data: bytes) -> int:
        """Write one report to the board."""
        written: int = self.handle.write(data)
        return written

    def read(self, size: int, timeout_ms: int = 0) -> list[int]:
        """Read one report from the board."""
        report: list[int] = self.handle.read(size, timeout_ms)
        return report

    def close(self) -> None:
        """Close the handle."""
        self.handle.close()


def attach(monkeypatch: Any, *boards: FakeBoard, other_majors: tuple[int, ...] = ()) -> None:
    """Put fake boards on a fake bus, so find_devices(), open_device(), find_board() and the rest find them.

    Each board needs its own board_id. The bus lists a board while it is plugged in.

    :param monkeypatch: pytest's monkeypatch fixture, which undoes this after the test
    :param boards: the fake boards on the bus
    :param other_majors: the usages of interfaces on the usage page that speak another protocol major
    """
    hid = _FakeHid(boards, tuple(other_majors))
    monkeypatch.setattr(hl, '_import_hid', lambda: hid)
