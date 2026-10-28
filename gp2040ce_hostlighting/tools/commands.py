# gp2040ce-hostlighting (GP2040-CE Host Lighting library and tools)
# Filename: commands.py
# Folder: gp2040ce_hostlighting/tools
# https://github.com/djGLiTCH/gp2040ce-hostlighting
# SPDX-FileCopyrightText: © 2026 Jacob Simpson (DJ GLiTCH)
# SPDX-License-Identifier: GPL-3.0-or-later
# See LICENSES/GPL-3.0-or-later.txt at the repository root.

"""The hlp-* commands for GP2040-CE's Host Lighting add-on, built on the protocol module.

hlp-ping, hlp-caps, hlp-settings, hlp-watch, hlp-fill, hlp-set-light, hlp-stream, hlp-show, hlp-buttons,
hlp-input-mode and hlp-reboot. Given GP2040-CE's .proto files (`-P`), they print enum names beside the values. The
commands that drive the LEDs hold a LightsClaim and print its INFO lines as part of their output.
tools.conformance shares hlp_command, device_parser, add_claim_arguments and ranged.
"""
import argparse
import colorsys
import functools
import json
import logging
import math
import os
import pathlib
import re
import sys
import time
from dataclasses import fields
from importlib import metadata
from typing import Any, Callable, Optional, Sequence

from gp2040ce_hostlighting.claim import ClaimState, LightsClaim
from gp2040ce_hostlighting.enums import read_enum_names
from gp2040ce_hostlighting.frames import Frame, publish_frame
from gp2040ce_hostlighting.protocol import (ADDRESS_RANGES, ADDRESSING_ACTION, ADDRESSING_KIND, ADDRESSING_LED_RUN,
                                            ADDRESSING_LIGHT, ADDRESSING_LIGHT_RUN, ADDRESSING_PIN, ANIMATION_OFF,
                                            BRIGHTNESS_SAVED, CMD_HELLO, EVENT_STATE_CHANGED, KIND_BUTTON, KIND_CASE,
                                            KIND_PLAYERS, KIND_TURBO, PAGE_ANIMATIONS, PAGE_CONTROLS, PAGE_IDENTITY,
                                            PAGE_INPUT_MODES, PAGE_KEYBOARD_MAPPING, PAGE_LIGHTS, PAGE_NAMES,
                                            PAGE_PROFILES, PAGE_STATE, PAGE_SUMMARY, PIXEL_RGB, PIXEL_RGBW,
                                            REBOOT_BOOTSEL, REBOOT_GAMEPAD, REBOOT_WEBCONFIG, SPEED_UNCHANGED,
                                            STATUS_CLAIMED, TAKEOVER_OVERLAY, TAKEOVER_WHOLE_FRAME,
                                            HostLightingClaimedError, HostLightingDevice, HostLightingError,
                                            HostLightingRejectedError, HostLightingTimeoutError,
                                            HostLightingUnansweredError, decode_state_changed, key_name, open_device,
                                            stage_capacity)
from gp2040ce_hostlighting.records import ControlRecord, Hello, Identity, LightRecord, Settings, StateRecord, jsonable

logger = logging.getLogger(__name__)


def version() -> str:
    """Name the installed gp2040ce-hostlighting version."""
    try:
        return metadata.version('gp2040ce-hostlighting')
    except metadata.PackageNotFoundError:
        return 'unknown'


# The options every hlp-* command takes. CommandParser fills common_options when a command line is parsed, so
# importing the package changes nothing in the program that imports it.
common_parser = argparse.ArgumentParser(add_help=False)
common_parser.add_argument('-v', '--version', action='version',
                           version=f"gp2040ce-hostlighting {version()} (Python {sys.version})",
                           help="show version information and exit")
common_parser.add_argument('-d', '--debug', action='store_true', help="enable debug logging")
common_parser.add_argument('-P', '--proto-files-path', type=pathlib.Path, action='append',
                           help="GP2040-CE's proto directory or its enums.proto, to print enum names beside the "
                                "values, and may be given more than once")
common_options = argparse.Namespace(debug=False, proto_files_path=[])


class _ClaimLines(logging.Handler):
    """Print a LightsClaim's INFO lines, what changed and the give up, as part of a command's output."""

    def emit(self, record: logging.LogRecord) -> None:
        """Print one INFO line. The other levels go to the log alone."""
        if record.levelno == logging.INFO:
            print(record.getMessage())


def _not_claim_line(record: logging.LogRecord) -> bool:
    """Leave the claim's INFO lines, which _ClaimLines prints, out of the log, so that -d shows each once."""
    return not (record.name == 'gp2040ce_hostlighting.claim' and record.levelno == logging.INFO)


class CommandParser(argparse.ArgumentParser):
    """An hlp-* command's parser, which applies the common options once the command line is parsed."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Build the parser.

        An argument that starts with a minus and a digit, such as the entry "-10:00FF00", is a value, as
        Python 3.14's argparse takes it. Earlier versions take it for an option.
        """
        super().__init__(*args, **kwargs)
        self._negative_number_matcher = re.compile(r'-\.?\d')

    def parse_known_args(self, args: Optional[Sequence[str]] = None,  # type: ignore[override]
                         namespace: Optional[argparse.Namespace] = None) -> tuple[argparse.Namespace, list[str]]:
        """Parse the command line, then set up logging and the .proto paths.

        The log goes to stderr, at WARNING or with -d at DEBUG. The claim's INFO lines are printed instead.
        """
        parsed, unknown = super().parse_known_args(args, namespace)
        common_options.debug = parsed.debug
        common_options.proto_files_path = parsed.proto_files_path or []
        level = logging.DEBUG if parsed.debug else logging.WARNING
        if not logging.root.handlers:
            logging.basicConfig(level=level, stream=sys.stderr,
                                format='%(asctime)s %(levelname)8s [%(name)s] %(message)s')
            logging.root.handlers[0].setLevel(level)
            logging.root.handlers[0].addFilter(_not_claim_line)
        claim_log = logging.getLogger('gp2040ce_hostlighting.claim')
        claim_log.setLevel(min(level, logging.INFO))
        if not any(isinstance(handler, _ClaimLines) for handler in claim_log.handlers):
            claim_log.addHandler(_ClaimLines())
        return parsed, unknown


def enum_names(device: Optional[HostLightingDevice] = None) -> dict[str, dict[int, str]]:
    """Name GP2040-CE's enum values from its enums.proto given with -P, else from the board's page 3.

    -P takes the file or the directory that holds it. A directory without enums.proto is skipped.

    :param device: the board, with page 3 naming its actions and light kinds when -P names nothing
    :return: {enum name: {value: value name}}, empty when neither gives a name
    """
    names = read_enum_names(common_options.proto_files_path)
    if common_options.proto_files_path and not names:
        logger.warning("no enum names found in the -P paths")
    if not names and device is not None and PAGE_NAMES in device.hello().pages:
        names = device.read_names()
    return names


def _named(value: int, names: dict[str, dict[int, str]], enum: str) -> str:
    """Show a GP2040-CE enum value verbatim, with its name when known."""
    name = names.get(enum, {}).get(value)
    return f"{value} ({name})" if name else str(value)


def hlp_command(function: Callable[[], None]) -> Callable[[], None]:
    """Report a command's HID and protocol errors as a one-line exit, and Ctrl-C as exit 130, not a traceback."""
    @functools.wraps(function)
    def run() -> None:
        """Run the command."""
        try:
            function()
        except KeyboardInterrupt:
            raise SystemExit(130) from None     # the claim was given up and the device closed on the way out
        except HostLightingClaimedError as error:
            raise SystemExit(f"error: {error}. Add --force to take them over") from error
        except BrokenPipeError:
            # The reader of the output has exited. Python flushes stdout again at exit, so it is pointed at
            # devnull first, by the method of the note on SIGPIPE in Python's signal module documentation.
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
            raise SystemExit(1) from None
        except (HostLightingError, OSError) as error:
            raise SystemExit(f"error: {error}") from error
    return run


def device_parser(description: str) -> argparse.ArgumentParser:
    """Build an argument parser with the common device-selection flag.

    :param description: help text for the command
    :return: an ArgumentParser with core and device-selection arguments
    """
    parser = CommandParser(description=description, parents=[common_parser])
    parser.add_argument('--board-id', default='',
                        help="hex prefix of the factory board ID, to select one of several connected boards")
    return parser


def add_claim_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the flags of a command that holds the lights: --force and --exclusive."""
    parser.add_argument('--force', action='store_true', help="take the lights over from another application")
    parser.add_argument('--exclusive', action='store_true',
                        help="claim the lights exclusively, so that until the claim is given up the board refuses "
                             "changes from applications that do not hold the claim (default: a shared claim)")


def ranged(low: int, high: int) -> Callable[[str], int]:
    """Build an argparse type for an integer from low to high, decimal or 0x hex."""
    def parse(text: str) -> int:
        """Parse and range-check one value."""
        try:
            value = int(text, 0)
        except ValueError:
            raise argparse.ArgumentTypeError(f"not a number: {text}") from None
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"{text} is outside {low}-{high}")
        return value
    return parse


def _duration(text: str) -> float:
    """Parse a number of seconds above 0, for a command that streams frames for that long."""
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text}") from None
    if not (math.isfinite(value) and value > 0):
        raise argparse.ArgumentTypeError(f"{text} is not a number of seconds above 0")
    return value


def _animation(text: str) -> int:
    """Parse an animation ordinal, or 'off'."""
    return ANIMATION_OFF if text == 'off' else ranged(0, 0xFFFF)(text)


def _keyboard_code(text: str) -> tuple[int, int]:
    """Parse ORDINAL=CODE: a page 11 ordinal and the code to set, each decimal or 0x hex."""
    ordinal, separator, code = text.partition('=')
    if not separator:
        raise argparse.ArgumentTypeError(f"not ORDINAL=CODE: {text}")
    return ranged(0, 0xFFFF)(ordinal), ranged(0, 0xFF)(code)


def _parse_colour(text: str) -> tuple[int, ...]:
    """Parse RRGGBB or RRGGBBWW hex into a colour tuple.

    :param text: six or eight hex digits
    :return: (red, green, blue) or (red, green, blue, white)
    """
    if len(text) not in (6, 8) or not set(text) <= set('0123456789abcdefABCDEF'):
        raise SystemExit(f"colour must be six or eight hex digits: {text}")
    return tuple(bytes.fromhex(text))


def _hue(index: float, count: int) -> tuple[int, int, int]:
    """One of count evenly spaced, fully saturated hues."""
    red, green, blue = colorsys.hsv_to_rgb((index / max(count, 1)) % 1.0, 1.0, 1.0)
    return int(red * 255), int(green * 255), int(blue * 255)


############
# COMMANDS #
############


@hlp_command
def ping() -> None:
    """Check a board's Host Lighting interface and protocol version."""
    parser = device_parser("Say HELLO to a GP2040-CE Host Lighting interface and time the round trip.")
    args = parser.parse_args()
    device = open_device(args.board_id)
    try:
        hello = device.hello()
        identity = device.read_identity() if PAGE_IDENTITY in hello.pages else Identity()
        print(f"{identity.board_label or '?'} ({identity.firmware_version or '?'}), board ID {hello.board_id}")
        print(f"magic {hello.magic.decode('ascii', 'replace')}, protocol {hello.major}.{hello.minor}, "
              f"reply queue limit {hello.reply_queue_limit}")
        count = 100
        start = time.monotonic()
        for _ in range(count):
            device.request_ok(CMD_HELLO)
        elapsed = time.monotonic() - start
        print(f"{count} round trips in {elapsed * 1000:.0f} ms ({elapsed * 1000 / count:.1f} ms average)")
    finally:
        device.close()


@hlp_command
def caps() -> None:
    """Print everything a board reports about itself, page by page, each page the board lists."""
    parser = device_parser("Read and decode a GP2040-CE board's Host Lighting capabilities and pages.")
    parser.add_argument('--json', action='store_true',
                        help="print HELLO and every page the library decodes as JSON, the records' fields by name")
    parser.add_argument('--names', action='store_true',
                        help="name actions and light kinds from the board's page 3 when -P names none "
                             "(148 more requests)")
    args = parser.parse_args()
    device = open_device(args.board_id)
    try:
        hello = device.hello()
        if args.json:
            print(json.dumps(_caps_json(device, hello, args.names), indent=1))
            return
        board_names = device.read_names() if args.names and PAGE_NAMES in hello.pages else {}
        names = enum_names() or board_names
        _print_hello(hello)
        pages: tuple[tuple[int, Callable[[], None]], ...] = (
                 (PAGE_IDENTITY, lambda: _print_identity(device)),
                 (PAGE_STATE, lambda: _print_state(device, names)),
                 (PAGE_INPUT_MODES, lambda: _print_input_modes(device)),
                 (PAGE_NAMES, lambda: _print_names(board_names, args.names)),
                 (PAGE_SUMMARY, lambda: _print_summary(device, names)),
                 (PAGE_PROFILES, lambda: _print_profiles(device)),
                 (PAGE_CONTROLS, lambda: _print_controls(device, names)),
                 (PAGE_KEYBOARD_MAPPING, lambda: _print_keyboard_mapping(device, names)),
                 (PAGE_LIGHTS, lambda: _print_lights(device, names)),
                 (PAGE_ANIMATIONS, lambda: _print_animations(device, names)))
        for page, show in pages:
            if page in hello.pages:
                show()
    finally:
        device.close()


def _caps_json(device: HostLightingDevice, hello: Hello, names: bool) -> dict[str, Any]:
    """Read HELLO's pages the library decodes, as hlp-caps --json prints them, page 3 only with names."""
    readers: dict[int, Callable[[], Any]] = {
        PAGE_IDENTITY: device.read_identity,
        PAGE_STATE: lambda: device.read_record(PAGE_STATE).records,
        PAGE_SUMMARY: lambda: device.read_record(PAGE_SUMMARY).records}
    for page in (PAGE_INPUT_MODES, PAGE_PROFILES, PAGE_CONTROLS, PAGE_KEYBOARD_MAPPING, PAGE_LIGHTS, PAGE_ANIMATIONS):
        readers[page] = functools.partial(lambda number: device.read_table(number).records, page)
    if names:
        readers[PAGE_NAMES] = device.read_names
    return {'hello': jsonable(hello),
            'pages': {str(page): jsonable(readers[page]()) for page in sorted(hello.pages) if page in readers}}


def _print_names(names: dict[str, dict[int, str]], read: bool) -> None:
    """Print page 3 as read_names() gave it, counted by enum, or that it was not read."""
    if not read:
        print('page 3 (names): not read (--names reads it)')
        return
    print('page 3 (names): ' + (', '.join(f"{len(values)} {enum}" for enum, values in names.items()) or 'none'))


def _print_identity(device: HostLightingDevice) -> None:
    """Read and print page 0, the board's identity."""
    identity = device.read_identity()
    print("page 0 (identity):")
    for item in fields(identity):
        label = item.name.replace('_', ' ').replace(' id', ' ID')
        print(f"  {label}: {getattr(identity, item.name) or '?'}")


def _print_hello(hello: Hello) -> None:
    """Print the HELLO reply's version, board ID and capabilities."""
    def listed(values: frozenset[int]) -> str:
        return ', '.join(str(value) for value in sorted(values)) or 'none'

    print(f"protocol {hello.major}.{hello.minor}, board ID {hello.board_id}, "
          f"reply queue limit {hello.reply_queue_limit}")
    print("  commands: " + ', '.join(f"0x{command:02X}" for command in sorted(hello.commands)))
    print(f"  pages: {listed(hello.pages)}; request flags: {listed(hello.request_flags)}; "
          f"addressing modes: {listed(hello.addressing_modes)}; pixel formats: {listed(hello.pixel_formats)}; "
          f"events: {listed(hello.events)} (subscribed now: {listed(hello.events_enabled)})")


def _print_state(device: HostLightingDevice, names: dict[str, dict[int, str]]) -> None:
    """Read and print page 1, the board's state."""
    read = device.read_record(PAGE_STATE)
    record = read.records[0]
    state, holder, settings = record.state, record.controller, record.settings
    animation = 'off' if state.animation is None else state.animation
    print(f"page 1 (state), token 0x{read.token:08X}: input mode {_named(state.input_mode, names, 'InputMode')}, "
          f"profile {state.profile}, player {state.player}, animation {animation}"
          f"{' (idle)' if state.animations_idle else ''}, "
          f"D-pad mode {_named(state.dpad_mode, names, 'DpadMode')}, "
          f"brightness step {state.brightness_step_shown}"
          + (f" (unsaved; saved step {state.saved_brightness_step})" if state.brightness_unsaved else ''))
    live = 'live' if state.takeover_live else 'not live'
    takeover = {TAKEOVER_WHOLE_FRAME: 'whole-frame', TAKEOVER_OVERLAY: 'overlay'}.get(settings.takeover,
                                                                                      f"takeover {settings.takeover}")
    print(f"  takeover {live} (number {state.takeover_number}), {takeover}, keepalive {settings.keepalive_ms} ms, "
          f"board brightness {'applied' if settings.brightness_applied else 'not applied'} to host pixels")
    if holder.holder_token:
        kind = 'exclusive' if holder.exclusive else 'shared'
        print(f"  claimed by '{holder.application_name}' (token 0x{holder.holder_token:08X}, {kind}), "
              f"claim generation {holder.claim_generation}")
    else:
        print(f"  unclaimed, claim generation {holder.claim_generation}")


def _print_input_modes(device: HostLightingDevice) -> None:
    """Read and print page 2, the input modes SET_INPUT_MODE accepts."""
    modes = device.read_table(PAGE_INPUT_MODES).records
    print(f"page 2 (input modes): {len(modes)}")
    for mode in modes:
        note = ''
        if mode.mode.lighting:
            note = ' (lighting once a PC is detected)' if mode.mode.detected else ' (lighting)'
        print(f"  {mode.mode.value:3d} {mode.name}{note}")


def _print_summary(device: HostLightingDevice, names: dict[str, dict[int, str]]) -> None:
    """Read and print page 8, the summary a host sizes itself from."""
    record = device.read_record(PAGE_SUMMARY).records[0]
    summary = record.summary
    rate = f"{summary.render_hz} Hz" if summary.render_hz else 'not stated'
    print(f"page 8 (summary): LED extent {summary.led_extent}, limit {summary.led_limit}, render rate {rate}, "
          f"colour format {_named(summary.colour_format, names, 'LEDFormat_Proto')}, "
          f"{'a' if summary.white_channel else 'no'} white channel")
    print(f"  brightness maximum {summary.brightness_maximum}, {summary.brightness_steps} steps; "
          f"animation speed steps {summary.animation_speed_steps}; grid {summary.grid_width}x{summary.grid_height}")
    print(f"  {summary.profiles} profiles, {summary.controls} controls ({summary.lit_controls} lit, "
          f"{summary.unlit_controls} unlit), {summary.lights} lights, {summary.animations} animations")
    print(f"  button layouts {_named(summary.button_layout, names, 'ButtonLayout')} and "
          f"{_named(summary.button_layout_right, names, 'ButtonLayoutRight')}, orientation "
          f"{_named(summary.button_layout_orientation, names, 'ButtonLayoutOrientation')}")
    print("  changes counted on pages "
          + ', '.join(f"{page}: {count}" for page, count in enumerate(record.changes.counters, PAGE_SUMMARY)))


def _print_profiles(device: HostLightingDevice) -> None:
    """Read and print page 9, the input profiles."""
    profiles = [record.profile for record in device.read_table(PAGE_PROFILES).records]
    print(f"page 9 (profiles): {len(profiles)}")
    for profile in profiles:
        state = 'enabled' if profile.enabled else 'disabled'
        print(f"  {profile.number} {state} {profile.label}".rstrip())


def _print_controls(device: HostLightingDevice, names: dict[str, dict[int, str]]) -> None:
    """Read and print page 10, every input with an action, lit or not."""
    read = device.read_table(PAGE_CONTROLS)
    controls = read.records
    lit = sum(1 for control in controls if control.input.lit)
    print(f"page 10 (controls): {len(controls)} ({lit} lit), token 0x{read.token:08X}")
    for control in controls:
        pieces = [key_name(control.input.key), 'action ' + _named(control.input.action, names, 'GpioAction')]
        if control.instances.instances > 1:
            pieces.append(f"instance {control.instances.instance} of {control.instances.instances}")
        if control.input.lit and control.position.grid_rectangle is not None:
            x0, y0, x1, y1 = control.position.grid_rectangle
            cell = f"({x0},{y0})" if (x0, y0) == (x1, y1) else f"({x0},{y0})-({x1},{y1})"
            pieces.append(f"LED {control.position.first_led} at {cell}, {control.lights.light_count} light(s) "
                          f"from ordinal {control.lights.first_light}")
        else:
            pieces.append('unlit')
        print("  " + ' '.join(pieces))


def _print_keyboard_mapping(device: HostLightingDevice, names: dict[str, dict[int, str]]) -> None:
    """Read and print page 11, the code the Keyboard input mode sends for each button."""
    read = device.read_table(PAGE_KEYBOARD_MAPPING)
    print(f"page {PAGE_KEYBOARD_MAPPING} (keyboard mapping): {len(read.records)}, token 0x{read.token:08X}")
    for record in read.records:
        print(f"  {_keyboard_line(record.ordinal, record.button.action, record.button.code, names)}")


def _keyboard_line(ordinal: int, action: int, code: int, names: dict[str, dict[int, str]]) -> str:
    """Show one page 11 record: its ordinal, its button's action and its code, in hex."""
    shown = f"code 0x{code:02X}" if code else 'no code'
    return f"{ordinal:2d} action {_named(action, names, 'GpioAction')}: {shown}"


def _print_lights(device: HostLightingDevice, names: dict[str, dict[int, str]]) -> None:
    """Read and print page 12, every light."""
    read = device.read_table(PAGE_LIGHTS)
    lights = read.records
    print(f"page {PAGE_LIGHTS} (lights): {len(lights)}, token 0x{read.token:08X}")
    for light in lights:
        pieces = [f"{light.ordinal:3d} LED {light.leds.first_led}"]
        if light.leds.led_count > 1:
            pieces.append(f"x{light.leds.led_count}")
        pieces.append('kind ' + _named(light.kind.value, names, 'LightType'))
        if light.owner.pin is not None:
            owner = key_name(light.owner.pin)
            if light.owner.action is not None:
                owner += ' action ' + _named(light.owner.action, names, 'GpioAction')
            if (light.owner_instances.instances or 0) > 1:
                owner += f" (instance {light.owner_instances.instance} of {light.owner_instances.instances})"
            pieces.append(owner)
        pieces.append(f"at ({light.grid.x},{light.grid.y})")
        if light.palette.slot is not None:
            pieces.append(f"palette slot {light.palette.slot}")
        print("  " + ' '.join(pieces))


def _print_animations(device: HostLightingDevice, names: dict[str, dict[int, str]]) -> None:
    """Read and print page 13, the on-board animations."""
    animations = device.read_table(PAGE_ANIMATIONS).records
    print(f"page {PAGE_ANIMATIONS} (animations): {len(animations)}")
    for animation in animations:
        effects, speeds = animation.effects, animation.speeds
        state = 'enabled' if effects.enabled else 'disabled'
        print(f"  {animation.ordinal} {state}, effects "
              f"idle {_named(effects.idle, names, 'AnimationNonPressedEffects')}, "
              f"pressed {_named(effects.pressed, names, 'AnimationPressedEffects')}, "
              f"case {_named(effects.case, names, 'AnimationNonPressedEffects')}, "
              f"speeds {speeds.idle}/{speeds.pressed}/{speeds.case}")


@hlp_command
def settings() -> None:
    """Show or change a board's profile, animation, animation speeds, brightness and keyboard codes."""
    parser = device_parser("Show a GP2040-CE board's input profile, animation, animation speeds, brightness and "
                           "keyboard codes, or change them. The board applies a change live and saves it to its "
                           "settings flash, a profile or keyboard code at once and the others about a second later. "
                           "A value equal to the current one writes nothing. Use it for a user's choice, not as an "
                           "effect. A profile change switches the live button mapping. --brightness with --unsaved "
                           "writes nothing. The board shows the step while this command holds the claim, and the "
                           "saved step again when it ends.")
    parser.add_argument('--profile', type=ranged(0, 0xFF), help="select this input profile (a page 9 number)")
    parser.add_argument('--animation', type=_animation,
                        help=f"select this animation (a page {PAGE_ANIMATIONS} ordinal) or 'off'")
    parser.add_argument('--speed', type=ranged(0, 0xFFFF), metavar='ORDINAL',
                        help="set this animation's speeds from --idle, --pressed and --case")
    for name in ('idle', 'pressed', 'case'):
        parser.add_argument('--' + name, type=ranged(0, SPEED_UNCHANGED - 1), default=SPEED_UNCHANGED,
                            help=f"{name} speed for --speed (default: unchanged)")
    parser.add_argument('--brightness', type=ranged(0, BRIGHTNESS_SAVED - 1), help="set this brightness step")
    parser.add_argument('--unsaved', action='store_true',
                        help="show --brightness unsaved for --seconds, holding the claim, then the saved step again")
    parser.add_argument('--seconds', type=float, default=5.0, help="how long --unsaved holds the step (default: 5)")
    parser.add_argument('--keyboard-code', type=_keyboard_code, action='append', metavar='ORDINAL=CODE',
                        help=f"set the code the Keyboard input mode sends for page {PAGE_KEYBOARD_MAPPING}'s record "
                             "ORDINAL, in the active profile, or in --profile's when given. Repeat it for more. "
                             "Consecutive ordinals go in one request, written once")
    parser.add_argument('--names', action='store_true',
                        help="name actions and light kinds from the board's page 3 when -P names none "
                             "(148 more requests)")
    add_claim_arguments(parser)
    args = parser.parse_args()
    speeds = _checked_options(args)
    device = open_device(args.board_id)
    try:
        names = enum_names(device if args.names else None)
        if args.keyboard_code:
            _check_keyboard_ordinals(device, args.keyboard_code)
        if args.unsaved:
            _hold_unsaved_brightness(device, args.brightness, args.seconds, args.force, args.exclusive)
        if args.profile is not None:
            _apply(f"--profile {args.profile}", lambda: device.set_profile(args.profile))
        if args.animation is not None:
            _apply(f"--animation {args.animation}", lambda: device.set_animation(args.animation))
        if args.speed is not None:
            _apply(f"--speed {args.speed}", lambda: device.set_animation_speed(args.speed, *speeds))
        if args.brightness is not None and not args.unsaved:
            _apply(f"--brightness {args.brightness}", lambda: device.set_brightness(args.brightness, save=True))
        if args.keyboard_code:
            _apply('--keyboard-code', lambda: _set_keyboard_codes(device, args.keyboard_code, args.profile))
        _print_settings(device, names)
    finally:
        device.close()


def _checked_options(args: argparse.Namespace) -> tuple[int, int, int]:
    """Check how hlp-settings' options combine, and return the speeds for --speed."""
    speeds = (args.idle, args.pressed, args.case)
    if args.speed is None and speeds != (SPEED_UNCHANGED,) * 3:
        raise SystemExit("--idle, --pressed and --case need --speed ORDINAL")
    if args.speed is not None and speeds == (SPEED_UNCHANGED,) * 3:
        raise SystemExit("--speed needs at least one of --idle, --pressed, --case")
    if args.unsaved and (args.brightness is None or
                         (args.profile, args.animation, args.speed, args.keyboard_code) != (None,) * 4):
        raise SystemExit("--unsaved takes --brightness alone. The other settings are always saved")
    if (args.force or args.exclusive) and not args.unsaved:
        raise SystemExit("--force and --exclusive go with --unsaved, which holds the claim")
    return speeds


def _hold_unsaved_brightness(device: HostLightingDevice, step: int, seconds: float, force: bool,
                             exclusive: bool) -> None:
    """Show a brightness step unsaved while holding the claim. The give up ends it and the saved step returns.

    Each renewal carries KEEPALIVE, which keeps the unsaved step. Were the command to stop without giving up,
    the step would end at the keepalive timeout. A claim taken again after a lapse shows the step again.
    """
    with LightsClaim(device, 'hlp-settings', wait_when_taken_over=False).take(exclusive, force) as claim:
        _apply(f"--brightness {step}", lambda: device.set_brightness(step))
        print(f"brightness step {step} shown, unsaved, for {seconds} s")
        claim.keep(seconds, lambda: device.set_brightness(step))


def _check_keyboard_ordinals(device: HostLightingDevice, changes: list[tuple[int, int]]) -> None:
    """Refuse an ordinal past page 11's records, before hlp-settings changes anything."""
    total = len(device.read_table(PAGE_KEYBOARD_MAPPING).records)
    for ordinal, _ in changes:
        if ordinal >= total:
            raise SystemExit(f"error: --keyboard-code: ordinal {ordinal} is past page {PAGE_KEYBOARD_MAPPING}'s "
                             f"{total} records")


def _set_keyboard_codes(device: HostLightingDevice, changes: list[tuple[int, int]],
                        profile: Optional[int] = None) -> None:
    """Set the codes of page 11's records, one SET_KEYBOARD_MAPPING for each run of consecutive ordinals.

    Only the codes given are sent, each run for the profile given, --profile's, or else the one page 1 shows. The
    board refuses a run after a switch to another profile and writes nothing into it, and a run sent while a
    switch to that profile is still pending is sent again. An ordinal given twice takes the last code. A refused run
    raises, and the runs before it stay written.
    """
    if profile is None:
        profile = device.read_record(PAGE_STATE).records[0].state.profile
    codes = dict(changes)
    ordinals = sorted(codes)
    start = 0
    for end in range(1, len(ordinals) + 1):
        if end == len(ordinals) or ordinals[end] != ordinals[end - 1] + 1:
            device.set_keyboard_mapping(profile, ordinals[start],
                                        [codes[ordinal] for ordinal in ordinals[start:end]])
            start = end


def _apply(option: str, change: Callable[[], None]) -> None:
    """Make one board-feature change, naming the option when the board refuses it."""
    try:
        change()
    except HostLightingRejectedError as error:
        raise SystemExit(f"error: {option}: {error}") from error


def _print_settings(device: HostLightingDevice, names: dict[str, dict[int, str]]) -> None:
    """Print the settings hlp-settings changes, with the values each accepts."""
    state = device.read_record(PAGE_STATE).records[0].state
    summary = device.read_record(PAGE_SUMMARY).records[0].summary
    profiles = [record.profile for record in device.read_table(PAGE_PROFILES).records]
    animations = device.read_table(PAGE_ANIMATIONS).records
    print(f"profile {state.profile}:")
    for profile in profiles:
        mark = '*' if profile.number == state.profile else ' '
        enabled = 'enabled' if profile.enabled else 'disabled'
        print(f"  {mark} {profile.number} {enabled} {profile.label}".rstrip())
    animation = 'off' if state.animation is None else state.animation
    print(f"animation {animation} (an enabled ordinal, or off); speeds 0-{summary.animation_speed_steps}:")
    for entry in animations:
        effects, speeds = entry.effects, entry.speeds
        mark = '*' if entry.ordinal == state.animation else ' '
        print(f"  {mark} {entry.ordinal} {'enabled' if effects.enabled else 'disabled'}, "
              f"idle {_named(effects.idle, names, 'AnimationNonPressedEffects')}, "
              f"pressed {_named(effects.pressed, names, 'AnimationPressedEffects')}, "
              f"case {_named(effects.case, names, 'AnimationNonPressedEffects')}, "
              f"speeds {speeds.idle}/{speeds.pressed}/{speeds.case}")
    print(f"brightness step {state.brightness_step_shown} of 0-{summary.brightness_steps} "
          f"(maximum {summary.brightness_maximum}){', unsaved' if state.brightness_unsaved else ''}")
    if PAGE_KEYBOARD_MAPPING in device.hello().pages:
        print(f"D-pad mode {_named(state.dpad_mode, names, 'DpadMode')} (Up to Right on a GPIO pin or Hall-effect "
              "trigger send their codes only in the digital D-pad mode)")
        print(f"keyboard codes, the active profile's (page {PAGE_KEYBOARD_MAPPING} ordinal, its button's action, "
              "code):")
        for record in device.read_table(PAGE_KEYBOARD_MAPPING).records:
            print(f"  {_keyboard_line(record.ordinal, record.button.action, record.button.code, names)}")


def _state_fields(record: StateRecord, token: int) -> dict[str, Any]:
    """Page 1's fields for hlp-watch, group by group in order, then the state token."""
    shown = {item.name: getattr(group, item.name) for group in (record.state, record.controller, record.settings)
             for item in fields(group)}
    shown['token'] = token
    return shown


def _state_text(key: str, value: object) -> str:
    """Show one page 1 field for hlp-watch."""
    if key in ('token', 'holder_token'):
        return f"0x{value:08X}"
    if key == 'animation' and value is None:
        return 'off'
    return str(value)


@hlp_command
def watch() -> None:
    """Print a board's STATE_CHANGED events as they arrive."""
    parser = device_parser("Subscribe to a GP2040-CE board's STATE_CHANGED event and print page 1, then what each "
                           "event changes. The subscription is shared by every application and lasts until the "
                           "board's USB session ends.")
    parser.add_argument('--seconds', type=float, default=0.0, help="stop after this long (default: until Ctrl-C)")
    args = parser.parse_args()
    device = open_device(args.board_id)
    try:
        if EVENT_STATE_CHANGED not in device.subscribe([EVENT_STATE_CHANGED]):
            raise HostLightingError("the board did not enable STATE_CHANGED")
        read = device.read_record(PAGE_STATE)
        last = _state_fields(read.records[0], read.token)
        device.events.clear()       # sent before the page 1 reply, so older than it
        start = time.monotonic()
        print("   0.000 " + ', '.join(f"{key} {_state_text(key, value)}" for key, value in last.items()))
        while True:
            left = args.seconds - (time.monotonic() - start) if args.seconds else 0.5
            if left <= 0:
                break
            event = device.next_event(min(0.5, left))
            if event is None or event[1] != EVENT_STATE_CHANGED:
                continue
            changed = decode_state_changed(event)
            now = _state_fields(changed.record, changed.token)
            changes = [f"{key} {_state_text(key, last[key])} -> {_state_text(key, value)}"
                       for key, value in now.items() if value != last[key]]
            print(f"{time.monotonic() - start:8.3f} " + (', '.join(changes) or 'no change'))
            last = now
    except KeyboardInterrupt:
        pass
    finally:
        device.close()


@hlp_command
def fill() -> None:
    """Fill a board's LEDs with one colour as a quick visual test."""
    parser = device_parser("Fill a GP2040-CE board's LEDs with a colour, hold, then restore animations. The hold "
                           "ends early if another application takes the lights over.")
    parser.add_argument('--scope', choices=['all', 'buttons', 'turbo', 'case', 'players'], default='all',
                        help="which lights to fill (default: all)")
    parser.add_argument('--seconds', type=float, default=3.0, help="how long to hold the fill (default: 3)")
    add_claim_arguments(parser)
    parser.add_argument('colour', help="colour as RRGGBB or RRGGBBWW hex, e.g. FF0000 for red")
    args = parser.parse_args()
    colour = _parse_colour(args.colour)
    pixel_format = PIXEL_RGBW if len(colour) == 4 else PIXEL_RGB
    kinds = {'buttons': (KIND_BUTTON,), 'turbo': (KIND_TURBO,), 'case': (KIND_CASE,), 'players': KIND_PLAYERS}
    device = open_device(args.board_id)

    def stage() -> None:
        if args.scope == 'all':
            device.fill(colour, pixel_format, commit=True)
        else:
            device.stage_entries(ADDRESSING_KIND, [(kind, colour) for kind in kinds[args.scope]], pixel_format,
                                 commit=True)
    try:
        with LightsClaim(device, 'hlp-fill', wait_when_taken_over=False).take(args.exclusive, args.force) as claim:
            stage()
            print(f"filling {args.scope} with #{args.colour.upper()} for {args.seconds} s")
            claim.keep(args.seconds, stage)
    finally:
        device.close()


@hlp_command
def set_light() -> None:
    """Colour individual lights by ordinal, pin, action or kind."""
    parser = device_parser(f"Colour lights on a GP2040-CE board by their page {PAGE_LIGHTS} ordinal (or by pin, action "
                           "or kind), hold, then restore animations. The hold ends early if another application takes "
                           "the lights over.")
    parser.add_argument('--by', choices=['light', 'pin', 'action', 'kind'], default='light',
                        help=f"what the addresses name (default: light, the page {PAGE_LIGHTS} ordinal)")
    parser.add_argument('--seconds', type=float, default=3.0, help="how long to hold the colours (default: 3)")
    add_claim_arguments(parser)
    parser.add_argument('entries', nargs='+', metavar='ADDRESS:RRGGBB[WW]',
                        help="an address and its colour, e.g. 3:FF0000 or -10:FF0000, where an eight-digit colour "
                             "adds a white byte")
    args = parser.parse_args()
    addressing = {'light': ADDRESSING_LIGHT, 'pin': ADDRESSING_PIN, 'action': ADDRESSING_ACTION,
                  'kind': ADDRESSING_KIND}[args.by]
    low, high = ADDRESS_RANGES[addressing]
    entries = []
    for token in args.entries:
        address, _, colour = token.partition(':')
        try:
            value = int(address, 0)
        except ValueError:
            raise SystemExit(f"the address must be a number: {token}") from None
        if not low <= value <= high:
            raise SystemExit(f"a {args.by} address is {low} to {high}: {token}")
        entries.append((value, _parse_colour(colour)))
    pixel_format = PIXEL_RGBW if any(len(colour) == 4 for _, colour in entries) else PIXEL_RGB
    device = open_device(args.board_id)
    try:
        with LightsClaim(device, 'hlp-set-light', wait_when_taken_over=False).take(args.exclusive, args.force) as claim:
            outcomes = device.stage_entries(addressing, entries, pixel_format, commit=True)
            applied = sum(outcome.applied for outcome in outcomes)
            skipped = sum(outcome.skipped for outcome in outcomes)
            print(f"{applied} applied, {skipped} skipped, holding for {args.seconds} s")
            if skipped:
                capacity = stage_capacity(addressing, pixel_format)
                missing = [entries[report * capacity + bit][0]
                           for report, outcome in enumerate(outcomes)
                           for bit in range(min(capacity, len(entries) - report * capacity))
                           if not (outcome.outcome >> bit) & 1]
                print(f"skipped addresses (no light, or none below the LED limit): {missing}")
            claim.keep(args.seconds, lambda: device.stage_entries(addressing, entries, pixel_format, commit=True))
    finally:
        device.close()


@hlp_command
def stream() -> None:
    """Stream whole frames, as a worked example of a streaming host."""
    parser = device_parser("Take a GP2040-CE board's lights over and stream whole frames: red, green and blue "
                           "fills, then a rainbow at 60 fps, then give the claim up. Each frame is LED runs "
                           "sent with NO_REPLY, COMMIT_AFTER on the last report, which alone is answered and is "
                           "sent once more when unanswered.")
    parser.add_argument('--seconds', type=_duration, default=5.0, help="how long the rainbow runs (default: 5)")
    parser.add_argument('--leds', type=ranged(1, 0xFFFF), help="LEDs per frame (default: the LED extent)")
    parser.add_argument('--timeout-test', action='store_true',
                        help="after the rainbow, go silent and time how long the board keeps the frame")
    add_claim_arguments(parser)
    args = parser.parse_args()
    device = open_device(args.board_id)
    try:
        summary = device.read_record(PAGE_SUMMARY).records[0].summary
        if args.leds and args.leds > summary.led_limit:
            parser.error(f"--leds is at most the board's addressable LED limit, {summary.led_limit}")
        leds = args.leds or summary.led_extent or 1
        with LightsClaim(device, 'hlp-stream', wait_when_taken_over=False).take(args.exclusive, args.force) as claim:
            _stream(claim, leds, args.seconds, args.timeout_test)
    finally:
        device.close()


def _stream(claim: LightsClaim, leds: int, seconds: float, timeout_test: bool) -> None:
    """Run hlp-stream's fills, rainbow and timeout test, stopping if another application takes the lights over."""
    for name, colour in (('red', (255, 0, 0)), ('green', (0, 255, 0)), ('blue', (0, 0, 255))):
        claim.device.fill(colour, commit=True)
        print(f"filled {name}")
        if not claim.keep(1.0, functools.partial(claim.device.fill, colour, commit=True)):
            return
    if _rainbow(claim, leds, seconds) and timeout_test:
        claim.renew()
        if claim.state is ClaimState.HELD:
            claim.let_lapse()       # renewed just now, so the keepalive and the claim's lease lapse together
            _time_expiry(claim.device)


def _play(claim: LightsClaim, seconds: float, frame_at: Callable[[int], Frame], what: str, extent: str) -> bool:
    """Stream frame_at(0), frame_at(1) and on at about 60 fps for seconds, renewing the claim when due.

    Each frame goes as the specification has a streaming host send it. A frame unacknowledged after its resend is
    not shown, and the board is lost only when a HELLO then goes unanswered too.

    :param what: names the frames in the closing line, such as "rainbow"
    :param extent: what each frame covers, in the closing line, such as "46 LEDs"
    :return: False when another application took the lights over
    :raises HostLightingUnansweredError: when the board answers neither a frame nor the HELLO after it
    """
    frames = max(1, int(seconds * 60))
    start = time.monotonic()
    acknowledged = 0
    failure = ''
    for index in range(frames):
        try:
            if publish_frame(claim.device, frame_at(index)).acknowledged:
                acknowledged += 1
            else:
                failure = failure or "no acknowledgement, the last report sent twice"
                _answers_hello(claim)
        except HostLightingRejectedError as error:
            failure = failure or str(error)
            claim.refused(error)        # a status 4 refusal shows a change of holder
        claim.maintain()
        if claim.state is not ClaimState.HELD:
            return False
        time.sleep(max(0.0, start + (index + 1) / 60 - time.monotonic()))
    print(f"{what}: {acknowledged} of {frames} frames acknowledged ({extent})")
    if failure:
        print(f"  the first unacknowledged frame: {failure}")
    return True


def _rainbow(claim: LightsClaim, leds: int, seconds: float) -> bool:
    """Stream a rainbow moving along the LEDs at about 60 fps.

    :return: False when another application took the lights over
    """
    def frame_at(index: int) -> Frame:
        """Build the rainbow's frame index."""
        frame = Frame()
        frame.run(ADDRESSING_LED_RUN, 0, [_hue(led + index * leds / 120, leds) for led in range(leds)])
        return frame
    return _play(claim, seconds, frame_at, "rainbow", f"{leds} LEDs")


def _lit(hue: float, level: float) -> tuple[int, int, int]:
    """Give a fully saturated hue, from 0 to 1 round the colour wheel, at a brightness level from 0 to 1."""
    red, green, blue = colorsys.hsv_to_rgb(hue % 1.0, 1.0, level)
    return round(red * 255), round(green * 255), round(blue * 255)


def show_colours(lights: Sequence[LightRecord], elapsed: float) -> list[tuple[int, int, int]]:
    """Colour each light for hlp-show at a moment of the show.

    The button lights each take their own hue, left to right across the grid, drifting round the colour wheel, and
    a peak travels across them, six lights a second, lighting its neighbours in part. The case lights and lights of
    other kinds carry a rainbow wave, its brightness a cosine-cubed pulse travelling along them. The player and turbo
    lights pulse cyan once a second.

    :param lights: the page 12 records. A light read without its kind group joins the wave, and a button without
        its grid group goes before the others.
    :param elapsed: seconds since the show started
    :return: a colour per light ordinal, from 0 to the highest, black for an ordinal not given; none for no lights
    """
    if not lights:
        return []
    colours = [(0, 0, 0)] * (max(light.ordinal for light in lights) + 1)
    kinds = {light.ordinal: light.kind.value if light.has('kind') else None for light in lights}
    buttons = sorted((light for light in lights if kinds[light.ordinal] == KIND_BUTTON),
                     key=lambda light: (light.grid.x, light.grid.y, light.ordinal) if light.has('grid') else
                     (-1, -1, light.ordinal))
    pulsing = [light for light in lights if kinds[light.ordinal] == KIND_TURBO or kinds[light.ordinal] in KIND_PLAYERS]
    skip = {light.ordinal for light in buttons + pulsing}
    wave = [light for light in lights if light.ordinal not in skip]
    for index, light in enumerate(buttons):
        distance = (elapsed * 6 - index) % len(buttons)
        level = max(0.15, 1 - min(distance, len(buttons) - distance))
        colours[light.ordinal] = _lit(index / len(buttons) + elapsed / 10, level)
    for index, light in enumerate(wave):
        pulse = ((math.cos(2 * math.pi * (index / len(wave) - elapsed * 0.3)) + 1) / 2) ** 3
        colours[light.ordinal] = _lit(index / len(wave) + elapsed / 5, max(0.05, pulse))
    cyan = round(255 * (1 - math.cos(2 * math.pi * elapsed)) / 2)
    for light in pulsing:
        colours[light.ordinal] = (0, cyan, cyan)
    return colours


@hlp_command
def show() -> None:
    """Run a light show across a board's buttons, case, player and turbo lights, as a visual test."""
    parser = device_parser("Take a GP2040-CE board's lights over for a light show at 60 fps, then give the claim "
                           "up: a rainbow chase across the buttons, a rainbow wave along the case lights, and the "
                           "player and turbo lights pulsing cyan. A board that reports no lights on page 12 shows "
                           "a rainbow moving along its LEDs.")
    parser.add_argument('--seconds', type=_duration, default=15.0, help="how long the show runs (default: 15)")
    add_claim_arguments(parser)
    args = parser.parse_args()
    device = open_device(args.board_id)
    try:
        lights = device.read_table(PAGE_LIGHTS).records if PAGE_LIGHTS in device.hello().pages else ()
        leds = 0 if lights else device.read_record(PAGE_SUMMARY).records[0].summary.led_extent or 1
        with LightsClaim(device, 'hlp-show', wait_when_taken_over=False).take(args.exclusive, args.force) as claim:
            if lights:
                _play(claim, args.seconds, functools.partial(_show_frame, lights), "show", f"{len(lights)} lights")
            else:
                _rainbow(claim, leds, args.seconds)
    finally:
        device.close()


def _show_frame(lights: Sequence[LightRecord], index: int) -> Frame:
    """Build the show's frame index, every light in one light run."""
    frame = Frame()
    frame.run(ADDRESSING_LIGHT_RUN, 0, show_colours(lights, index / 60))
    return frame


def _answers_hello(claim: LightsClaim) -> None:
    """Send HELLO. Its reply, of any status, shows the board is still there. A status 4 reply goes to the claim.

    :raises HostLightingUnansweredError: when it goes unanswered
    """
    try:
        reply = claim.device.request(CMD_HELLO)
    except HostLightingTimeoutError:
        raise HostLightingUnansweredError("the board answers neither a frame nor HELLO") from None
    if reply[2] == STATUS_CLAIMED:
        claim.refused(claim.device.rejected(CMD_HELLO, reply))


def _time_expiry(device: HostLightingDevice) -> None:
    """Go silent and report when page 1 shows the takeover ended."""
    print("silent, waiting for the keepalive timeout")
    start = time.monotonic()
    while device.read_record(PAGE_STATE).records[0].state.takeover_live and time.monotonic() - start < 15:
        time.sleep(0.02)
    print(f"takeover ended after {time.monotonic() - start:.2f} s")


# The buttons hlp-buttons lights in its third phase, as GpioAction names of GP2040-CE's enums.proto. Each value is
# used when the .proto files were not given.
OVERLAY_BUTTONS = (('BUTTON_PRESS_B1', 5), ('BUTTON_PRESS_B2', 6), ('BUTTON_PRESS_B3', 7), ('BUTTON_PRESS_B4', 8),
                   ('BUTTON_PRESS_L1', 9), ('BUTTON_PRESS_R1', 10), ('BUTTON_PRESS_L2', 11), ('BUTTON_PRESS_R2', 12))


def overlay_buttons(controls: tuple[ControlRecord, ...], names: dict[str, dict[int, str]]) -> list[tuple[str, int]]:
    """Pick the buttons of OVERLAY_BUTTONS that the board lights.

    :param controls: the page 10 records
    :param names: from enum_names(), with GpioAction names that give the values when present
    :return: [(label, action)] in the order of OVERLAY_BUTTONS, for the controls the board reports as lit
    """
    values = {name: value for value, name in names.get('GpioAction', {}).items()}
    lit = {control.input.action for control in controls if control.input.lit}
    return [(name[len('BUTTON_PRESS_'):], values.get(name, fallback)) for name, fallback in OVERLAY_BUTTONS
            if values.get(name, fallback) in lit]


@hlp_command
def buttons() -> None:
    """Colour every lit control and every light with its own hue, then the lit buttons over the animations."""
    parser = device_parser("Take a GP2040-CE board's lights over: every lit control its own hue (staged by "
                           "action), then every light its own hue (one light run), then the lit buttons among "
                           "B1-B4, L1, R1, L2 and R2 white over the running animations (overlay takeover), then "
                           "give the claim up.")
    parser.add_argument('--seconds', type=float, default=5.0, help="how long each phase holds (default: 5)")
    parser.add_argument('--names', action='store_true',
                        help="take the buttons' actions from the board's page 3 when -P names none "
                             "(148 more requests)")
    add_claim_arguments(parser)
    args = parser.parse_args()
    device = open_device(args.board_id)
    try:
        controls = device.read_table(PAGE_CONTROLS).records
        lights = device.read_table(PAGE_LIGHTS).records
        overlay = overlay_buttons(controls, enum_names(device if args.names else None))
        with LightsClaim(device, 'hlp-buttons', wait_when_taken_over=False).take(args.exclusive, args.force) as claim:
            _buttons(claim, controls, lights, args.seconds, overlay)
    finally:
        device.close()


def _buttons(claim: LightsClaim, controls: tuple[ControlRecord, ...], lights: tuple[LightRecord, ...],
             seconds: float, overlay: list[tuple[str, int]]) -> None:
    """Run hlp-buttons' three phases, stopping if another application takes the lights over.

    The give up restores the CONFIGURE defaults, whole-frame takeover included. A claim taken again after a lapse
    stages the phase again.
    """
    device = claim.device
    actions = sorted({control.input.action for control in controls if control.input.lit})
    if actions:
        hues = [(action, _hue(i, len(actions))) for i, action in enumerate(actions)]
        outcomes = device.stage_entries(ADDRESSING_ACTION, hues, commit=True)
        print(f"phase 1: {sum(outcome.applied for outcome in outcomes)} of {len(actions)} lit actions, one hue each")
        if not claim.keep(seconds, lambda: device.stage_entries(ADDRESSING_ACTION, hues, commit=True)):
            return
    if lights:
        colours = [_hue(i, len(lights)) for i in range(len(lights))]
        device.clear()
        device.stage_run(ADDRESSING_LIGHT_RUN, 0, colours, commit=True)
        print(f"phase 2: {len(lights)} lights, one hue each")
        if not claim.keep(seconds, lambda: device.stage_run(ADDRESSING_LIGHT_RUN, 0, colours, commit=True)):
            return
    device.clear()
    claim.configure(Settings(takeover=TAKEOVER_OVERLAY))
    if claim.state is not ClaimState.HELD:
        return          # taken over, so nothing is staged into another application's frame
    white = [(action, (255, 255, 255)) for _, action in overlay]
    if overlay:
        outcomes = device.stage_entries(ADDRESSING_ACTION, white, commit=True)
        print(f"phase 3: overlay, {' '.join(label for label, _ in overlay)} white over the animations "
              f"({sum(outcome.applied for outcome in outcomes)} applied)")
    else:
        print("phase 3: overlay, the board lights none of B1-B4, L1, R1, L2 and R2")
    claim.keep(seconds, lambda: device.stage_entries(ADDRESSING_ACTION, white, commit=True) if white else None)


@hlp_command
def input_mode() -> None:
    """Set a board's input mode over Host Lighting."""
    parser = device_parser("Set a GP2040-CE board's input mode. The board saves it and reboots into it.")
    parser.add_argument('mode', type=ranged(0, 0xFF),
                        help="InputMode value, decimal or 0x hex. Page 2 (hlp-caps) lists the modes the board accepts "
                             "and which carry the lighting interface")
    parser.add_argument('--force', action='store_true',
                        help="switch even into a mode without the lighting interface, which these tools cannot leave")
    args = parser.parse_args()
    device = open_device(args.board_id)
    try:
        modes = device.read_table(PAGE_INPUT_MODES).records
        known = {mode.mode.value: mode for mode in modes}
        if args.mode not in known:
            raise SystemExit(f"the board does not accept input mode {args.mode}. It accepts "
                             + ', '.join(f"{mode.mode.value} ({mode.name})" for mode in modes))
        if not known[args.mode].mode.lighting:
            leave = ("only the web configurator, a boot-time button hold, the reboot hotkeys or the display menu "
                     "leave it")
            if not args.force:
                raise SystemExit(f"{known[args.mode].name} has no lighting interface and these tools cannot "
                                 f"switch the board back. {leave.capitalize()}. Add --force to switch anyway")
            print(f"note: {known[args.mode].name} has no lighting interface, and {leave}")
        elif known[args.mode].mode.detected:
            print(f"note: {known[args.mode].name} presents the lighting interface once the board has detected "
                  "a PC, a few seconds and a second enumeration after it starts")
        device.set_input_mode(args.mode, allow_no_lighting=True)      # checked against page 2 above
        print(f"input mode {args.mode} ({known[args.mode].name}) set. The board is rebooting")
    finally:
        device.close()


# hlp-reboot's targets: the REBOOT target and what the command prints
REBOOT_TARGETS = {'gamepad': (REBOOT_GAMEPAD, "rebooting into the saved input mode"),
                  'webconfig': (REBOOT_WEBCONFIG, "rebooting to web configurator"),
                  'bootsel': (REBOOT_BOOTSEL, "rebooting to BOOTSEL. Watch for the RPI-RP2 drive")}


@hlp_command
def reboot() -> None:
    """Reboot a board into its saved input mode, its web configurator or BOOTSEL."""
    parser = device_parser("Reboot a GP2040-CE board: gamepad into its saved input mode, which can differ from "
                           "the mode it runs when a boot hotkey chose that one, webconfig into its web "
                           "configurator (usually at 192.168.7.1), or bootsel into BOOTSEL mode (the RPI-RP2 "
                           "drive) for flashing.")
    parser.add_argument('target', choices=list(REBOOT_TARGETS), help="where to reboot to")
    args = parser.parse_args()
    target, message = REBOOT_TARGETS[args.target]
    device = open_device(args.board_id)
    try:
        device.reboot(target)
        print(message)
    finally:
        device.close()
