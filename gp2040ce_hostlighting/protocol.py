"""Talk to a GP2040-CE board's Host Lighting interface over HID.

The Host Lighting add-on exposes a vendor HID interface (usage page 0xFF47,
usage 0x0002) carrying the Host Lighting Protocol (HLP) 2.0: fixed 64-byte
reports that stage and commit LED frames and describe the board from its own
configuration.

A request is `[0] command, [1] sequence, [2] flags, [3..] payload`. Its reply
echoes the command with bit 7 set: `[0] command|0x80, [1] sequence,
[2] status, [3..] payload`. An event arrives with `[0] = 0x80`. HELLO names
the board and lists its capabilities. GET_PAGE reads the pages:

* page 0 (identity): board label, board config, platform and build strings
* page 1 (state): input mode, profile, player, animation, the brightness step
  shown and the saved one, takeover, the application holding CLAIM, whether
  exclusively and the claim generation, and the CONFIGURE settings
* page 2 (input modes): every mode SET_INPUT_MODE accepts, named by the firmware
* page 8 (summary): LED extent and limit, render rate, colour format,
  brightness and speed steps, grid size, each configuration page's total,
  the display's button layouts, and a change counter per configuration page
* page 9 (profiles), page 10 (controls: every input with an action, lit or
  not, keyed by GPIO pin, Hall-effect trigger or expander pin), page 11
  (lights: every light's LEDs, kind, owner and grid cell) and page 12 (the
  on-board animations and their speeds)

Actions, light kinds and colour formats are GP2040-CE's own enum values,
reported verbatim.

LightsClaim keeps a claim on the lights. It renews the claim, sends CONFIGURE
again after a lapse and hands the lights back.

See docs/host-lighting.md for the full protocol reference. The hlp-*
commands are in gp2040ce_hostlighting.tools.commands, and hlp-conformance
and hlp-bench in gp2040ce_hostlighting.tools.conformance. Requires the `hidapi` package
(`pip install hidapi`).

SPDX-FileCopyrightText: © 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import logging
import secrets
import struct
import time
from dataclasses import fields
from typing import Callable, Optional

from gp2040ce_hostlighting.records import (AnimationRecord, Changes, ControlInput, Controller, ControlLights,
                                           ControlRecord, Effects, GridCell, Hello, Identity, InputModeFlags,
                                           InputModeRecord, Instances, LedRange, LightKind, LightRecord, Owner,
                                           PageHeader, PageRead, PaletteSlot, Position, Profile, ProfileRecord,
                                           Settings, Speeds, StageOutcome, State, StateChanged, StateRecord, Summary,
                                           SummaryRecord)

logger = logging.getLogger(__name__)

# discovery: match the interface by these, never by VID:PID (which varies by input mode)
USAGE_PAGE = 0xFF47
USAGE = 0x0002
REPORT_SIZE = 64
MAGIC = b'GPHL'
PROTOCOL_MAJOR = 2

# commands, grouped by function
CMD_HELLO = 0x01                # session and discovery, 0x01-0x0F
CMD_GET_PAGE = 0x02
CMD_CLAIM = 0x03
CMD_CONFIGURE = 0x04
CMD_SUBSCRIBE = 0x05
CMD_STAGE = 0x10                # frame staging, 0x10-0x1F
CMD_UNSTAGE = 0x11
CMD_FILL = 0x12
CMD_CLEAR = 0x13
CMD_COMMIT = 0x20               # frame lifecycle, 0x20-0x2F
CMD_RELEASE = 0x21
CMD_SET_PROFILE = 0x30          # board features, 0x30-0x4F
CMD_SET_ANIMATION = 0x31
CMD_SET_ANIMATION_SPEED = 0x32
CMD_SET_BRIGHTNESS = 0x33
CMD_SET_INPUT_MODE = 0x70       # privileged (magic-guarded), 0x70-0x7F
CMD_REBOOT = 0x71
# the command groups. An addition takes the next free number in its group, and 0x50-0x6F is reserved for new
# groups
COMMAND_RANGES = ((0x01, 0x0F), (0x10, 0x1F), (0x20, 0x2F), (0x30, 0x4F), (0x50, 0x6F), (0x70, 0x7F))

# request flags, the same on every command
FLAG_COMMIT_AFTER = 0x01        # publish the staged frame after this command succeeds
FLAG_NO_REPLY = 0x02            # send no reply
FLAG_KEEPALIVE = 0x04           # refresh a live takeover's and an unsaved brightness step's keepalive
FLAG_HOLDER = 0x08              # sent by the holder of an exclusive claim, which refuses changes without it
# bits 4-6 carry the claim generation the sender acts under, 1-7, or 0 for none. A request other than CLAIM under a
# generation that is no longer current is refused, and an exclusive claim needs it beside FLAG_HOLDER
FLAG_GENERATION_SHIFT = 4
FLAG_GENERATION = 0x70
GENERATION_MAX = 7

REPLY_FLAG = 0x80
EVENT_MARKER = 0x80             # [0] of an event. There is no command 0x00
STATUS_OK = 0
STATUS_UNSUPPORTED = 1
STATUS_INVALID = 2
STATUS_STALE_TOKEN = 3
STATUS_CLAIMED = 4
STATUS_NAMES = {STATUS_OK: 'OK', STATUS_UNSUPPORTED: 'UNSUPPORTED', STATUS_INVALID: 'INVALID_ARG',
                STATUS_STALE_TOKEN: 'STALE_TOKEN', STATUS_CLAIMED: 'CLAIMED'}
EVENT_STATE_CHANGED = 0

# pages
PAGE_IDENTITY = 0
PAGE_STATE = 1
PAGE_INPUT_MODES = 2
PAGE_SUMMARY = 8
PAGE_PROFILES = 9
PAGE_CONTROLS = 10
PAGE_LIGHTS = 11
PAGE_ANIMATIONS = 12
PAGE_RANGES = ((0, 7), (8, 15), (16, 23), (24, 31))     # board, configuration, diagnostics, reserved

# every page reply's header. The records area is [12..59] and the state token [60..63]
HEADER_RECORDS = 12
RECORD_AREA = 48
HEADER_TOKEN = 60

# each page's field groups, in mask bit order, and their widths in bytes. Page 0's groups are
# strings of up to 48 bytes each
PAGE_GROUPS = {
    PAGE_STATE: (9, 22, 4),
    PAGE_INPUT_MODES: (2, 22),
    PAGE_SUMMARY: (30, 8),
    PAGE_PROFILES: (18,),
    PAGE_CONTROLS: (4, 4, 6, 4),
    PAGE_LIGHTS: (4, 2, 3, 4, 2, 1),
    PAGE_ANIMATIONS: (4, 3),
}
IDENTITY_GROUPS = 8
IDENTITY_STRING_MAX = 48

# page 1's State flags and page 8's Summary flags
STATE_LIVE = 0x01
STATE_UNSAVED = 0x02
STATE_IDLE = 0x04
SUMMARY_WHITE_CHANNEL = 0x01

# page 10 keys: GPIO pin p is key p, Hall-effect trigger n is KEY_TRIGGER + n and PCF8575 pin n is
# KEY_EXPANDER + n. The rest are reserved
KEY_SOURCES = (('gpio', 0, 48), ('hall_effect', 0x80, 32), ('expander', 0xC0, 16))     # (source, base, count)
KEY_TRIGGER = KEY_SOURCES[1][1]
KEY_EXPANDER = KEY_SOURCES[2][1]
KEY_PREFIXES = {'gpio': 'GP', 'hall_effect': 'HE', 'expander': 'PCF'}

# the none values: they never equal a key
PIN_NONE = 0xFF
SLOT_NONE = 0xFF
INDEX_NONE = 0xFFFF
ACTION_NONE = -32768            # 0x8000 as a signed 16-bit action
ANIMATION_OFF = 0xFFFF
SPEED_UNCHANGED = 0xFF          # SET_ANIMATION_SPEED: leave this speed as it is
BRIGHTNESS_SAVED = 0xFF         # SET_BRIGHTNESS: show the saved step

# staging: [3] bits 0-3 addressing, bits 4-7 pixel format
ADDRESSING_LED_RUN = 0
ADDRESSING_LIGHT_RUN = 1
ADDRESSING_LIGHT = 4
ADDRESSING_PIN = 5
ADDRESSING_ACTION = 6
ADDRESSING_KIND = 7
ADDRESS_SIZES = {ADDRESSING_LIGHT: 2, ADDRESSING_PIN: 1, ADDRESSING_ACTION: 2, ADDRESSING_KIND: 1}
ADDRESS_RANGES = {ADDRESSING_LIGHT: (0, 0xFFFF), ADDRESSING_PIN: (0, 0xFF), ADDRESSING_ACTION: (-32768, 32767),
                  ADDRESSING_KIND: (0, 0xFF)}
PIXEL_RGB = 0
PIXEL_RGBW = 1
PIXEL_RGB565 = 2
PIXEL_SIZES = {PIXEL_RGB: 3, PIXEL_RGBW: 4, PIXEL_RGB565: 2}
OUTCOME_BITS = 32               # the STAGE and UNSTAGE outcome mask

# CLAIM actions, the application name's size and the claim flags
CLAIM_TAKE = 0                  # take the claim when unclaimed, keep it when held
CLAIM_TAKE_OVER = 1
CLAIM_GIVE_UP = 2
CLAIM_NAME_SIZE = 16
# an exclusive claim refuses other applications' changes: requests without FLAG_HOLDER and the current generation
CLAIM_EXCLUSIVE = 0x01
CONTROLLER_SIZE = 22            # page 1's Controller group, which the CLAIM and status 4 replies carry

# CONFIGURE
TAKEOVER_WHOLE_FRAME = 0
TAKEOVER_OVERLAY = 1

# LightType values the tools select by, as GP2040-CE defines them (proto/enums.proto)
KIND_BUTTON = 0
KIND_CASE = 1
KIND_TURBO = 2
KIND_PLAYERS = (3, 4, 5, 6)

# privileged commands' guard magic and REBOOT targets
MAGIC_INPUT_MODE = b'MODE'
MAGIC_REBOOT = b'BOOT'
REBOOT_GAMEPAD = 0
REBOOT_WEBCONFIG = 1
REBOOT_BOOTSEL = 2


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from('<H', data, offset)[0]


def _s16(data: bytes, offset: int) -> int:
    return struct.unpack_from('<h', data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from('<I', data, offset)[0]


def _string(data: bytes) -> str:
    """Decode a NUL-padded string leniently: a cut string can end in a partial character."""
    return data.split(b'\x00')[0].decode('utf-8', 'replace')


def _bits(value: int) -> list:
    """List the set bit numbers of a bitmap, lowest first."""
    return [bit for bit in range(value.bit_length()) if value & (1 << bit)]


def build_request(command: int, sequence: int, payload: bytes = b'', flags: int = 0) -> bytes:
    """Frame one HLP request as a full 64-byte report.

    :param command: HLP command byte
    :param sequence: sequence byte, echoed by the reply
    :param payload: the command's bytes from [3]
    :param flags: request flags (FLAG_*)
    :return: the 64-byte report
    """
    if len(payload) > REPORT_SIZE - 3:
        raise ValueError(f"payload of {len(payload)} bytes does not fit a {REPORT_SIZE}-byte report")
    return bytes([command, sequence, flags]) + payload + bytes(REPORT_SIZE - 3 - len(payload))


def match_reply(reply: bytes, command: int, sequence: int) -> bool:
    """Say whether a report is the reply to a request.

    :param reply: a received report
    :param command: the request's command byte
    :param sequence: the request's sequence byte
    :return: True when the report echoes both
    """
    return len(reply) >= 3 and reply[0] == command | REPLY_FLAG and reply[1] == sequence


def is_event(report: bytes) -> bool:
    """Say whether a received report is an event."""
    return len(report) >= 3 and report[0] == EVENT_MARKER


def stage_capacity(addressing: int, pixel_format: Optional[int]) -> int:
    """How many run members or entries one STAGE (or, with no format, UNSTAGE) report carries.

    :param addressing: ADDRESSING_* mode
    :param pixel_format: PIXEL_*, or None for UNSTAGE
    :return: the per-report capacity
    """
    pixel = PIXEL_SIZES[pixel_format] if pixel_format is not None else 0
    if addressing in (ADDRESSING_LED_RUN, ADDRESSING_LIGHT_RUN):
        capacity = (REPORT_SIZE - 7) // pixel if pixel else OUTCOME_BITS
    else:
        capacity = (REPORT_SIZE - 5) // (ADDRESS_SIZES[addressing] + pixel)
    return min(capacity, OUTCOME_BITS)


def encode_pixel(colour: tuple, pixel_format: int) -> bytes:
    """Encode one colour in a pixel format.

    :param colour: (red, green, blue) or (red, green, blue, white), 0-255 each
    :param pixel_format: PIXEL_RGB, PIXEL_RGBW or PIXEL_RGB565
    :return: the pixel's bytes as STAGE and FILL send them
    """
    red, green, blue = colour[:3]
    if pixel_format == PIXEL_RGB:
        return bytes([red, green, blue])
    if pixel_format == PIXEL_RGBW:
        return bytes([red, green, blue, colour[3] if len(colour) > 3 else 0])
    if pixel_format == PIXEL_RGB565:
        return struct.pack('<H', ((red >> 3) << 11) | ((green >> 2) << 5) | (blue >> 3))
    raise ValueError(f"unknown pixel format {pixel_format}")


def encode_address(addressing: int, address: int) -> bytes:
    """Encode one entry's address for STAGE or UNSTAGE.

    :param addressing: ADDRESSING_LIGHT, ADDRESSING_PIN, ADDRESSING_ACTION or ADDRESSING_KIND
    :param address: the light ordinal, pin, action (signed) or kind
    :return: the address bytes
    """
    if addressing == ADDRESSING_ACTION:
        return struct.pack('<h', address)
    if ADDRESS_SIZES[addressing] == 2:
        return struct.pack('<H', address)
    return bytes([address])


def stage_entries_payloads(addressing: int, entries: list, pixel_format: int = PIXEL_RGB) -> list:
    """Build the STAGE payloads for entries addressed by light, pin, action or kind, chunked to the report capacity.

    Nothing is sent. A host with its own transport sends each payload as a STAGE request, with COMMIT_AFTER on the
    last to publish the frame.

    :param addressing: ADDRESSING_LIGHT, ADDRESSING_PIN, ADDRESSING_ACTION or ADDRESSING_KIND
    :param entries: (address, colour) pairs, each colour as for encode_pixel
    :param pixel_format: PIXEL_RGB, PIXEL_RGBW or PIXEL_RGB565
    :return: one payload per report, in order, and none for no entries
    """
    capacity = stage_capacity(addressing, pixel_format)
    payloads = []
    for start in range(0, len(entries), capacity):
        chunk = entries[start:start + capacity]
        payloads.append(bytes([addressing | (pixel_format << 4), len(chunk)]) + b''.join(
            encode_address(addressing, address) + encode_pixel(colour, pixel_format) for address, colour in chunk))
    return payloads


def stage_run_payloads(addressing: int, first: int, colours: list, pixel_format: int = PIXEL_RGB) -> list:
    """Build the STAGE payloads for consecutive LEDs or lights from a first index, chunked to the report capacity.

    Nothing is sent. A streaming host sends every payload but the last with NO_REPLY, and the last with
    COMMIT_AFTER to publish the frame, so one reply acknowledges the run.

    :param addressing: ADDRESSING_LED_RUN or ADDRESSING_LIGHT_RUN
    :param first: the first LED index or light ordinal
    :param colours: one colour per LED or light, as for encode_pixel
    :param pixel_format: PIXEL_RGB, PIXEL_RGBW or PIXEL_RGB565
    :return: one payload per report, in order
    """
    if addressing not in (ADDRESSING_LED_RUN, ADDRESSING_LIGHT_RUN) or not colours:
        raise ValueError("a run needs ADDRESSING_LED_RUN or ADDRESSING_LIGHT_RUN and at least one colour")
    capacity = stage_capacity(addressing, pixel_format)
    payloads = []
    for start in range(0, len(colours), capacity):
        chunk = colours[start:start + capacity]
        payload = bytes([addressing | (pixel_format << 4)]) + struct.pack('<HB', first + start, len(chunk))
        payloads.append(payload + b''.join(encode_pixel(colour, pixel_format) for colour in chunk))
    return payloads


def _require_length(data: bytes, length: int, what: str) -> None:
    """Refuse data too short for the fields about to be read from it.

    :raises HostLightingError: naming what was short, before a decoder reads past its end
    """
    if len(data) < length:
        raise HostLightingError(f"{what} is {len(data)} bytes, shorter than the {length} it needs")


def decode_hello(reply: bytes) -> Hello:
    """Decode a HELLO reply.

    :param reply: the reply report
    :return: the magic, version, board ID, reply queue limit and the capability bitmaps
    :raises HostLightingError: if the reply is too short
    """
    _require_length(reply, 47, 'the HELLO reply')
    return Hello(magic=bytes(reply[3:7]), major=reply[7], minor=reply[8], board_id=bytes(reply[9:17]).hex().upper(),
                 reply_queue_limit=reply[17], request_flags=frozenset(_bits(reply[18])),
                 commands=frozenset(_bits(int.from_bytes(reply[19:35], 'little'))),
                 pages=frozenset(_bits(_u32(reply, 35))), addressing_modes=frozenset(_bits(_u16(reply, 39))),
                 pixel_formats=frozenset(_bits(_u16(reply, 41))), events=frozenset(_bits(_u16(reply, 43))),
                 events_enabled=frozenset(_bits(_u16(reply, 45))))


def decode_page_header(reply: bytes) -> PageHeader:
    """Decode the header every GET_PAGE reply carries.

    :param reply: the reply report
    :return: the echoed page, start and field groups, the page's total, the records in this reply, their
        stride and the state token
    :raises HostLightingError: if the reply is too short
    """
    _require_length(reply, HEADER_TOKEN + 4, 'the GET_PAGE reply')
    return PageHeader(status=reply[2], page=reply[3], start=_u16(reply, 4), total=_u16(reply, 6), count=reply[8],
                      stride=reply[9], groups=_u16(reply, 10), token=_u32(reply, HEADER_TOKEN))


def _state(data: bytes) -> State:
    animation = _u16(data, 4)
    return State(takeover_live=bool(data[0] & STATE_LIVE), brightness_unsaved=bool(data[0] & STATE_UNSAVED),
                 animations_idle=bool(data[0] & STATE_IDLE), input_mode=data[1], profile=data[2], player=data[3],
                 animation=None if animation == ANIMATION_OFF else animation, brightness_step_shown=data[6],
                 takeover_number=data[7], saved_brightness_step=data[8])


def decode_controller(data: bytes) -> Controller:
    """Decode page 1's Controller group, which the CLAIM reply ([8..29]) and a status 4 reply ([3..24]) carry too.

    :param data: the group's 22 bytes
    :return: the claim's holder
    :raises HostLightingError: if the group is too short
    """
    _require_length(data, CONTROLLER_SIZE, 'the Controller group')
    return Controller(holder_token=_u32(data, 0), application_name=_string(data[4:20]),
                      exclusive=bool(data[20] & CLAIM_EXCLUSIVE), claim_generation=data[21])


def _settings(data: bytes) -> Settings:
    return Settings(takeover=data[0], keepalive_ms=_u16(data, 1), brightness_applied=bool(data[3]))


def _input_mode_flags(data: bytes) -> InputModeFlags:
    return InputModeFlags(value=data[0], lighting=bool(data[1] & 0x01), detected=bool(data[1] & 0x02))


def _input_mode_name(data: bytes) -> str:
    return _string(data[:22])


def _summary(data: bytes) -> Summary:
    return Summary(led_extent=_u16(data, 0), led_limit=_u16(data, 2), render_hz=_u16(data, 4), colour_format=data[6],
                   white_channel=bool(data[7] & SUMMARY_WHITE_CHANNEL), brightness_maximum=data[8],
                   brightness_steps=data[9], animation_speed_steps=data[10], button_layout=data[11],
                   button_layout_right=data[12], button_layout_orientation=data[13], grid_width=_u16(data, 14),
                   grid_height=_u16(data, 16), profiles=_u16(data, 18), controls=_u16(data, 20),
                   lit_controls=_u16(data, 22), unlit_controls=_u16(data, 24), lights=_u16(data, 26),
                   animations=_u16(data, 28))


def _changes(data: bytes) -> Changes:
    return Changes(counters=tuple(data[:8]))


def _profile(data: bytes) -> Profile:
    return Profile(number=data[0], enabled=bool(data[1] & 0x01), label=_string(data[2:18]))


def decode_key(key: int) -> tuple:
    """Decode a page 10 key as (source, index).

    :return: ('gpio', pin), ('hall_effect', trigger) or ('expander', pin), or ('unknown', key) for a key protocol
        2.0 does not define, which a later 2.x board may use
    """
    for source, base, count in KEY_SOURCES:
        if base <= key < base + count:
            return source, key - base
    return 'unknown', key


def key_name(key: int) -> str:
    """Name a page 10 key's input: GP5, HE3 (Hall-effect trigger 3), PCF0 (expander pin 0) or key 0x40."""
    source, index = decode_key(key)
    return f"{KEY_PREFIXES[source]}{index}" if source in KEY_PREFIXES else f"key 0x{key:02X}"


def _control_input(data: bytes) -> ControlInput:
    source, index = decode_key(data[0])
    return ControlInput(key=data[0], source=source, index=index, action=_s16(data, 1),
                        duplicated=bool(data[3] & 0x01), lit=bool(data[3] & 0x02))


def _instances(data: bytes) -> Instances:
    return Instances(instance=_u16(data, 0), instances=_u16(data, 2))


def _position(data: bytes) -> Position:
    first = _u16(data, 0)
    return Position(first_led=None if first == INDEX_NONE else first,
                    grid_rectangle=None if first == INDEX_NONE else tuple(data[2:6]))


def _control_lights(data: bytes) -> ControlLights:
    first = _u16(data, 0)
    return ControlLights(first_light=None if first == INDEX_NONE else first, light_count=_u16(data, 2))


def _led_range(data: bytes) -> LedRange:
    return LedRange(first_led=_u16(data, 0), led_count=_u16(data, 2))


def _light_kind(data: bytes) -> LightKind:
    return LightKind(value=data[0], duplicated=bool(data[1] & 0x01))


def _owner(data: bytes) -> Owner:
    action = _s16(data, 1)
    return Owner(pin=None if data[0] == PIN_NONE else data[0], action=None if action == ACTION_NONE else action)


def _grid_cell(data: bytes) -> GridCell:
    return GridCell(x=data[0], y=data[1])


def _palette_slot(data: bytes) -> PaletteSlot:
    return PaletteSlot(slot=None if data[0] == SLOT_NONE else data[0])


def _effects(data: bytes) -> Effects:
    return Effects(enabled=bool(data[0] & 0x01), idle=data[1], pressed=data[2], case=data[3])


def _speeds(data: bytes) -> Speeds:
    return Speeds(idle=data[0], pressed=data[1], case=data[2])


# each table page's record type, and the attribute and decoder of each field group in mask bit order
_PAGE_RECORDS: dict = {
    PAGE_STATE: (StateRecord, (('state', _state), ('controller', decode_controller), ('settings', _settings))),
    PAGE_INPUT_MODES: (InputModeRecord, (('mode', _input_mode_flags), ('name', _input_mode_name))),
    PAGE_SUMMARY: (SummaryRecord, (('summary', _summary), ('changes', _changes))),
    PAGE_PROFILES: (ProfileRecord, (('profile', _profile),)),
    PAGE_CONTROLS: (ControlRecord, (('input', _control_input), ('instances', _instances), ('position', _position),
                                    ('lights', _control_lights))),
    PAGE_LIGHTS: (LightRecord, (('leds', _led_range), ('kind', _light_kind), ('owner', _owner),
                                ('owner_instances', _instances), ('grid', _grid_cell), ('palette', _palette_slot))),
    PAGE_ANIMATIONS: (AnimationRecord, (('effects', _effects), ('speeds', _speeds))),
}


def decode_records(reply: bytes) -> tuple:
    """Decode the records of a GET_PAGE reply, field group by field group.

    Each record holds the groups the reply echoes, and None for the others, with `ordinal`, the record's
    position in the page (the reply's start plus its index in the reply).

    :param reply: the reply report of a table page (1, 2, 8-12)
    :return: the records
    :raises HostLightingError: for a page that is not a table, a stride narrower than the groups it echoes, or
        more records than the reply holds
    """
    header = decode_page_header(reply)
    page = header.page
    if page not in PAGE_GROUPS:
        raise HostLightingError(f"page {page} is not a table page")
    widths = PAGE_GROUPS[page]
    record_type, groups = _PAGE_RECORDS[page]
    echoed = sum(width for group, width in enumerate(widths) if header.groups & (1 << group))
    if header.count and header.stride < echoed:
        raise HostLightingError(f"page {page} records are {header.stride} bytes apart, narrower than the "
                                f"{echoed} bytes of the groups echoed")
    if header.count * header.stride > RECORD_AREA:
        raise HostLightingError(f"the page {page} reply holds {header.count} records of {header.stride} "
                                f"bytes, more than its {RECORD_AREA}-byte records area")
    records = []
    for index in range(header.count):
        offset = HEADER_RECORDS + index * header.stride
        values: dict = {'ordinal': header.start + index}
        for group, (width, (name, decode)) in enumerate(zip(widths, groups)):
            if header.groups & (1 << group):
                values[name] = decode(bytes(reply[offset:offset + width]))
                offset += width
        records.append(record_type(**values))
    return tuple(records)


def decode_state_changed(event: bytes) -> StateChanged:
    """Decode a STATE_CHANGED event. Its [3..63] are those of a page 1 reply with mask 0.

    :param event: the event report ([1] is the event number)
    :return: the page 1 record and the state token
    :raises HostLightingError: for any other event
    """
    if event[1] != EVENT_STATE_CHANGED:
        raise HostLightingError(f"event {event[1]} is not STATE_CHANGED")
    return StateChanged(record=decode_records(event)[0], token=_u32(event, HEADER_TOKEN))


_IDENTITY_NAMES = tuple(item.name for item in fields(Identity))


def decode_identity(reply: bytes) -> Identity:
    """Decode a page 0 reply's strings.

    Each returned group is a string of up to 48 bytes, NUL-terminated when shorter, starting after the
    previous one's NUL or 48th byte.

    :param reply: the reply report
    :return: the strings the reply echoes, None for the others
    :raises HostLightingError: if the reply is too short
    """
    _require_length(reply, HEADER_RECORDS, 'the page 0 reply')
    groups = _u16(reply, 10)
    area = bytes(reply[HEADER_RECORDS:HEADER_RECORDS + RECORD_AREA])
    strings = {}
    used = 0
    for group in range(IDENTITY_GROUPS):
        if groups & (1 << group):
            raw = area[used:used + IDENTITY_STRING_MAX].split(b'\x00')[0]
            strings[_IDENTITY_NAMES[group]] = raw.decode('utf-8', 'replace')
            used += len(raw) + 1 if len(raw) < IDENTITY_STRING_MAX else IDENTITY_STRING_MAX
    return Identity(**strings)


def _check_claim_name(name: bytes) -> None:
    """Refuse a claim name longer than the 16 bytes the board keeps.

    :raises ValueError: for a longer name
    """
    if len(name) > CLAIM_NAME_SIZE:
        raise ValueError(f"the claim name is {len(name)} bytes, over the {CLAIM_NAME_SIZE} the board keeps")


class HostLightingError(RuntimeError):
    """Errors talking to a Host Lighting interface."""


class HostLightingRejectedError(HostLightingError):
    """The board answered a command with a status other than 0."""

    def __init__(self, command: int, status: int, offset: int, holder: Optional[Controller] = None):
        """Record the rejected command, its status, and the byte at fault or the claim's holder.

        :param holder: for CLAIMED, the holder as decode_controller gives it, with holder_token 0 when unclaimed
        """
        self.command = command
        self.status = status
        self.offset = offset
        self.holder = holder
        detail = f" at request byte {offset}" if status == STATUS_INVALID else ''
        if status == STATUS_CLAIMED and holder:
            detail = (f" by '{holder.application_name}' (claim 0x{holder.holder_token:08X}, generation "
                      f"{holder.claim_generation})" if holder.holder_token
                      else f": the claim has ended (generation {holder.claim_generation})")
        super().__init__(f"command 0x{command:02X} rejected: {STATUS_NAMES.get(status, hex(status))}{detail}")


class HostLightingTimeoutError(HostLightingError):
    """No reply arrived in time. The board may be busy, for example writing its settings."""


class HostLightingStaleTokenError(HostLightingError):
    """The board's state changed: the state token a read named is out of date."""

    def __init__(self, page: int, token: int):
        """Record the page and the state token that no longer match the board."""
        self.page = page
        self.token = token
        super().__init__(f"page {page}: state token 0x{token:08X} is out of date")


class HostLightingDisconnectedError(HostLightingError):
    """A read or a write on the HID device failed: the board has left the bus."""


class HostLightingGroupNotReadError(HostLightingError, AttributeError):
    """A record's field group was read, but the reply had not returned it.

    It is an AttributeError too, so hasattr() tells whether a record holds a group.
    """

    def __init__(self, page: int, group: str, ordinal: int):
        """Record the page, the group and the record's ordinal."""
        self.page = page
        self.group = group
        self.ordinal = ordinal
        super().__init__(f"page {page} record {ordinal}: the {group} group was not read")


class HostLightingDevice:
    """One GP2040-CE board's Host Lighting interface."""

    def __init__(self, path: Optional[bytes] = None, handle=None):
        """Open the HID device at the given hidapi path, or use a handle already open.

        :param path: platform-specific hidapi device path from enumeration
        :param handle: instead of a path, an open hidapi device set non-blocking, or an object with its write,
            read and close methods, such as gp2040ce_hostlighting.testing's fake boards
        :raises ValueError: when neither a path nor a handle is given
        """
        if handle is None:
            if path is None:
                raise ValueError("give the device's path or an open handle")
            hid = _import_hid()
            handle = hid.device()
            handle.open_path(path)
            # a read with no timeout returns at once, so request() can drop the reports already received
            handle.set_nonblocking(True)
        self.device = handle
        # the first sequence number is random, so two applications seldom send the same one
        self.sequence = secrets.randbelow(255)
        self.events: list = []
        # the requests sent with replies awaited, and those replies when read while waiting for another
        self.awaited: set = set()
        self.pending: list = []
        # this application holds the claim under this generation (0: it holds none): its requests carry it,
        # and FLAG_HOLDER while the claim is exclusive, which that claim requires
        self.generation = 0
        self.holding_exclusive = False
        self.closed = False

    def close(self) -> None:
        """Close the HID device. A second close does nothing."""
        if not self.closed:
            self.closed = True
            self.device.close()

    def _write(self, report: bytes) -> None:
        """Write one report.

        hidapi reports a failed write by a negative result, and does not raise.

        :raises HostLightingDisconnectedError: if the device does not take the report
        """
        sending = f"sending command 0x{report[1]:02X}" if len(report) > 1 else "writing"
        try:
            written = self.device.write(report)
        except OSError as error:
            raise HostLightingDisconnectedError(f"the board is no longer connected ({sending} failed with "
                                                f"{error})") from error
        if isinstance(written, int) and written < 0:
            raise HostLightingDisconnectedError(f"the board is no longer connected ({sending} failed)")

    def _read(self, timeout_ms: int) -> bytes:
        """Read one report, or nothing within the timeout.

        :param timeout_ms: how long to wait, where 0 returns at once
        :raises HostLightingDisconnectedError: if the device cannot be read
        """
        try:
            return bytes(self.device.read(REPORT_SIZE, timeout_ms))
        except OSError as error:
            raise HostLightingDisconnectedError(f"the board is no longer connected (reading failed with "
                                                f"{error})") from error

    def send(self, command: int, payload: bytes = b'', flags: int = 0, sequence: Optional[int] = None) -> int:
        """Send one request without waiting for its reply.

        :param command: HLP command byte
        :param payload: the command's bytes from [3]
        :param flags: request flags (FLAG_*). While this application holds the claim, its generation is added
            unless the flags carry one, and FLAG_HOLDER while the claim is exclusive
        :param sequence: the sequence number to send, or None for the next one
        :return: the sequence number sent
        """
        if sequence is None:
            self.sequence = (self.sequence % 255) + 1
            sequence = self.sequence
        if not flags & FLAG_GENERATION:
            flags |= self.generation << FLAG_GENERATION_SHIFT
        if self.holding_exclusive:
            flags |= FLAG_HOLDER
        self._await(command, sequence, flags)
        # the interface uses unnumbered reports. hidapi wants a leading 0x00 report ID on write
        try:
            self._write(b'\x00' + build_request(command, sequence, payload, flags))
        except HostLightingDisconnectedError:
            self.awaited.discard((command, sequence))
            raise
        return sequence

    def collect(self, seconds: float) -> list:
        """Read every report that arrives within a time: replies, other applications' replies and events.

        :param seconds: how long to read
        :return: the reports, in arrival order
        """
        reports: list = []
        deadline = time.monotonic() + seconds
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return reports
            report = self._read(max(1, int(left * 1000)))
            if report:
                reports.append(report)

    def wait_reply(self, command: int, sequence: int, timeout: float = 0.5, accept=None) -> bytes:
        """Wait for the reply to one request.

        Replies reach every application with the interface open, so a report that answers none of this
        application's requests is another application's and is dropped. Events are kept in `events`, and the
        replies to other requests still awaited in `pending`, where a later wait finds them: each reply is
        matched by its command echo and sequence number, not by order.

        :param command: the request's command byte
        :param sequence: the request's sequence number
        :param timeout: seconds to wait
        :param accept: optional test a matching report must also pass (to tell two applications' replies apart)
        :return: the reply report
        """
        for report in [report for report in self.pending if match_reply(report, command, sequence)]:
            self.pending.remove(report)
            if accept is None or accept(report):
                self.awaited.discard((command, sequence))
                return report
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            # a read that blocks until a report or the timeout: no polling sleep, which is ~16 ms on Windows
            # before Python 3.11
            report = self._read(max(1, int(left * 1000)))
            if not report:
                continue
            if not match_reply(report, command, sequence):
                self._keep(report)
            elif accept is None or accept(report):
                self.awaited.discard((command, sequence))
                return report
            # otherwise another application's reply with this request's numbers: dropped
        self.awaited.discard((command, sequence))
        raise HostLightingTimeoutError(f"no reply to command 0x{command:02X} within {timeout} s")

    def _await(self, command: int, sequence: int, flags: int) -> None:
        """Note a request that will be answered. No report kept for an earlier one with its numbers answers it."""
        if not flags & FLAG_NO_REPLY and 0 < command < REPLY_FLAG:
            self.pending = [report for report in self.pending if not match_reply(report, command, sequence)]
            self.awaited.add((command, sequence))

    def _keep(self, report: bytes) -> None:
        """Keep an event, or the reply to a request still awaited, and drop another application's reply."""
        if is_event(report):
            self.events.append(report)
        elif len(report) >= 3 and (report[0] & ~REPLY_FLAG, report[1]) in self.awaited:
            self.pending.append(report)

    def drain(self) -> None:
        """Read the reports already received, keeping the events and the replies still awaited.

        None of them is the reply to a request not yet sent. request() drains first. A host that sends with
        send() and waits with wait_reply() drains before each send.
        """
        while True:
            report = self._read(0)
            if not report:
                return
            self._keep(report)

    def take_events(self) -> list:
        """Hand over the events received so far, oldest first, draining first.

        :return: the event reports, which are then forgotten here
        """
        self.drain()
        events, self.events = self.events, []
        return events

    def request(self, command: int, payload: bytes = b'', flags: int = 0, timeout: float = 0.5,
                accept=None) -> bytes:
        """Send one request and wait for its reply.

        Every application's replies reach this one and wait until read, so the reports already received are
        read first and dropped, but for events and the replies to earlier send() calls still awaited: another
        application's earlier reply with the same command and sequence is not taken for this one's.

        :param command: HLP command byte
        :param payload: the command's bytes from [3]
        :param flags: request flags (FLAG_*). FLAG_NO_REPLY is for send(), since nothing comes back
        :param timeout: seconds to wait for the reply
        :param accept: optional test a matching report must also pass
        :return: the reply report ([2] is the status, [3..] the payload)
        """
        if flags & FLAG_NO_REPLY:
            raise ValueError("a request sent with FLAG_NO_REPLY gets no reply. Use send()")
        self.drain()
        return self.wait_reply(command, self.send(command, payload, flags), timeout, accept)

    def request_ok(self, command: int, payload: bytes = b'', flags: int = 0, timeout: float = 0.5,
                   accept=None) -> bytes:
        """Send one request and require status 0 in the reply.

        :param command: HLP command byte
        :param payload: the command's bytes from [3]
        :param flags: request flags (FLAG_*)
        :param timeout: seconds to wait for the reply
        :param accept: optional test a matching report must also pass
        :return: the reply report
        :raises HostLightingRejectedError: on any other status, naming the claim's holder for CLAIMED
        """
        reply = self.request(command, payload, flags, timeout, accept)
        if reply[2] != STATUS_OK:
            raise self.rejected(command, reply)
        return reply

    @staticmethod
    def rejected(command: int, reply: bytes) -> HostLightingRejectedError:
        """Build the error for a refused request.

        A status 2 reply carries the offset of the invalid request byte at [3], and a status 4 reply the claim's
        holder at [3..24].

        :param command: the command the board refused
        :param reply: its reply report
        :return: the error to raise
        """
        if reply[2] != STATUS_CLAIMED:
            return HostLightingRejectedError(command, reply[2], reply[3])
        return HostLightingRejectedError(command, reply[2], 0, decode_controller(bytes(reply[3:3 + CONTROLLER_SIZE])))

    def hello(self) -> Hello:
        """Send HELLO and decode the reply.

        :return: the decoded reply (see decode_hello)
        :raises HostLightingError: if the reply does not carry the GPHL magic
        """
        info = decode_hello(self.request_ok(CMD_HELLO))
        if info.magic != MAGIC:
            raise HostLightingError(f"HELLO reply carries magic {info.magic!r}, not {MAGIC!r}")
        return info

    def get_page(self, page: int, start: int = 0, mask: int = 0, token: int = 0) -> bytes:
        """Read one GET_PAGE reply.

        A reply to another application's identical request is told apart by its echoed page, start and
        field groups.

        :param page: page number
        :param start: first record
        :param mask: field groups to return, or 0 for every group
        :param token: expected state token, 0 for no check
        :return: the reply report, with status 0 or 3 (STALE_TOKEN)
        :raises HostLightingRejectedError: on any other status
        """
        def echoes(reply: bytes) -> bool:
            if reply[2] not in (STATUS_OK, STATUS_STALE_TOKEN):
                return True
            header = decode_page_header(reply)
            if header.page != page or header.start != start:
                return False
            if reply[2] == STATUS_STALE_TOKEN:
                return True
            if mask:
                return not header.groups & ~mask
            # mask 0: a table page returns every group it defines (only page 0's strings can fail to fit)
            known = (1 << len(PAGE_GROUPS[page])) - 1 if page in PAGE_GROUPS else 0
            return header.groups & known == known

        reply = self.request(CMD_GET_PAGE, struct.pack('<BHIH', page, start, token, mask), accept=echoes)
        if reply[2] not in (STATUS_OK, STATUS_STALE_TOKEN):
            raise self.rejected(CMD_GET_PAGE, reply)
        return reply

    def read_table(self, page: int, mask: int = 0, token: int = 0, attempts: int = 5) -> PageRead:
        """Read every record of a table page as one coherent snapshot.

        Without a token, the walk names the state token of its first reply in every later read. If the board's
        state changes mid-walk, the board answers STALE_TOKEN and the walk starts again, so the records never mix
        two states. With a token, a host that read page 1 or 8 under it reads under the same token, so
        everything it holds describes one state, and a change raises HostLightingStaleTokenError instead.

        :param page: page number
        :param mask: field groups to return, or 0 for every group
        :param token: the state token to read under, or 0 to adopt the first reply's
        :param attempts: without a token, how many times to start the walk before giving up
        :return: the decoded records and the state token they carry
        :raises HostLightingStaleTokenError: with a token, if the board's state no longer matches it
        :raises HostLightingError: if the state keeps changing or the board stops making progress
        """
        if token:
            walked = self._walk_table(page, mask, token)
            if walked is None:
                raise HostLightingStaleTokenError(page, token)
            return walked
        for _ in range(attempts):
            walked = self._walk_table(page, mask, 0)
            if walked:
                return walked
        raise HostLightingError(f"page {page} kept changing during {attempts} walks")

    def _walk_table(self, page: int, mask: int, token: int) -> Optional[PageRead]:
        """Walk a table page from its first record, adopting the first reply's token when token is 0.

        :return: the decoded records and their state token, or None when the board answers STALE_TOKEN
        """
        records: list = []
        while True:
            reply = self.get_page(page, len(records), mask, token)
            if reply[2] == STATUS_STALE_TOKEN:
                return None
            header = decode_page_header(reply)
            token = header.token
            if header.count == 0:
                if len(records) < header.total:
                    raise HostLightingError(f"page {page} stalled at {len(records)} of {header.total} records")
                return PageRead(records=tuple(records), token=token)
            records.extend(decode_records(reply))
            if len(records) >= header.total:
                return PageRead(records=tuple(records), token=token)

    def read_record(self, page: int, mask: int = 0) -> PageRead:
        """Read a one-record page (1 or 8).

        :param page: page number
        :param mask: field groups to return, or 0 for every group
        :return: the one record and the state token
        """
        reply = self.get_page(page, 0, mask)
        return PageRead(records=decode_records(reply), token=decode_page_header(reply).token)

    def read_identity(self, mask: int = 0) -> Identity:
        """Read page 0's strings, asking separately for the groups that did not fit.

        :param mask: the groups wanted, bit n for Identity's nth field, or 0 for all eight
        :return: the strings read, None for the others
        """
        strings: dict = {}
        missing = mask or (1 << IDENTITY_GROUPS) - 1
        while missing:
            identity = decode_identity(self.get_page(PAGE_IDENTITY, 0, missing))
            found = {name: getattr(identity, name) for name in _IDENTITY_NAMES if getattr(identity, name) is not None}
            if not found:
                break
            strings.update(found)
            for name in found:
                missing &= ~(1 << _IDENTITY_NAMES.index(name))
        return Identity(**strings)

    def claim(self, action: int, token: int, name: bytes = b'', flags: int = 0,
              exclusive: bool = False) -> Controller:
        """Claim the board, take it over or give it up.

        The reply echoes the action and token, which tells it apart from another application's CLAIM reply.
        Each claim or take over that leaves this application holding the claim records the claim flags sent,
        so a holder renews an exclusive claim with exclusive set. While this application holds the claim, its
        requests carry the claim generation the reply showed (`generation`), and FLAG_HOLDER while the claim
        is exclusive. Every change of holder puts the CONFIGURE settings back to their defaults, so a holder
        calls configure() after it claims.

        :param action: CLAIM_TAKE, CLAIM_TAKE_OVER or CLAIM_GIVE_UP
        :param token: this application's non-zero token
        :param name: this application's name, up to 16 bytes, shown in page 1 while it holds the claim
        :param flags: request flags (FLAG_*). FLAG_KEEPALIVE also refreshes a live takeover and an unsaved
            brightness step
        :param exclusive: claim exclusively: until the claim ends, the board refuses other applications'
            requests without FLAG_HOLDER and the generation but CLAIM, and HELLO, GET_PAGE and SUBSCRIBE
            without COMMIT_AFTER or KEEPALIVE
        :return: the holder now, as decode_controller gives it, with holder_token 0 when unclaimed
        :raises ValueError: for a name over 16 bytes
        """
        _check_claim_name(name)
        payload = bytes([action]) + struct.pack('<I', token) + name.ljust(CLAIM_NAME_SIZE, b'\x00')
        payload += bytes([CLAIM_EXCLUSIVE if exclusive else 0])

        def echoes(reply: bytes) -> bool:
            # an invalid argument's reply carries no echo
            return reply[2] != STATUS_OK or reply[3:8] == payload[:5]

        reply = self.request_ok(CMD_CLAIM, payload, flags, accept=echoes)
        holder = decode_controller(bytes(reply[8:8 + CONTROLLER_SIZE]))
        held = holder.held_by(token)
        self.generation = holder.claim_generation if held else 0
        self.holding_exclusive = held and exclusive
        return holder

    def configure(self, overlay: bool = False, keepalive_ms: int = 0, brightness: bool = True) -> Settings:
        """Set the takeover mode, keepalive timeout and brightness scaling.

        :param overlay: True for overlay takeover, False for whole-frame
        :param keepalive_ms: keepalive timeout in ms, 0 for the default 2000, clamped to 100-10000
        :param brightness: scale host pixels by the board's brightness
        :return: the settings the board applied
        """
        reply = self.request_ok(CMD_CONFIGURE, struct.pack('<BHB', int(overlay), keepalive_ms, int(brightness)))
        return _settings(bytes(reply[3:7]))

    def subscribe(self, events: int) -> int:
        """Enable events. The subscription is shared by every application on the board, so this only adds.

        :param events: bit n enables event n (EVENT_STATE_CHANGED is 0)
        :return: the events now enabled
        """
        return _u16(self.request_ok(CMD_SUBSCRIBE, struct.pack('<H', events)), 3)

    def next_event(self, timeout: float) -> Optional[bytes]:
        """Wait for the next event. Events that arrived while waiting for a reply come first.

        A reply to a request still awaited is kept for its wait_reply. Another application's is dropped.

        :param timeout: seconds to wait
        :return: the event report, or None when none arrives in time
        """
        if self.events:
            return self.events.pop(0)
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            report = self._read(max(1, int(left * 1000)))
            if not report:
                continue
            if is_event(report):
                return report
            self._keep(report)

    def stage_entries(self, addressing: int, entries: list, pixel_format: int = PIXEL_RGB,
                      commit: bool = False) -> list:
        """Stage entries addressed by light, pin, action or kind, chunked to the report capacity.

        Each report is answered, for its outcome mask.

        :param addressing: ADDRESSING_LIGHT, ADDRESSING_PIN, ADDRESSING_ACTION or ADDRESSING_KIND
        :param entries: (address, colour) pairs, each colour as for encode_pixel
        :param pixel_format: PIXEL_RGB, PIXEL_RGBW or PIXEL_RGB565
        :param commit: publish the staged frame with the last report (COMMIT_AFTER)
        :return: a StageOutcome per report sent
        """
        payloads = stage_entries_payloads(addressing, entries, pixel_format)
        outcomes = []
        for index, payload in enumerate(payloads):
            last = index == len(payloads) - 1
            reply = self.request_ok(CMD_STAGE, payload, FLAG_COMMIT_AFTER if commit and last else 0)
            outcomes.append(StageOutcome(applied=reply[3], skipped=reply[4], outcome=_u32(reply, 5)))
        return outcomes

    def stage_run(self, addressing: int, first: int, colours: list, pixel_format: int = PIXEL_RGB,
                  commit: bool = False) -> StageOutcome:
        """Stage consecutive LEDs or lights from a first index, as a streaming host does.

        The colours are chunked to the report capacity. Every report but the last goes with NO_REPLY. The last
        is answered, and publishes the frame with COMMIT_AFTER when commit is set, so one reply acknowledges
        the run.

        :param addressing: ADDRESSING_LED_RUN or ADDRESSING_LIGHT_RUN
        :param first: the first LED index or light ordinal
        :param colours: one colour per LED or light, as for encode_pixel
        :param pixel_format: PIXEL_RGB, PIXEL_RGBW or PIXEL_RGB565
        :param commit: publish the staged frame with the last report (COMMIT_AFTER)
        :return: the last report's StageOutcome
        """
        payloads = stage_run_payloads(addressing, first, colours, pixel_format)
        for payload in payloads[:-1]:
            self.send(CMD_STAGE, payload, FLAG_NO_REPLY)
        reply = self.request_ok(CMD_STAGE, payloads[-1], FLAG_COMMIT_AFTER if commit else 0)
        return StageOutcome(applied=reply[3], skipped=reply[4], outcome=_u32(reply, 5))

    def fill(self, colour: tuple, pixel_format: int = PIXEL_RGB, commit: bool = False) -> None:
        """Stage one colour on every LED below the addressable LED limit.

        :param colour: as for encode_pixel
        :param pixel_format: PIXEL_RGB, PIXEL_RGBW or PIXEL_RGB565
        :param commit: publish the staged frame (COMMIT_AFTER)
        """
        self.request_ok(CMD_FILL, bytes([pixel_format << 4]) + encode_pixel(colour, pixel_format),
                        FLAG_COMMIT_AFTER if commit else 0)


class LightsClaim:
    """Keep a claim on a board's lights, renew it and hand it back."""

    def __init__(self, device: HostLightingDevice, name: str, token: int, exclusive: bool = False,
                 report: Optional[Callable[[str], None]] = None):
        """Record a claim the application holds.

        :param device: an opened HostLightingDevice
        :param name: the application's name, up to 16 bytes, shown in page 1 while it holds the claim
        :param token: the claim token
        :param exclusive: whether the claim is exclusive. Each renewal claims the same way
        :param report: called with one line of text when another application has taken the lights over,
            which is logged at INFO when no callable is given
        :raises ValueError: for a name over 16 bytes
        """
        _check_claim_name(name.encode())
        self.device = device
        self.name = name
        self.token = token
        self.exclusive = exclusive
        self.held = True
        self.settings: dict = {}
        self.report = report or logger.info

    def configure(self, overlay: bool = False, keepalive_ms: int = 2000, brightness: bool = True) -> Settings:
        """Send CONFIGURE and remember the settings, which renew() sends again once it retakes a lapsed claim.

        :param overlay: True for overlay takeover, False for whole-frame
        :param keepalive_ms: keepalive timeout in ms
        :param brightness: scale host pixels by the board's brightness
        :return: the settings the board applied
        """
        self.settings = {'overlay': overlay, 'keepalive_ms': keepalive_ms, 'brightness': brightness}
        return self.device.configure(**self.settings)

    def renew(self) -> bool:
        """Renew the claim and, with KEEPALIVE, the takeover.

        A new generation means the claim had lapsed and this renewal took it again: that change of holder put
        the CONFIGURE settings back to their defaults, so the claim's settings are sent again.

        :return: False, having reported it, once another application has taken the lights over
        """
        generation = self.device.generation
        holder = self.device.claim(CLAIM_TAKE, self.token, self.name.encode(), FLAG_KEEPALIVE, self.exclusive)
        if not holder.held_by(self.token):
            self.report(f"'{holder.application_name}' took the lights over")
            self.held = False
        elif self.device.generation != generation and self.settings:
            self.device.configure(**self.settings)
        return self.held

    def keep(self, seconds: float) -> bool:
        """Keep the lights for a while, renewing them each second.

        :param seconds: how long to keep them
        :return: False as soon as another application takes them over
        """
        remaining = seconds
        while remaining > 0:
            step = min(1.0, remaining)
            time.sleep(step)
            remaining -= step
            if not self.renew():
                return False
        return True

    def let_lapse(self) -> None:
        """Stop holding without handing back: the takeover and the claim end at the keepalive timeout.

        The application's requests then carry neither the claim generation nor FLAG_HOLDER, as those of an
        application that holds no claim: once the claim has lapsed, its generation would be refused.
        """
        self.held = False
        self.device.generation = 0
        self.device.holding_exclusive = False

    def hand_back(self) -> bool:
        """Give up the claim, which restores the on-board animations and the CONFIGURE defaults.

        Giving up ends the takeover and an unsaved brightness step and puts the settings back. When another
        application holds the claim (the lease lapsed unnoticed and it claimed), nothing changes.

        :return: True when the lights were handed back
        """
        self.held = False
        try:
            holder = self.device.claim(CLAIM_GIVE_UP, self.token)
            if holder.holder_token:
                self.report(f"'{holder.application_name}' took the lights over")
                return False
            return True
        except (HostLightingError, OSError):
            logger.warning("could not hand the lights back. The keepalive timeout restores the animations")
            return False


def _import_hid():
    """Import the hidapi module, with a helpful error if it is missing."""
    try:
        import hid
    except ImportError as error:
        raise HostLightingError("these tools require the hidapi package: pip install hidapi") from error
    return hid


def find_devices() -> list:
    """Enumerate all Host Lighting interfaces on the system.

    :return: list of hidapi enumeration dicts for matching interfaces
    """
    hid = _import_hid()
    return [info for info in hid.enumerate()
            if info.get('usage_page') == USAGE_PAGE and info.get('usage') == USAGE]


def _probe(info: dict, board_id_prefix: str) -> Optional[tuple[str, HostLightingDevice]]:
    """Open one interface and keep it when it answers HELLO as a matching protocol 2 board.

    :param info: a hidapi enumeration dict
    :param board_id_prefix: hex prefix of the HELLO factory board ID
    :return: (board ID, the opened device), or None, with the device closed
    """
    try:
        device = HostLightingDevice(info['path'])
    except OSError:
        logger.warning("skipping an interface that will not open (on Linux, check its udev rule)")
        logger.debug("opening %s failed", info['path'], exc_info=True)
        return None
    try:
        hello = device.hello()
    except (HostLightingError, OSError):
        device.close()
        return None
    if hello.major != PROTOCOL_MAJOR:
        logger.warning("skipping a board speaking protocol %d.%d. These tools need %d.x",
                       hello.major, hello.minor, PROTOCOL_MAJOR)
    elif hello.board_id.startswith(board_id_prefix.upper()):
        return hello.board_id, device
    device.close()
    return None


def open_device(board_id_prefix: str = '') -> HostLightingDevice:
    """Open a Host Lighting device, disambiguating by board ID if needed.

    :param board_id_prefix: optional hex prefix of the HELLO factory board ID
    :return: an opened HostLightingDevice
    """
    infos = find_devices()
    if not infos:
        raise HostLightingError("no Host Lighting interface found. Connect a board "
                                "with the add-on enabled")
    candidates = [found for found in (_probe(info, board_id_prefix) for info in infos) if found]
    if not candidates:
        raise HostLightingError(f"no board speaking protocol {PROTOCOL_MAJOR}.x matches ID prefix "
                                f"'{board_id_prefix}'")
    if len(candidates) > 1:
        ids = ', '.join(board_id for board_id, _ in candidates)
        for _, device in candidates:
            device.close()
        raise HostLightingError(f"multiple boards found ({ids}). Select one with --board-id")
    return candidates[0][1]


def find_board(board_id: str) -> Optional[HostLightingDevice]:
    """Open the board with this whole factory board ID, if it is connected.

    :param board_id: the HELLO factory board ID, 16 hex digits in either case
    :return: an opened HostLightingDevice, or None when no connected protocol 2 board has that ID
    """
    if len(board_id) != 16:
        raise ValueError("find_board needs the whole 16-digit board ID. open_device takes a prefix")
    for info in find_devices():
        found = _probe(info, board_id)
        if found:
            return found[1]
    return None


def send_reboot(device: HostLightingDevice, command: int, payload: bytes) -> bool:
    """Send a reboot-style command, tolerating the board disconnecting.

    The reply is sent before the reboot, but the board may drop off the bus before it can be read. A vanished
    device or a missing reply means the reboot is under way, not a failure. A rejection (an invalid mode or
    target, or a wrong guard magic) still raises.

    :param device: an opened HostLightingDevice
    :param command: CMD_SET_INPUT_MODE or CMD_REBOOT
    :param payload: the command's value and guard magic
    :return: True if the board acknowledged, False if it went away without a readable acknowledgement
    """
    try:
        device.request_ok(command, payload)
        return True
    except HostLightingRejectedError:
        raise
    except (HostLightingError, OSError):
        logger.debug("no acknowledgement before the disconnect. The reboot is proceeding")
        return False
