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
  shown and the saved one, takeover, whether the animations are idle, the
  application holding CLAIM, whether exclusively and the claim generation, and
  the CONFIGURE settings
* page 2 (input modes): every mode SET_INPUT_MODE accepts, named by the firmware
* page 8 (summary): LED extent and limit, render rate, colour format and white
  channel, brightness and speed steps, the display's button layouts and their
  orientation, grid size, each configuration page's total, and a change
  counter per configuration page
* page 9 (profiles), page 10 (controls: every input with an action, lit or
  not, keyed by GPIO pin, Hall-effect trigger or expander pin), page 11
  (lights: every light's LEDs, kind, owner and grid cell) and page 12 (the
  on-board animations and their speeds)

Actions, light kinds and colour formats are GP2040-CE's own enum values,
reported verbatim.

gp2040ce_hostlighting.claim keeps a claim on the lights. It renews the claim,
sends CONFIGURE again after a lapse and gives the claim up.

See docs/host-lighting.md for the full protocol reference. The hlp-*
commands are in gp2040ce_hostlighting.tools.commands, and hlp-conformance
and hlp-bench in gp2040ce_hostlighting.tools.conformance. Requires the
`hidapi` package (`pip install hidapi`).

SPDX-FileCopyrightText: © 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import logging
import operator
import secrets
import struct
import sys
import time
from dataclasses import fields
from typing import Any, Callable, Final, Iterable, Literal, Optional, Protocol, Sequence, overload

from gp2040ce_hostlighting.records import (AnimationRecord, BoardListing, Changes, ControlInput, ControlInstances,
                                           Controller, ControlLights, ControlRecord, Effects, GridCell, Hello, Identity,
                                           InputModeFlags, InputModeRecord, Instances, LedRange, LightKind, LightRecord,
                                           Owner, PageHeader, PageRead, PaletteSlot, Position, Profile, ProfileRecord,
                                           Requirements, Settings, Speeds, StageOutcome, State, StateChanged,
                                           StateRecord, Summary, SummaryRecord)

__all__ = [
    # the interface, framing and replies
    'USAGE_PAGE', 'USAGE', 'USAGE_PROTOCOL_1', 'REPORT_SIZE', 'MAGIC', 'PROTOCOL_MAJOR', 'REPLY_FLAG', 'EVENT_MARKER',
    'build_request', 'match_reply', 'is_event',
    # commands, flags, statuses, events
    'CMD_HELLO', 'CMD_GET_PAGE', 'CMD_CLAIM', 'CMD_CONFIGURE', 'CMD_SUBSCRIBE', 'CMD_STAGE', 'CMD_UNSTAGE', 'CMD_FILL',
    'CMD_CLEAR', 'CMD_COMMIT', 'CMD_RELEASE', 'CMD_SET_PROFILE', 'CMD_SET_ANIMATION', 'CMD_SET_ANIMATION_SPEED',
    'CMD_SET_BRIGHTNESS', 'CMD_SET_INPUT_MODE', 'CMD_REBOOT', 'COMMAND_NAMES', 'COMMAND_RANGES',
    'FLAG_COMMIT_AFTER', 'FLAG_NO_REPLY', 'FLAG_KEEPALIVE', 'FLAG_HOLDER', 'FLAG_GENERATION', 'FLAG_GENERATION_SHIFT',
    'GENERATION_MAX', 'STATUS_OK', 'STATUS_UNSUPPORTED', 'STATUS_INVALID', 'STATUS_STALE_TOKEN', 'STATUS_CLAIMED',
    'STATUS_NAMES', 'EVENT_STATE_CHANGED',
    # pages
    'PAGE_IDENTITY', 'PAGE_STATE', 'PAGE_INPUT_MODES', 'PAGE_SUMMARY', 'PAGE_PROFILES', 'PAGE_CONTROLS',
    'PAGE_LIGHTS', 'PAGE_ANIMATIONS', 'PAGE_RANGES', 'PAGE_GROUPS', 'HEADER_RECORDS', 'RECORD_AREA', 'HEADER_TOKEN',
    'IDENTITY_GROUPS', 'IDENTITY_STRING_MAX', 'STATE_LIVE', 'STATE_UNSAVED', 'STATE_IDLE', 'SUMMARY_WHITE_CHANNEL',
    'KEY_SOURCES', 'KEY_TRIGGER', 'KEY_EXPANDER', 'KEY_PREFIXES', 'PIN_NONE', 'SLOT_NONE', 'INDEX_NONE',
    'ACTION_NONE', 'ANIMATION_OFF', 'SPEED_UNCHANGED', 'BRIGHTNESS_SAVED',
    # staging
    'ADDRESSING_LED_RUN', 'ADDRESSING_LIGHT_RUN', 'ADDRESSING_LIGHT', 'ADDRESSING_PIN', 'ADDRESSING_ACTION',
    'ADDRESSING_KIND', 'ADDRESSING_NAMES', 'ADDRESS_SIZES', 'ADDRESS_RANGES', 'PIXEL_RGB', 'PIXEL_RGBW',
    'PIXEL_RGB565', 'PIXEL_NAMES', 'PIXEL_SIZES', 'OUTCOME_BITS', 'stage_capacity', 'encode_pixel',
    'encode_address', 'stage_entry_payloads', 'stage_run_payloads', 'unstage_entry_payloads', 'unstage_run_payloads',
    'subtractive_white',
    # the claim, CONFIGURE and the board features
    'CLAIM_TAKE', 'CLAIM_TAKE_OVER', 'CLAIM_GIVE_UP', 'CLAIM_NAME_SIZE', 'CLAIM_EXCLUSIVE', 'CONTROLLER_SIZE',
    'TAKEOVER_WHOLE_FRAME', 'TAKEOVER_OVERLAY', 'KIND_BUTTON', 'KIND_CASE', 'KIND_TURBO', 'KIND_PLAYERS',
    'MAGIC_INPUT_MODE', 'MAGIC_REBOOT', 'REBOOT_GAMEPAD', 'REBOOT_WEBCONFIG', 'REBOOT_BOOTSEL',
    # decoders
    'decode_hello', 'decode_page_header', 'decode_controller', 'decode_records', 'decode_state_changed',
    'decode_identity', 'decode_key', 'key_name', 'latest_state',
    # errors
    'HostLightingError', 'HostLightingRejectedError', 'HostLightingTimeoutError', 'HostLightingStaleTokenError',
    'HostLightingDisconnectedError', 'HostLightingUnansweredError', 'HostLightingIncompatibleError',
    'HostLightingGroupNotReadError', 'HostLightingBoardSelectionError', 'HostLightingClaimedError',
    # the device, discovery and capabilities
    'HidHandle', 'HostLightingDevice', 'EVENTS_KEPT', 'find_devices', 'find_other_majors', 'open_device', 'find_board',
    'list_boards', 'missing_capabilities', 'require_capabilities',
    # the records, from gp2040ce_hostlighting.records
    'BoardListing', 'Controller', 'Hello', 'Identity', 'PageHeader', 'PageRead', 'Requirements', 'Settings',
    'StageOutcome', 'StateChanged',
]

logger = logging.getLogger(__name__)

# discovery matches the interface by these, never by VID:PID (which varies by input mode)
USAGE_PAGE = 0xFF47
USAGE = 0x0002
USAGE_PROTOCOL_1 = 0x004C       # the usage pre-release protocol 1.x firmware presented on the same usage page
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
COMMAND_NAMES = {CMD_HELLO: 'HELLO', CMD_GET_PAGE: 'GET_PAGE', CMD_CLAIM: 'CLAIM', CMD_CONFIGURE: 'CONFIGURE',
                 CMD_SUBSCRIBE: 'SUBSCRIBE', CMD_STAGE: 'STAGE', CMD_UNSTAGE: 'UNSTAGE', CMD_FILL: 'FILL',
                 CMD_CLEAR: 'CLEAR', CMD_COMMIT: 'COMMIT', CMD_RELEASE: 'RELEASE', CMD_SET_PROFILE: 'SET_PROFILE',
                 CMD_SET_ANIMATION: 'SET_ANIMATION', CMD_SET_ANIMATION_SPEED: 'SET_ANIMATION_SPEED',
                 CMD_SET_BRIGHTNESS: 'SET_BRIGHTNESS', CMD_SET_INPUT_MODE: 'SET_INPUT_MODE', CMD_REBOOT: 'REBOOT'}
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

# pages. Final, so that a type checker reads each as its value and knows the records read_table() returns
PAGE_IDENTITY: Final = 0
PAGE_STATE: Final = 1
PAGE_INPUT_MODES: Final = 2
PAGE_SUMMARY: Final = 8
PAGE_PROFILES: Final = 9
PAGE_CONTROLS: Final = 10
PAGE_LIGHTS: Final = 11
PAGE_ANIMATIONS: Final = 12
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

# the none values, which never equal a key
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
ADDRESSING_NAMES = {ADDRESSING_LED_RUN: 'LED run', ADDRESSING_LIGHT_RUN: 'light run', ADDRESSING_LIGHT: 'light',
                    ADDRESSING_PIN: 'pin', ADDRESSING_ACTION: 'action', ADDRESSING_KIND: 'kind'}
PIXEL_NAMES = {PIXEL_RGB: 'RGB', PIXEL_RGBW: 'RGBW', PIXEL_RGB565: 'RGB565'}
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
    value: int = struct.unpack_from('<H', data, offset)[0]
    return value


def _s16(data: bytes, offset: int) -> int:
    value: int = struct.unpack_from('<h', data, offset)[0]
    return value


def _u32(data: bytes, offset: int) -> int:
    value: int = struct.unpack_from('<I', data, offset)[0]
    return value


def _string(data: bytes) -> str:
    """Decode a NUL-padded string leniently, since a cut string can end in a partial character."""
    return data.split(b'\x00')[0].decode('utf-8', 'replace')


def _bits(value: int) -> list[int]:
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


_RUNS = (ADDRESSING_LED_RUN, ADDRESSING_LIGHT_RUN)
_INDEX_MAX = 0xFFFF                 # LED indexes and light ordinals are 16-bit


def _check_entries(addressing: int) -> None:
    """Refuse an addressing mode that takes no entries.

    :raises ValueError: for a run's addressing or an unknown one
    """
    if addressing not in ADDRESS_SIZES:
        raise ValueError(f"addressing {addressing} does not take entries")


def _check_run(addressing: int, first: int, count: int) -> None:
    """Refuse a run that is not LED or light addressing, is empty, or passes the last 16-bit index.

    :raises ValueError: naming what is wrong
    """
    if addressing not in _RUNS or count < 1:
        raise ValueError("a run needs ADDRESSING_LED_RUN or ADDRESSING_LIGHT_RUN and at least one member")
    if first < 0 or first + count - 1 > _INDEX_MAX:
        raise ValueError(f"a run of {count} from {first} passes index {_INDEX_MAX}")


def stage_capacity(addressing: int, pixel_format: Optional[int]) -> int:
    """How many run members or entries one STAGE (or, with no format, UNSTAGE) report carries.

    :param addressing: ADDRESSING_* mode
    :param pixel_format: PIXEL_*, or None for UNSTAGE
    :return: the per-report capacity
    :raises ValueError: for an unknown addressing mode or pixel format
    """
    if pixel_format is not None and pixel_format not in PIXEL_SIZES:
        raise ValueError(f"unknown pixel format {pixel_format}")
    if addressing not in _RUNS and addressing not in ADDRESS_SIZES:
        raise ValueError(f"unknown addressing mode {addressing}")
    pixel = PIXEL_SIZES[pixel_format] if pixel_format is not None else 0
    if addressing in _RUNS:
        capacity = (REPORT_SIZE - 7) // pixel if pixel else OUTCOME_BITS
    else:
        capacity = (REPORT_SIZE - 5) // (ADDRESS_SIZES[addressing] + pixel)
    return min(capacity, OUTCOME_BITS)


def encode_pixel(colour: tuple[int, ...], pixel_format: int) -> bytes:
    """Encode one colour in a pixel format.

    :param colour: (red, green, blue) or (red, green, blue, white), 0-255 each
    :param pixel_format: PIXEL_RGB, PIXEL_RGBW or PIXEL_RGB565
    :return: the pixel's bytes as STAGE and FILL send them
    :raises ValueError: for fewer than three channels, a channel outside 0-255 or an unknown pixel format
    """
    if len(colour) < 3:
        raise ValueError(f"colour {colour} has fewer than three channels")
    red, green, blue = colour[:3]
    if pixel_format == PIXEL_RGB:
        return bytes([red, green, blue])
    if pixel_format == PIXEL_RGBW:
        return bytes([red, green, blue, colour[3] if len(colour) > 3 else 0])
    if pixel_format == PIXEL_RGB565:
        if not (0 <= red <= 255 and 0 <= green <= 255 and 0 <= blue <= 255):
            raise ValueError(f"colour {colour} has a channel outside 0-255")
        return struct.pack('<H', ((red >> 3) << 11) | ((green >> 2) << 5) | (blue >> 3))
    raise ValueError(f"unknown pixel format {pixel_format}")


def _check_colour(colour: tuple[int, ...]) -> None:
    """Refuse a colour that is not three or four integer channels of 0-255.

    :raises ValueError: naming the colour
    """
    try:
        channels = [operator.index(channel) for channel in colour]
    except TypeError:
        channels = []
    if len(colour) not in (3, 4) or len(channels) != len(colour) or not all(0 <= value <= 255 for value in channels):
        raise ValueError(f"colour {colour} is not three or four integer channels of 0-255")


def encode_address(addressing: int, address: int) -> bytes:
    """Encode one entry's address for STAGE or UNSTAGE.

    :param addressing: ADDRESSING_LIGHT, ADDRESSING_PIN, ADDRESSING_ACTION or ADDRESSING_KIND
    :param address: the light ordinal, pin, action (signed) or kind
    :return: the address bytes
    :raises ValueError: for an addressing mode without entries, or an address outside its range
    """
    _check_entries(addressing)
    low, high = ADDRESS_RANGES[addressing]
    if not low <= address <= high:
        raise ValueError(f"{ADDRESSING_NAMES[addressing]} address {address} is outside {low} to {high}")
    if addressing == ADDRESSING_ACTION:
        return struct.pack('<h', address)
    if ADDRESS_SIZES[addressing] == 2:
        return struct.pack('<H', address)
    return bytes([address])


def stage_entry_payloads(addressing: int, entries: Sequence[tuple[int, tuple[int, ...]]],
                         pixel_format: int = PIXEL_RGB) -> list[bytes]:
    """Build the STAGE payloads for entries addressed by light, pin, action or kind, chunked to the report capacity.

    Nothing is sent. A host with its own transport sends each payload as a STAGE request, with COMMIT_AFTER on the
    last to publish the frame.

    :param addressing: ADDRESSING_LIGHT, ADDRESSING_PIN, ADDRESSING_ACTION or ADDRESSING_KIND
    :param entries: (address, colour) pairs, each colour as for encode_pixel
    :param pixel_format: PIXEL_RGB, PIXEL_RGBW or PIXEL_RGB565
    :return: one payload per report, in order, and none for no entries
    :raises ValueError: for a run's addressing, an unknown pixel format, or an address or colour out of range
    """
    _check_entries(addressing)
    capacity = stage_capacity(addressing, pixel_format)
    payloads = []
    for start in range(0, len(entries), capacity):
        chunk = entries[start:start + capacity]
        payloads.append(bytes([addressing | (pixel_format << 4), len(chunk)]) + b''.join(
            encode_address(addressing, address) + encode_pixel(colour, pixel_format) for address, colour in chunk))
    return payloads


def stage_run_payloads(addressing: int, first: int, colours: Sequence[tuple[int, ...]],
                       pixel_format: int = PIXEL_RGB) -> list[bytes]:
    """Build the STAGE payloads for consecutive LEDs or lights from a first index, chunked to the report capacity.

    Nothing is sent. A streaming host sends every payload but the last with NO_REPLY, and the last with
    COMMIT_AFTER to publish the frame, so one reply acknowledges the run.

    :param addressing: ADDRESSING_LED_RUN or ADDRESSING_LIGHT_RUN
    :param first: the first LED index or light ordinal
    :param colours: one colour per LED or light, as for encode_pixel
    :param pixel_format: PIXEL_RGB, PIXEL_RGBW or PIXEL_RGB565
    :return: one payload per report, in order
    :raises ValueError: for an entry addressing, no colours, a run past index 65535, an unknown pixel format or a
        colour out of range
    """
    _check_run(addressing, first, len(colours))
    capacity = stage_capacity(addressing, pixel_format)
    payloads = []
    for start in range(0, len(colours), capacity):
        chunk = colours[start:start + capacity]
        payload = bytes([addressing | (pixel_format << 4)]) + struct.pack('<HB', first + start, len(chunk))
        payloads.append(payload + b''.join(encode_pixel(colour, pixel_format) for colour in chunk))
    return payloads


def unstage_entry_payloads(addressing: int, addresses: Sequence[int]) -> list[bytes]:
    """Build the UNSTAGE payloads for addresses by light, pin, action or kind, chunked to the report capacity.

    :param addressing: ADDRESSING_LIGHT, ADDRESSING_PIN, ADDRESSING_ACTION or ADDRESSING_KIND
    :param addresses: the light ordinals, pins, actions or kinds to unstage
    :return: one payload per report, in order, and none for no addresses
    :raises ValueError: for a run's addressing or an address out of range
    """
    _check_entries(addressing)
    capacity = stage_capacity(addressing, None)
    return [bytes([addressing, len(addresses[start:start + capacity])]) +
            b''.join(encode_address(addressing, address) for address in addresses[start:start + capacity])
            for start in range(0, len(addresses), capacity)]


def unstage_run_payloads(addressing: int, first: int, count: int) -> list[bytes]:
    """Build the UNSTAGE payloads for consecutive LEDs or lights from a first index, chunked to the capacity.

    :param addressing: ADDRESSING_LED_RUN or ADDRESSING_LIGHT_RUN
    :param first: the first LED index or light ordinal
    :param count: how many to unstage, at least one
    :return: one payload per report, in order
    :raises ValueError: for an entry addressing, a count under one, or a run past index 65535
    """
    _check_run(addressing, first, count)
    capacity = stage_capacity(addressing, None)
    return [bytes([addressing]) + struct.pack('<HB', first + start, min(capacity, count - start))
            for start in range(0, count, capacity)]


def subtractive_white(colour: tuple[int, ...]) -> tuple[int, int, int, int]:
    """Give the RGBW pixel the specification's subtractive conversion makes of a colour.

    W takes min(R, G, B) and each colour channel loses it, so the white part of the colour lands on the white
    emitter. A W the colour already carries is added, up to 255. The conversion is hlp-spice2x's.

    :param colour: (red, green, blue) or (red, green, blue, white)
    :return: (red, green, blue, white)
    """
    red, green, blue = colour[:3]
    white = min(red, green, blue)
    return red - white, green - white, blue - white, min(255, white + (colour[3] if len(colour) > 3 else 0))


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

    The claim generation is the low three bits of its byte, as the specification's field widths give it.

    :param data: the group's 22 bytes
    :return: the claim's holder
    :raises HostLightingError: if the group is too short
    """
    _require_length(data, CONTROLLER_SIZE, 'the Controller group')
    return Controller(holder_token=_u32(data, 0), application_name=_string(data[4:20]),
                      exclusive=bool(data[20] & CLAIM_EXCLUSIVE),
                      claim_generation=data[21] & (FLAG_GENERATION >> FLAG_GENERATION_SHIFT))


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


def decode_key(key: int) -> tuple[str, int]:
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


def _control_instances(data: bytes) -> ControlInstances:
    return ControlInstances(instance=_u16(data, 0), instances=_u16(data, 2))


def _instances(data: bytes) -> Instances:
    return Instances(instance=_u16(data, 0) or None, instances=_u16(data, 2) or None)


def _position(data: bytes) -> Position:
    first = _u16(data, 0)
    return Position(first_led=None if first == INDEX_NONE else first,
                    grid_rectangle=None if first == INDEX_NONE else (data[2], data[3], data[4], data[5]))


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
_PAGE_RECORDS: dict[int, tuple[type, tuple[tuple[str, Callable[[bytes], Any]], ...]]] = {
    PAGE_STATE: (StateRecord, (('state', _state), ('controller', decode_controller), ('settings', _settings))),
    PAGE_INPUT_MODES: (InputModeRecord, (('mode', _input_mode_flags), ('name', _input_mode_name))),
    PAGE_SUMMARY: (SummaryRecord, (('summary', _summary), ('changes', _changes))),
    PAGE_PROFILES: (ProfileRecord, (('profile', _profile),)),
    PAGE_CONTROLS: (ControlRecord, (('input', _control_input), ('instances', _control_instances),
                                    ('position', _position), ('lights', _control_lights))),
    PAGE_LIGHTS: (LightRecord, (('leds', _led_range), ('kind', _light_kind), ('owner', _owner),
                                ('owner_instances', _instances), ('grid', _grid_cell), ('palette', _palette_slot))),
    PAGE_ANIMATIONS: (AnimationRecord, (('effects', _effects), ('speeds', _speeds))),
}


def decode_records(reply: bytes) -> tuple[Any, ...]:
    """Decode the records of a GET_PAGE reply, field group by field group.

    Each record holds the groups the reply echoes, with `ordinal`, the record's position in the page (the
    reply's start plus its index in the reply). Reading a group the reply did not echo raises
    HostLightingGroupNotReadError.

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
        values: dict[str, Any] = {'ordinal': header.start + index}
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
    :raises HostLightingError: for any other event, or one that does not carry one page 1 record
    """
    if event[1] != EVENT_STATE_CHANGED:
        raise HostLightingError(f"event {event[1]} is not STATE_CHANGED")
    header = decode_page_header(event)
    if header.page != PAGE_STATE or header.count != 1:
        raise HostLightingError(f"the STATE_CHANGED event carries {header.count} page {header.page} records, "
                                f"not one page {PAGE_STATE} record")
    return StateChanged(record=decode_records(event)[0], token=_u32(event, HEADER_TOKEN))


def latest_state(events: list[bytes]) -> Optional[StateChanged]:
    """Pick the newest STATE_CHANGED among the events received, which carries page 1 as it stood when sent.

    An event that does not decode is passed over.

    :param events: event reports, oldest first, as take_events() gives them
    :return: the newest STATE_CHANGED decoded, or None when there is none
    """
    for event in reversed(events):
        if len(event) > 1 and event[1] == EVENT_STATE_CHANGED:
            try:
                return decode_state_changed(event)
            except HostLightingError:
                logger.debug("passed over a STATE_CHANGED event that does not decode", exc_info=True)
    return None


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


def _check_range(name: str, value: int, high: int, low: int = 0) -> None:
    """Refuse a value outside the range its request field holds.

    :raises ValueError: naming the field and its range
    """
    if not low <= value <= high:
        raise ValueError(f"{name} {value} is outside {low} to {high}")


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
                      else f", the claim having ended under generation {holder.claim_generation}")
        name = COMMAND_NAMES.get(command)
        super().__init__(f"command 0x{command:02X}{f' ({name})' if name else ''} rejected: "
                         f"{STATUS_NAMES.get(status, hex(status))}{detail}")


class HostLightingTimeoutError(HostLightingError):
    """No reply arrived in time. The board may be busy, for example writing its settings."""


class HostLightingStaleTokenError(HostLightingError):
    """The board's state changed, so the state token a read named is out of date."""

    def __init__(self, page: int, token: int, current: int = 0):
        """Record the page, the state token that no longer matches the board, and the board's current token."""
        self.page = page
        self.token = token
        self.current = current
        super().__init__(f"page {page}: state token 0x{token:08X} is out of date (now 0x{current:08X})")


class HostLightingDisconnectedError(HostLightingError):
    """A read or a write on the HID device failed, since the board has left the bus."""


class HostLightingUnansweredError(HostLightingDisconnectedError):
    """The board is on the bus but answers nothing, so a host treats it as gone."""


class HostLightingIncompatibleError(HostLightingError):
    """The board speaks another protocol major, or lacks a capability the host requires."""


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


class HostLightingBoardSelectionError(HostLightingError):
    """open_device() found no board to open: none at all, none matching the board ID prefix, or several.

    A host can word its own message from what was found. `matched` is empty, or holds the boards the prefix left
    to choose between.
    """

    def __init__(self, message: str, board_id_prefix: str, boards: tuple[BoardListing, ...],
                 matched: tuple[BoardListing, ...], other_majors: tuple[int, ...]) -> None:
        """Record what was found.

        :param message: the library's message
        :param board_id_prefix: the board ID prefix asked for
        :param boards: a BoardListing for each protocol 2 board found
        :param matched: the boards among them the prefix matches
        :param other_majors: the usage of each interface that speaks another protocol major
        """
        self.board_id_prefix = board_id_prefix
        self.boards = boards
        self.matched = matched
        self.other_majors = other_majors
        super().__init__(message)


class HostLightingClaimedError(HostLightingError):
    """Another application holds the claim, which was neither to be taken over nor waited for.

    LightsClaim.take() raises it, and so do the conformance checks and measurements once the claim is lost.
    """

    def __init__(self, holder: Controller):
        """Record the holder, as the board showed it."""
        self.holder = holder
        super().__init__(f"'{holder.application_name}' holds the lights (claim 0x{holder.holder_token:08X}, "
                         f"generation {holder.claim_generation})")


def _sending(report: bytes) -> str:
    """Name what a failed write was sending, for its error. [0] is hidapi's report ID."""
    return f"sending command 0x{report[1]:02X}" if len(report) > 1 else "writing"


# the events a device keeps until take_events() or next_event() hands them over. Past it the oldest is dropped,
# since the subscription is shared and a host that never takes events would keep every one
EVENTS_KEPT = 64


class HidHandle(Protocol):
    """The calls the device makes on an open hidapi device, which the fake boards answer too."""

    def write(self, data: bytes) -> int:
        """Write one report, its report ID first, returning the bytes written or -1."""

    def read(self, size: int, timeout_ms: int = 0) -> Sequence[int]:
        """Read one report, or nothing when none arrives within the timeout."""

    def close(self) -> None:
        """Close the device."""


class HostLightingDevice:
    """One GP2040-CE board's Host Lighting interface."""

    def __init__(self, path: Optional[bytes] = None, handle: Optional[HidHandle] = None) -> None:
        """Open the HID device at the given hidapi path, or use a handle already open.

        :param path: platform-specific hidapi device path from enumeration
        :param handle: instead of a path, an open hidapi device set non-blocking, or another HidHandle, an
            object with its write, read and close methods, such as gp2040ce_hostlighting.testing's fake boards
        :raises ValueError: when neither a path nor a handle is given
        :raises HostLightingError: when the device does not open, as on Linux without the udev rule
        """
        if handle is None:
            if path is None:
                raise ValueError("give the device's path or an open handle")
            device = _import_hid().device()
            try:
                device.open_path(path)
            except OSError as error:
                raise HostLightingError(f"the interface will not open ({error}). On Linux, check its udev "
                                        "rule") from error
            # a read with no timeout returns at once, so request() can drop the reports already received
            device.set_nonblocking(True)
            handle = device
        self.handle: HidHandle = handle
        # the first sequence number is random, so two applications seldom send the same one
        self.sequence = secrets.randbelow(255)
        self.events: list[bytes] = []
        # the requests sent with replies awaited, with the flags each was sent with, and those replies when
        # read while waiting for another
        self._awaited: dict[tuple[int, int], int] = {}
        self._pending: list[bytes] = []
        # this application's claim token, from its last claim(), and the generation it holds the claim under,
        # 0 when it holds none. Its requests carry the generation, and FLAG_HOLDER while the claim is exclusive,
        # which that claim requires
        self.claim_token = 0
        self.generation = 0
        self.holding_exclusive = False
        self.closed = False
        # the conformance checks take every status 4 reply with a request's numbers as its reply, to judge it
        self._every_refusal = False

    def __enter__(self) -> 'HostLightingDevice':
        """Use the device in a with block, which closes it."""
        return self

    def __exit__(self, *exception: object) -> None:
        """Close the device."""
        self.close()

    def close(self) -> None:
        """Close the HID device. A second close does nothing."""
        if not self.closed:
            self.closed = True
            self.handle.close()

    def _write(self, report: bytes) -> None:
        """Write one report.

        hidapi reports a failed write by a negative result, and a closed device by ValueError.

        :raises HostLightingDisconnectedError: if the device is closed or does not take the report
        """
        if self.closed:
            raise HostLightingDisconnectedError("the device is closed")
        try:
            written = self.handle.write(report)
        except (OSError, ValueError) as error:
            raise HostLightingDisconnectedError(f"the board is no longer connected ({_sending(report)} failed with "
                                                f"{error})") from error
        if isinstance(written, int) and written < 0:
            raise HostLightingDisconnectedError(f"the board is no longer connected ({_sending(report)} failed)")

    def _read(self, timeout_ms: int) -> bytes:
        """Read one report, or nothing within the timeout.

        :param timeout_ms: how long to wait, where 0 returns at once
        :raises HostLightingDisconnectedError: if the device is closed or cannot be read
        """
        if self.closed:
            raise HostLightingDisconnectedError("the device is closed")
        try:
            return bytes(self.handle.read(REPORT_SIZE, timeout_ms))
        except (OSError, ValueError) as error:
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
            self._awaited.pop((command, sequence), None)
            raise
        return sequence

    def collect(self, seconds: float) -> list[bytes]:
        """Read every report that arrives within a time: replies, other applications' replies and events.

        :param seconds: how long to read
        :return: the reports, in arrival order
        """
        reports: list[bytes] = []
        deadline = time.monotonic() + seconds
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return reports
            report = self._read(max(1, int(left * 1000)))
            if report:
                reports.append(report)

    def wait_reply(self, command: int, sequence: int, timeout: float = 0.5,
                   accept: Optional[Callable[[bytes], bool]] = None) -> bytes:
        """Wait for the reply to one request.

        Replies reach every application with the interface open, so a report that answers none of this
        application's requests is another application's and is dropped. Events are kept in `events`, up to
        EVENTS_KEPT, and the replies to other requests still awaited, where a later wait finds them. Each reply is
        matched by its command echo and sequence number, not by order.

        A status 4 reply shows the claim's holder, and the specification's table says what it means. It answers
        this request only when that holder would refuse it, which it does when the request carried another
        non-zero generation, or when the claim shown is exclusive, the request lacked FLAG_HOLDER and that
        generation, and the claim leaves it no opening (CLAIM, and HELLO, GET_PAGE and SUBSCRIBE without
        COMMIT_AFTER or KEEPALIVE). Any other status 4 reply answers another application's request and is passed
        over. The one taken updates the generation and FLAG_HOLDER the device sends from then on.

        :param command: the request's command byte
        :param sequence: the request's sequence number
        :param timeout: seconds to wait
        :param accept: optional test a matching report must also pass (to tell two applications' replies apart)
        :return: the reply report
        """
        for report in [report for report in self._pending if match_reply(report, command, sequence)]:
            self._pending.remove(report)
            if self._ours(report, command, sequence, accept):
                return self._answered(report, command, sequence)
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            # a read that blocks until a report or the timeout, with no polling sleep, which is ~16 ms on Windows
            # before Python 3.11
            report = self._read(max(1, int(left * 1000)))
            if not report:
                continue
            if not match_reply(report, command, sequence):
                self._keep(report)
            elif self._ours(report, command, sequence, accept):
                return self._answered(report, command, sequence)
            # otherwise another application's reply with this request's numbers: dropped
        self._awaited.pop((command, sequence), None)
        raise HostLightingTimeoutError(f"no reply to command 0x{command:02X} within {timeout} s")

    def _ours(self, report: bytes, command: int, sequence: int, accept: Optional[Callable[[bytes], bool]]) -> bool:
        """Say whether a report with this request's numbers is this application's reply."""
        if accept is not None and not accept(report):
            return False
        if report[2] != STATUS_CLAIMED or len(report) < 3 + CONTROLLER_SIZE or self._every_refusal:
            return True
        # status 4 is this request's only when the holder shown would refuse it, as the firmware decides
        holder = decode_controller(bytes(report[3:3 + CONTROLLER_SIZE]))
        flags = self._awaited.get((command, sequence), 0)
        sent_generation = (flags & FLAG_GENERATION) >> FLAG_GENERATION_SHIFT
        if command == CMD_CLAIM:
            return False        # CLAIM is never refused with status 4
        if sent_generation and sent_generation != holder.claim_generation:
            return True         # sent under a generation no longer current
        guarded = holder.holder_token and holder.exclusive and not (
            flags & FLAG_HOLDER and sent_generation == holder.claim_generation)
        open_unflagged = command in (CMD_HELLO, CMD_GET_PAGE, CMD_SUBSCRIBE) and not flags & (
            FLAG_COMMIT_AFTER | FLAG_KEEPALIVE)
        return bool(guarded) and not open_unflagged

    def _answered(self, report: bytes, command: int, sequence: int) -> bytes:
        """Take a reply as this request's, acting on the holder a status 4 reply shows."""
        self._awaited.pop((command, sequence), None)
        if report[2] == STATUS_CLAIMED and len(report) >= 3 + CONTROLLER_SIZE:
            holder = decode_controller(bytes(report[3:3 + CONTROLLER_SIZE]))
            if holder.held_by(self.claim_token):
                self.generation, self.holding_exclusive = holder.claim_generation, holder.exclusive
            else:
                self.generation, self.holding_exclusive = 0, False
        return report

    def _await(self, command: int, sequence: int, flags: int) -> None:
        """Note a request that will be answered. No report kept for an earlier one with its numbers answers it."""
        if not flags & FLAG_NO_REPLY and 0 < command < REPLY_FLAG:
            self._pending = [report for report in self._pending if not match_reply(report, command, sequence)]
            self._awaited[(command, sequence)] = flags

    def _keep(self, report: bytes) -> None:
        """Keep an event, the oldest dropped past EVENTS_KEPT, or the reply to a request still awaited."""
        if is_event(report):
            if len(self.events) >= EVENTS_KEPT:
                del self.events[0]
            self.events.append(report)
        elif len(report) >= 3 and (report[0] & ~REPLY_FLAG, report[1]) in self._awaited:
            self._pending.append(report)

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

    def take_events(self) -> list[bytes]:
        """Hand over the events received so far, oldest first, draining first.

        :return: the event reports, which are then forgotten here
        """
        self.drain()
        events, self.events = self.events, []
        return events

    def request(self, command: int, payload: bytes = b'', flags: int = 0, timeout: float = 0.5,
                accept: Optional[Callable[[bytes], bool]] = None) -> bytes:
        """Send one request and wait for its reply.

        Every application's replies reach this one and wait until read, so the reports already received are
        read first and dropped, but for events and the replies to earlier send() calls still awaited. Another
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
                   accept: Optional[Callable[[bytes], bool]] = None) -> bytes:
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
            return HostLightingRejectedError(command, reply[2], reply[3] if len(reply) > 3 else 0)
        return HostLightingRejectedError(command, reply[2], 0, decode_controller(bytes(reply[3:3 + CONTROLLER_SIZE])))

    def keepalive(self) -> None:
        """Send HELLO with KEEPALIVE and NO_REPLY: the keepalive of a host that does not claim."""
        self.send(CMD_HELLO, flags=FLAG_KEEPALIVE | FLAG_NO_REPLY)

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

        A reply to another application's request with this one's numbers is told apart by its echoed page, start
        and field groups, and by the state token. A request that names none is never stale, and one that does
        is answered under that token or shown another.

        :param page: page number
        :param start: first record
        :param mask: field groups to return, or 0 for every group
        :param token: expected state token, 0 for no check
        :return: the reply report, with status 0 or 3 (STALE_TOKEN)
        :raises HostLightingRejectedError: on any other status
        :raises ValueError: for a value its field cannot hold
        """
        _check_range('page', page, 0xFF)
        _check_range('start', start, 0xFFFF)
        _check_range('mask', mask, 0xFFFF)
        _check_range('token', token, 0xFFFFFFFF)

        def echoes(reply: bytes) -> bool:
            if reply[2] not in (STATUS_OK, STATUS_STALE_TOKEN):
                return True
            header = decode_page_header(reply)
            if header.page != page or header.start != start:
                return False
            if reply[2] == STATUS_STALE_TOKEN:
                return bool(token) and header.token != token
            if token and header.token != token:
                return False
            if page not in PAGE_GROUPS:
                return not mask or not header.groups & ~mask        # page 0: the strings that fit
            # a table page returns every group asked for that it defines, and with mask 0 a later 2.x board's too
            known = (1 << len(PAGE_GROUPS[page])) - 1
            return header.groups & known == (mask or known) & known and not (mask and header.groups & ~mask)

        reply = self.request(CMD_GET_PAGE, struct.pack('<BHIH', page, start, token, mask), accept=echoes)
        if reply[2] not in (STATUS_OK, STATUS_STALE_TOKEN):
            raise self.rejected(CMD_GET_PAGE, reply)
        return reply

    @overload
    def read_table(self, page: Literal[1], mask: int = 0, token: int = 0,
                   attempts: int = 5) -> PageRead[StateRecord]: ...

    @overload
    def read_table(self, page: Literal[2], mask: int = 0, token: int = 0,
                   attempts: int = 5) -> PageRead[InputModeRecord]: ...

    @overload
    def read_table(self, page: Literal[8], mask: int = 0, token: int = 0,
                   attempts: int = 5) -> PageRead[SummaryRecord]: ...

    @overload
    def read_table(self, page: Literal[9], mask: int = 0, token: int = 0,
                   attempts: int = 5) -> PageRead[ProfileRecord]: ...

    @overload
    def read_table(self, page: Literal[10], mask: int = 0, token: int = 0,
                   attempts: int = 5) -> PageRead[ControlRecord]: ...

    @overload
    def read_table(self, page: Literal[11], mask: int = 0, token: int = 0,
                   attempts: int = 5) -> PageRead[LightRecord]: ...

    @overload
    def read_table(self, page: Literal[12], mask: int = 0, token: int = 0,
                   attempts: int = 5) -> PageRead[AnimationRecord]: ...

    @overload
    def read_table(self, page: int, mask: int = 0, token: int = 0, attempts: int = 5) -> PageRead[Any]: ...

    def read_table(self, page: int, mask: int = 0, token: int = 0, attempts: int = 5) -> PageRead[Any]:
        """Read every record of a table page as one coherent snapshot.

        Without a token, the walk names the state token of its first reply in every later read. If the board's
        state changes mid-walk, the board answers STALE_TOKEN and the walk starts again, so the records never mix
        two states. With a token, a host that read page 1 or 8 under it reads under the same token, so
        everything it holds describes one state, and a change raises HostLightingStaleTokenError instead. A mask
        that names no group the board defines reads no records.

        :param page: page number
        :param mask: field groups to return, or 0 for every group
        :param token: the state token to read under, or 0 to adopt the first reply's
        :param attempts: without a token, how many times to start the walk before giving up
        :return: the decoded records, of the page's record type, and the state token they carry
        :raises HostLightingStaleTokenError: with a token, if the board's state no longer matches it
        :raises HostLightingError: if the state keeps changing or the board stops making progress
        """
        if token:
            walked = self._walk_table(page, mask, token)
            if isinstance(walked, int):
                raise HostLightingStaleTokenError(page, token, walked)
            return walked
        for _ in range(attempts):
            walked = self._walk_table(page, mask, 0)
            if not isinstance(walked, int):
                return walked
        raise HostLightingError(f"page {page} kept changing during {attempts} walks")

    def _walk_table(self, page: int, mask: int, token: int) -> PageRead[Any] | int:
        """Walk a table page from its first record, adopting the first reply's token when token is 0.

        :return: the decoded records and their state token, or the board's current token when it answers
            STALE_TOKEN
        """
        records: list[Any] = []
        while True:
            reply = self.get_page(page, len(records), mask, token)
            header = decode_page_header(reply)
            if reply[2] == STATUS_STALE_TOKEN:
                return header.token
            token = header.token
            if header.count == 0:
                # a mask naming no group the board defines returns the total, no records and no groups
                if len(records) < header.total and header.groups:
                    raise HostLightingError(f"page {page} stalled at {len(records)} of {header.total} records")
                return PageRead(records=tuple(records), token=token)
            records.extend(decode_records(reply))
            if len(records) >= header.total:
                return PageRead(records=tuple(records), token=token)

    @overload
    def read_record(self, page: Literal[1], mask: int = 0) -> PageRead[StateRecord]: ...

    @overload
    def read_record(self, page: Literal[8], mask: int = 0) -> PageRead[SummaryRecord]: ...

    @overload
    def read_record(self, page: int, mask: int = 0) -> PageRead[Any]: ...

    def read_record(self, page: int, mask: int = 0) -> PageRead[Any]:
        """Read a one-record page (1 or 8).

        :param page: page number
        :param mask: field groups to return, or 0 for every group
        :return: the one record, of the page's record type, and the state token
        """
        reply = self.get_page(page, 0, mask)
        return PageRead(records=decode_records(reply), token=decode_page_header(reply).token)

    def read_identity(self, mask: int = 0) -> Identity:
        """Read page 0's strings, asking separately for the groups that did not fit.

        :param mask: the groups wanted, bit n for Identity's nth field, or 0 for all eight
        :return: the strings read, None for the others
        """
        strings: dict[str, str] = {}
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
        :raises ValueError: for a name over 16 bytes, an action over 255 or a token outside 1 to 0xFFFFFFFF
        """
        _check_claim_name(name)
        _check_range('action', action, 0xFF)
        _check_range('token', token, 0xFFFFFFFF, 1)
        self.claim_token = token
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

    def configure(self, takeover: int = TAKEOVER_WHOLE_FRAME, keepalive_ms: int = 0,
                  brightness_applied: bool = True) -> Settings:
        """Set the takeover mode, keepalive timeout and brightness scaling.

        :param takeover: TAKEOVER_WHOLE_FRAME or TAKEOVER_OVERLAY
        :param keepalive_ms: keepalive timeout in ms, 0 for the default 2000, clamped to 100-10000
        :param brightness_applied: scale host pixels by the board's brightness
        :return: the settings the board applied
        :raises ValueError: for a takeover mode over 255 or a timeout over 65535
        """
        _check_range('takeover', takeover, 0xFF)
        _check_range('keepalive_ms', keepalive_ms, 0xFFFF)
        reply = self.request_ok(CMD_CONFIGURE, struct.pack('<BHB', takeover, keepalive_ms, int(brightness_applied)))
        return _settings(bytes(reply[3:7]))

    def subscribe(self, events: Iterable[int]) -> frozenset[int]:
        """Enable events. The subscription is shared by every application on the board, so this only adds.

        :param events: the event numbers to enable, EVENT_STATE_CHANGED among them
        :return: the events now enabled
        :raises ValueError: for an event number outside 0-15
        """
        wanted = set(events)
        for event in wanted:
            _check_range('event', event, 15)
        mask = sum(1 << event for event in wanted)
        return frozenset(_bits(_u16(self.request_ok(CMD_SUBSCRIBE, struct.pack('<H', mask)), 3)))

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

    def stage_entries(self, addressing: int, entries: Sequence[tuple[int, tuple[int, ...]]],
                      pixel_format: int = PIXEL_RGB,
                      commit: bool = False) -> list[StageOutcome]:
        """Stage entries addressed by light, pin, action or kind, chunked to the report capacity.

        Each report is answered, for its outcome mask.

        :param addressing: ADDRESSING_LIGHT, ADDRESSING_PIN, ADDRESSING_ACTION or ADDRESSING_KIND
        :param entries: (address, colour) pairs, each colour as for encode_pixel
        :param pixel_format: PIXEL_RGB, PIXEL_RGBW or PIXEL_RGB565
        :param commit: publish the staged frame with the last report (COMMIT_AFTER), or with COMMIT when there
            are no entries
        :return: a StageOutcome per report sent
        """
        return self._entries(CMD_STAGE, stage_entry_payloads(addressing, entries, pixel_format), commit)

    def stage_run(self, addressing: int, first: int, colours: Sequence[tuple[int, ...]],
                  pixel_format: int = PIXEL_RGB,
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
        return self._run(CMD_STAGE, stage_run_payloads(addressing, first, colours, pixel_format), commit)

    def unstage_entries(self, addressing: int, addresses: Sequence[int],
                        commit: bool = False) -> list[StageOutcome]:
        """Unstage the lights addressed by light, pin, action or kind, chunked to the report capacity.

        Each LED reached becomes as after CLEAR, unstaged and off. Each report is answered, for its outcome mask.

        :param addressing: ADDRESSING_LIGHT, ADDRESSING_PIN, ADDRESSING_ACTION or ADDRESSING_KIND
        :param addresses: the light ordinals, pins, actions or kinds
        :param commit: publish the staged frame with the last report (COMMIT_AFTER), or with COMMIT when there
            are no addresses
        :return: a StageOutcome per report sent
        """
        return self._entries(CMD_UNSTAGE, unstage_entry_payloads(addressing, addresses), commit)

    def unstage_run(self, addressing: int, first: int, count: int, commit: bool = False) -> StageOutcome:
        """Unstage consecutive LEDs or lights from a first index. Every report but the last goes with NO_REPLY.

        :param addressing: ADDRESSING_LED_RUN or ADDRESSING_LIGHT_RUN
        :param first: the first LED index or light ordinal
        :param count: how many to unstage
        :param commit: publish the staged frame with the last report (COMMIT_AFTER)
        :return: the last report's StageOutcome
        """
        return self._run(CMD_UNSTAGE, unstage_run_payloads(addressing, first, count), commit)

    def _entries(self, command: int, payloads: list[bytes], commit: bool) -> list[StageOutcome]:
        """Send STAGE or UNSTAGE entry reports, each answered, COMMIT_AFTER on the last when committing.

        With no entries nothing is staged, and committing sends COMMIT.
        """
        if not payloads and commit:
            self.commit()
        outcomes = []
        for index, payload in enumerate(payloads):
            last = index == len(payloads) - 1
            reply = self.request_ok(command, payload, FLAG_COMMIT_AFTER if commit and last else 0)
            outcomes.append(StageOutcome(applied=reply[3], skipped=reply[4], outcome=_u32(reply, 5)))
        return outcomes

    def _run(self, command: int, payloads: list[bytes], commit: bool) -> StageOutcome:
        """Send STAGE or UNSTAGE run reports, all but the last with NO_REPLY, COMMIT_AFTER on the last."""
        for payload in payloads[:-1]:
            self.send(command, payload, FLAG_NO_REPLY)
        reply = self.request_ok(command, payloads[-1], FLAG_COMMIT_AFTER if commit else 0)
        return StageOutcome(applied=reply[3], skipped=reply[4], outcome=_u32(reply, 5))

    def fill(self, colour: tuple[int, ...], pixel_format: int = PIXEL_RGB, commit: bool = False) -> None:
        """Stage one colour on every LED below the addressable LED limit.

        :param colour: as for encode_pixel
        :param pixel_format: PIXEL_RGB, PIXEL_RGBW or PIXEL_RGB565
        :param commit: publish the staged frame (COMMIT_AFTER)
        """
        self.request_ok(CMD_FILL, bytes([pixel_format << 4]) + encode_pixel(colour, pixel_format),
                        FLAG_COMMIT_AFTER if commit else 0)

    def clear(self, commit: bool = False) -> None:
        """Unstage every pixel and set it off.

        :param commit: publish the cleared frame (COMMIT_AFTER), which an empty frame needs to show
        """
        self.request_ok(CMD_CLEAR, flags=FLAG_COMMIT_AFTER if commit else 0)

    def commit(self) -> None:
        """Publish the staged frame, starting a takeover when none is live."""
        self.request_ok(CMD_COMMIT)

    def release(self) -> None:
        """Return the LEDs to the on-board animations and clear the staged frame. A claim survives it."""
        self.request_ok(CMD_RELEASE)

    def set_profile(self, number: int) -> None:
        """Select an input profile, applied live and saved, as the profile hotkeys do.

        The board writes its settings flash for a new profile, and answers nothing for about 200 ms meanwhile.

        :param number: a page 9 profile number
        """
        self.request_ok(CMD_SET_PROFILE, bytes([number]))

    def set_animation(self, index: Optional[int]) -> None:
        """Select the on-board animation, applied live and saved about a second later.

        :param index: a page 12 ordinal, or None or ANIMATION_OFF for off
        :raises ValueError: for an index over 0xFFFF
        """
        if index is not None:
            _check_range('animation', index, 0xFFFF)
        self.request_ok(CMD_SET_ANIMATION, struct.pack('<H', ANIMATION_OFF if index is None else index))

    def set_animation_speed(self, index: int, idle: Optional[int] = None, pressed: Optional[int] = None,
                            case: Optional[int] = None) -> None:
        """Set one on-board animation's speeds, applied live and saved about a second later.

        :param index: a page 12 ordinal
        :param idle: the idle speed, or None to leave it
        :param pressed: the pressed speed, or None to leave it
        :param case: the case speed, or None to leave it
        :raises ValueError: for an index over 0xFFFF or a speed over 255
        """
        _check_range('animation', index, 0xFFFF)
        speeds = [SPEED_UNCHANGED if speed is None else speed for speed in (idle, pressed, case)]
        for speed in speeds:
            _check_range('speed', speed, 0xFF)
        self.request_ok(CMD_SET_ANIMATION_SPEED, struct.pack('<HBBB', index, *speeds))

    def set_brightness(self, step: int, save: bool = False) -> None:
        """Show a brightness step, unsaved by default, which writes nothing to flash.

        An unsaved step lasts while it is kept alive as a takeover is, and ends at a change of holder. A saved step
        is written about a second later.

        :param step: 0 to the Summary's brightness steps, or BRIGHTNESS_SAVED for the saved step
        :param save: save the step, as the brightness hotkeys do
        """
        self.request_ok(CMD_SET_BRIGHTNESS, bytes([step, int(save)]))

    def set_input_mode(self, mode: int, allow_no_lighting: bool = False) -> bool:
        """Save an input mode and reboot the board into it.

        By default page 2 is read first and a mode without the lighting interface is refused, since no host can
        switch the board back from it. A mode page 2 does not list is sent, and the board refuses it.

        :param mode: an InputMode value, as page 2 lists them
        :param allow_no_lighting: switch into a mode without the lighting interface too
        :return: True when the board acknowledged, False when it left the bus before the reply was read
        :raises ValueError: for a listed mode without the lighting interface, unless allowed
        """
        if not allow_no_lighting:
            listed = {record.mode.value: record for record in self.read_table(PAGE_INPUT_MODES, 0b1).records}
            if mode in listed and not listed[mode].mode.lighting:
                raise ValueError(f"input mode {mode} has no lighting interface, and no host can switch the board "
                                 "back from it. Pass allow_no_lighting=True to switch anyway")
        return self._reboot(CMD_SET_INPUT_MODE, bytes([mode]) + MAGIC_INPUT_MODE)

    def reboot(self, target: int) -> bool:
        """Reboot the board.

        :param target: REBOOT_GAMEPAD into the saved input mode, REBOOT_WEBCONFIG or REBOOT_BOOTSEL
        :return: True when the board acknowledged, False when it left the bus before the reply was read
        """
        return self._reboot(CMD_REBOOT, bytes([target]) + MAGIC_REBOOT)

    def _reboot(self, command: int, payload: bytes) -> bool:
        """Send SET_INPUT_MODE or REBOOT, tolerating the board leaving the bus.

        The reply is sent before the reboot, but the board may leave before it is read. A vanished device or a
        missing reply means the reboot is under way. A refusal still raises. A host confirms the reboot by the
        board enumerating again.
        """
        try:
            self.request_ok(command, payload)
            return True
        except HostLightingRejectedError:
            raise
        except HostLightingError:
            logger.debug("no acknowledgement before the disconnect. The reboot is proceeding")
            return False


def _import_hid() -> Any:
    """Import the hidapi module, with a helpful error if it is missing.

    On Linux it is hidapi's hidraw backend. The hid module of hidapi 0.15.0's Linux wheel uses libusb (measured),
    which reports no usage page, so it finds no board, and claims the interface for one application.
    """
    try:
        if sys.platform.startswith('linux'):
            try:
                import hidraw
                return hidraw
            except ImportError:
                pass            # a hidapi built without the hidraw module
        import hid
    except ImportError as error:
        raise HostLightingError("the hidapi package is needed. Install it with pip install hidapi") from error
    return hid


def find_devices() -> list[dict[str, Any]]:
    """Enumerate the Host Lighting interfaces of protocol 2 on the system.

    :return: list of hidapi enumeration dicts for matching interfaces
    """
    hid = _import_hid()
    return [info for info in hid.enumerate()
            if info.get('usage_page') == USAGE_PAGE and info.get('usage') == USAGE]


def find_other_majors() -> list[int]:
    """Find the Host Lighting interfaces that speak another protocol major, without opening them.

    The usage is the protocol major, so the usage page alone finds them. Pre-release protocol 1.x firmware
    presents USAGE_PROTOCOL_1.

    :return: the usage each such interface presents
    """
    hid = _import_hid()
    return [info.get('usage') for info in hid.enumerate()
            if info.get('usage_page') == USAGE_PAGE and info.get('usage') != USAGE]


def _none_answered(count: int) -> str:
    """Say that interfaces were found, but none opened and answered HELLO as a protocol 2 board."""
    return (f"{count} Host Lighting interface(s) found, but none opened and answered as a protocol "
            f"{PROTOCOL_MAJOR} board. On Linux, check the udev rule")


def _no_interface(usages: tuple[int, ...]) -> str:
    """Say that no protocol 2 interface was found, naming the boards of another major there are."""
    note = ''
    if usages:
        note = (f" {len(usages)} interface(s) speak another protocol major" +
                (", among them pre-release protocol 1.x firmware" if USAGE_PROTOCOL_1 in usages else '') + '.')
    return (f"no Host Lighting interface of protocol {PROTOCOL_MAJOR} found.{note} Connect a board with the add-on "
            "enabled")


def _probe(info: dict[str, Any]) -> Optional[tuple[Hello, HostLightingDevice]]:
    """Open one interface and keep it when it answers HELLO as a protocol 2 board.

    :param info: a hidapi enumeration dict
    :return: (its HELLO, the opened device), or None, with the device closed
    """
    try:
        device = HostLightingDevice(info['path'])
    except HostLightingError:
        logger.warning("skipping an interface that will not open. On Linux, check its udev rule")
        logger.debug("opening %s failed", info['path'], exc_info=True)
        return None
    try:
        hello = device.hello()
    except (HostLightingError, OSError):
        device.close()
        return None
    if hello.major == PROTOCOL_MAJOR:
        return hello, device
    logger.warning("skipping a board speaking protocol %d.%d. This library speaks %d.x",
                   hello.major, hello.minor, PROTOCOL_MAJOR)
    device.close()
    return None


def _listing(hello: Hello, device: HostLightingDevice) -> BoardListing:
    """List one opened board by its HELLO and page 0, then close it."""
    try:
        identity = _label(device, hello)
    finally:
        device.close()
    return BoardListing(board_id=hello.board_id, board_label=identity.board_label,
                        firmware_version=identity.firmware_version, major=hello.major, minor=hello.minor)


def _label(device: HostLightingDevice, hello: Hello) -> Identity:
    """Read a board's label and firmware version from page 0, nothing when it has no page 0 or fails."""
    if PAGE_IDENTITY not in hello.pages:
        return Identity()
    try:
        return device.read_identity(1 << _IDENTITY_NAMES.index('board_label') |
                                    1 << _IDENTITY_NAMES.index('firmware_version'))
    except HostLightingError:
        logger.debug("reading page 0 failed", exc_info=True)
        return Identity()


def open_device(board_id_prefix: str = '') -> HostLightingDevice:
    """Open a board's Host Lighting interface, choosing by board ID when several are connected.

    A board is bound by the factory-unique ID its HELLO reply gives, never by its device path, which changes
    with the USB port.

    :param board_id_prefix: optional hex prefix of the HELLO factory board ID
    :return: an opened HostLightingDevice
    :raises HostLightingBoardSelectionError: when no board, or more than one, matches
    """
    infos = find_devices()
    if not infos:
        usages = tuple(find_other_majors())
        raise HostLightingBoardSelectionError(_no_interface(usages), board_id_prefix, (), (), usages)
    found = [probe for probe in (_probe(info) for info in infos) if probe]
    if not found:
        raise HostLightingBoardSelectionError(_none_answered(len(infos)), board_id_prefix, (), (), ())
    matching = [hello.board_id for hello, _ in found if hello.board_id.startswith(board_id_prefix.upper())]
    if len(matching) == 1:
        for hello, device in found:
            if hello.board_id != matching[0]:
                device.close()
        return next(device for hello, device in found if hello.board_id == matching[0])
    boards = tuple(_listing(hello, device) for hello, device in found)
    matched = tuple(board for board in boards if board.board_id in matching)
    if not matched:
        message = f"no board speaking protocol {PROTOCOL_MAJOR}.x matches ID prefix '{board_id_prefix}'"
    else:
        named = ', '.join(f"{board.board_id} ({board.board_label})" if board.board_label else board.board_id
                          for board in matched)
        message = f"multiple boards found: {named}. Choose one by a board ID prefix"
    raise HostLightingBoardSelectionError(message, board_id_prefix, boards, matched, ())


def find_board(board_id: str) -> Optional[HostLightingDevice]:
    """Open the board with this whole factory board ID, if it is connected.

    :param board_id: the HELLO factory board ID, 16 hex digits in either case
    :return: an opened HostLightingDevice, or None when no connected protocol 2 board has that ID
    :raises ValueError: for an ID that is not 16 digits
    """
    if len(board_id) != 16:
        raise ValueError("find_board needs the whole 16-digit board ID. open_device takes a prefix")
    for info in find_devices():
        found = _probe(info)
        if found and found[0].board_id == board_id.upper():
            return found[1]
        if found:
            found[1].close()
    return None


def list_boards() -> list[BoardListing]:
    """Identify every connected protocol 2 board from HELLO and page 0, claiming none of them.

    :return: a BoardListing per board, with None for a label or firmware version the board did not give
    """
    boards = []
    for info in find_devices():
        found = _probe(info)
        if not found:
            continue
        boards.append(_listing(*found))
    return boards


# how a capability of each HELLO bitmap is named, and the names of the members the protocol defines
_CAPABILITY_LABELS = {
    'commands': ('command 0x{:02X}', COMMAND_NAMES),
    'request_flags': ('request flag bit {}', {}),
    'pages': ('page {}', {}),
    'addressing_modes': ('addressing mode {}', ADDRESSING_NAMES),
    'pixel_formats': ('pixel format {}', PIXEL_NAMES),
    'events': ('event {}', {EVENT_STATE_CHANGED: 'STATE_CHANGED'}),
}


def _capability(bitmap: str, member: int) -> str:
    """Name one capability, such as "command 0x11 (UNSTAGE)"."""
    label, names = _CAPABILITY_LABELS[bitmap]
    return label.format(member) + (f" ({names[member]})" if member in names else '')


def missing_capabilities(hello: Hello, requirements: Requirements) -> list[str]:
    """Name each capability a host requires that a board's HELLO does not list.

    A host gates on HELLO's bitmaps and never on the minor version.

    :param hello: the board's HELLO
    :param requirements: what the host uses
    :return: the capabilities missing, each named, such as "command 0x11 (UNSTAGE)", or none
    """
    return [_capability(item.name, member) for item in fields(requirements)
            for member in sorted(getattr(requirements, item.name) - getattr(hello, item.name))]


def require_capabilities(hello: Hello, requirements: Requirements) -> None:
    """Refuse a board of another major, or one that lacks a capability a host requires.

    :param hello: the board's HELLO
    :param requirements: what the host uses
    :raises HostLightingIncompatibleError: naming what is missing
    """
    if hello.major != PROTOCOL_MAJOR:
        raise HostLightingIncompatibleError(f"the board speaks protocol {hello.major}.{hello.minor}, and this "
                                            f"library speaks {PROTOCOL_MAJOR}.x")
    missing = missing_capabilities(hello, requirements)
    if missing:
        raise HostLightingIncompatibleError(f"the board speaks protocol {hello.major}.{hello.minor} and does not "
                                            "offer " + ', '.join(missing))
