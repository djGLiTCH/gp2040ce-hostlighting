"""Test the hlp-* commands.

SPDX-FileCopyrightText: © 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import json
import struct

import pytest

from gp2040ce_hostlighting import protocol as hl
from gp2040ce_hostlighting import records
from gp2040ce_hostlighting.testing import (control_record, light_record, other_event, state_event, state_record,
                                           summary_record)
from gp2040ce_hostlighting.tools import commands as tools


def test_caps_prints_every_page(hlp_run_on_fake_board, capsys, monkeypatch):
    """Test hlp-caps' output, with enum names when the .proto files supply them."""
    monkeypatch.setattr(tools, 'enum_names', lambda: {'GpioAction': {1: 'BUTTON_PRESS_UP'},
                                                      'LightType': {1: 'Case'},
                                                      'InputMode': {0: 'INPUT_MODE_XINPUT'},
                                                      'AnimationNonPressedEffects': {2: 'EFFECT_RAINBOW_ROTATE'},
                                                      'AnimationPressedEffects': {1: 'PRESSEDEFFECT_RANDOM'}})
    hlp_run_on_fake_board(tools.caps)
    out = capsys.readouterr().out
    assert 'protocol 2.0, board ID 433030343237362E, reply queue limit 16' in out
    assert 'firmware version: v0.7.12-88-ga83f820' in out
    assert 'input mode 0 (INPUT_MODE_XINPUT), profile 1, player 1, animation 0, brightness step 5' in out
    assert '0 XINPUT (lighting once a PC is detected)' in out and '4 PS4\n' in out
    assert 'LED extent 19, limit 100, render rate 40 Hz, colour format 0, no white channel\n' in out
    assert 'button layouts 0 and 0, orientation 0\n' in out and 'changes counted on pages 8: 0, 9: 0' in out
    assert 'GP2 action 1 (BUTTON_PRESS_UP) LED 0 at (1,0), 1 light(s) from ordinal 0' in out
    assert 'GP12 action 13 unlit' in out and 'PCF3 action 5 unlit' in out
    assert '1 LED 1 x3 kind 1 (Case) at (0,0) palette slot 0' in out
    assert ('effects idle 2 (EFFECT_RAINBOW_ROTATE), pressed 1 (PRESSEDEFFECT_RANDOM), case 2 '
            '(EFFECT_RAINBOW_ROTATE), speeds 5/5/5') in out


def test_fill_scopes(hlp_run_on_fake_board, hlp_fake_board, capsys):
    """Test hlp-fill: FILL for all, STAGE by kind for a scope, a renewal each third of 2 s, then the give up."""
    hlp_run_on_fake_board(tools.fill, '--seconds', '2', 'FF8000')
    (fill,) = hlp_fake_board.sent(hl.CMD_FILL)
    generation = 2 << hl.FLAG_GENERATION_SHIFT                # the claim's, on every request once it is held
    assert fill[2] == hl.FLAG_COMMIT_AFTER | generation and fill[3:7] == bytes([0, 0xFF, 0x80, 0x00])
    claims = hlp_fake_board.sent(hl.CMD_CLAIM)
    renewal = (hl.FLAG_KEEPALIVE | generation, hl.CLAIM_TAKE)
    assert [(claim[2], claim[3]) for claim in claims] == [(0, hl.CLAIM_TAKE), renewal, renewal, renewal,
                                                          (generation, hl.CLAIM_GIVE_UP)]
    assert claims[0][8:24].rstrip(b'\x00') == b'hlp-fill' and hlp_fake_board.holder_token == 0
    # the give up hands everything back: no RELEASE follows, nor a CONFIGURE after the claim's own
    assert hlp_fake_board.requests[-1] is claims[-1] and not hlp_fake_board.sent(hl.CMD_RELEASE)
    assert [request[3:7] for request in hlp_fake_board.sent(hl.CMD_CONFIGURE)] == [bytes([0, 0xD0, 0x07, 1])]
    hlp_run_on_fake_board(tools.fill, '--scope', 'players', '00FF00')
    stage = hlp_fake_board.sent(hl.CMD_STAGE)[-1]
    assert stage[3:5] == bytes([hl.ADDRESSING_KIND, 4]) and stage[5:9] == bytes([3, 0, 0xFF, 0])
    assert 'filled players with #00FF00' in capsys.readouterr().out
    hlp_run_on_fake_board(tools.fill, '--scope', 'turbo', '0000FF')
    stage = hlp_fake_board.sent(hl.CMD_STAGE)[-1]
    assert stage[3:5] == bytes([hl.ADDRESSING_KIND, 1]) and stage[5:9] == bytes([hl.KIND_TURBO, 0, 0, 0xFF])


def test_fill_claims_exclusively_with_exclusive(hlp_run_on_fake_board, hlp_fake_board):
    """Test that --exclusive sets claim flag bit 0 on each claim and FLAG_HOLDER with the generation while held."""
    hlp_run_on_fake_board(tools.fill, '--seconds', '2', '--exclusive', 'FF8000')
    claims = hlp_fake_board.sent(hl.CMD_CLAIM)
    holder = hl.FLAG_HOLDER | 2 << hl.FLAG_GENERATION_SHIFT
    renewal = (hl.FLAG_KEEPALIVE | holder, hl.CLAIM_TAKE, hl.CLAIM_EXCLUSIVE)
    assert [(claim[2], claim[3], claim[24]) for claim in claims] == [
        (0, hl.CLAIM_TAKE, hl.CLAIM_EXCLUSIVE), renewal, renewal, renewal, (holder, hl.CLAIM_GIVE_UP, 0)]
    (fill,) = hlp_fake_board.sent(hl.CMD_FILL)
    assert fill[2] == hl.FLAG_COMMIT_AFTER | holder and hlp_fake_board.holder_token == 0
    hlp_run_on_fake_board(tools.fill, 'FF8000')
    shared = hlp_fake_board.sent(hl.CMD_CLAIM)[5:]
    assert [(claim[2] & hl.FLAG_HOLDER, claim[24]) for claim in shared] == [(0, 0)] * len(shared)
    # a claim, a give up and a claim later: generation 4, carried without HOLDER
    assert hlp_fake_board.sent(hl.CMD_FILL)[-1][2] == hl.FLAG_COMMIT_AFTER | 4 << hl.FLAG_GENERATION_SHIFT


def test_caps_shows_an_unsaved_step_and_an_exclusive_claim(hlp_run_on_fake_board, hlp_fake_board, capsys):
    """Test hlp-caps' page 1 lines for an unsaved brightness step and an exclusive claim."""
    hlp_fake_board.state = state_record(brightness_step_shown=3, brightness_unsaved=1, saved_brightness_step=5)
    hlp_fake_board.saved_step = 5
    hlp_fake_board.holder_token, hlp_fake_board.holder_name = 0xA1, b'MESH'.ljust(16, b'\x00')
    hlp_fake_board.holder_flags = hl.CLAIM_EXCLUSIVE
    hlp_run_on_fake_board(tools.caps)
    out = capsys.readouterr().out
    assert 'brightness step 3 (unsaved; saved step 5)' in out
    assert "claimed by 'MESH' (token 0x000000A1, exclusive), claim generation 1" in out


def test_set_light_reports_skipped_addresses(hlp_run_on_fake_board, hlp_fake_board, capsys):
    """Test hlp-set-light by ordinal, and by action with an RGBW colour."""
    hlp_run_on_fake_board(tools.set_light, '0:FF0000', '5:00FF00')
    assert '1 applied, 1 skipped' in capsys.readouterr().out
    hlp_run_on_fake_board(tools.set_light, '--by', 'action', '--', '1:FF000080', '-10:000000FF')
    out = capsys.readouterr().out
    stage = hlp_fake_board.sent(hl.CMD_STAGE)[-1]
    assert stage[3] == hl.ADDRESSING_ACTION | (hl.PIXEL_RGBW << 4)
    assert stage[5:11] == struct.pack('<h', 1) + bytes([0xFF, 0, 0, 0x80])
    assert 'skipped addresses (no light, or none below the LED limit): [-10]' in out


def test_input_mode_checks_page_2(hlp_run_on_fake_board, hlp_fake_board, capsys):
    """Test hlp-input-mode: modes the board accepts, --force for one without lighting, and the guard magic."""
    with pytest.raises(SystemExit, match='does not accept input mode 9'):
        hlp_run_on_fake_board(tools.input_mode, '9')
    with pytest.raises(SystemExit, match='Add --force'):
        hlp_run_on_fake_board(tools.input_mode, '4')
    assert not hlp_fake_board.sent(hl.CMD_SET_INPUT_MODE)
    hlp_run_on_fake_board(tools.input_mode, '3')
    assert hlp_fake_board.sent(hl.CMD_SET_INPUT_MODE)[-1][3:8] == b'\x03MODE'
    hlp_run_on_fake_board(tools.input_mode, '--force', '4')
    out = capsys.readouterr().out
    assert 'PS4 has no lighting interface' in out and 'input mode 4 (PS4) set' in out
    assert hlp_fake_board.sent(hl.CMD_SET_INPUT_MODE)[-1][3:8] == b'\x04MODE'


def test_reboots_and_ping(hlp_run_on_fake_board, hlp_fake_board, capsys):
    """Test hlp-reboot's three targets and hlp-ping."""
    for target in ('gamepad', 'webconfig', 'bootsel'):
        hlp_run_on_fake_board(tools.reboot, target)
    assert [request[3:8] for request in hlp_fake_board.sent(hl.CMD_REBOOT)] == [b'\x00BOOT', b'\x01BOOT',
                                                                                b'\x02BOOT']
    assert 'rebooting into the saved input mode\nrebooting to web configurator\n' in capsys.readouterr().out
    with pytest.raises(SystemExit):
        hlp_run_on_fake_board(tools.reboot, 'flash')
    hlp_run_on_fake_board(tools.ping)
    out = capsys.readouterr().out
    assert 'Haute42 COSMOX M Ultra (v0.7.12-88-ga83f820), board ID 433030343237362E' in out
    assert 'magic GPHL, protocol 2.0, reply queue limit 16' in out and '100 round trips' in out


def test_enum_names_needs_proto_files(monkeypatch):
    """Test that names are looked up only when -P was given."""
    monkeypatch.setattr(tools.common_options, 'proto_files_path', [])
    assert tools.enum_names() == {}


def test_tools_leave_another_applications_lights_alone(hlp_run_on_fake_board, hlp_fake_board, capsys):
    """Test that the LED tools refuse while another application holds the claim, and --force takes over."""
    hlp_fake_board.holder_token, hlp_fake_board.holder_name = 0xA1, b'MESH'.ljust(16, b'\x00')
    with pytest.raises(SystemExit, match="'MESH' holds the lights"):
        hlp_run_on_fake_board(tools.fill, 'FF0000')
    assert not hlp_fake_board.sent(hl.CMD_CONFIGURE) and not hlp_fake_board.sent(hl.CMD_FILL)
    assert hlp_fake_board.holder_token == 0xA1
    hlp_run_on_fake_board(tools.set_light, '--force', '0:FF0000')
    assert hlp_fake_board.sent(hl.CMD_CLAIM)[1][3] == hl.CLAIM_TAKE_OVER and hlp_fake_board.holder_token == 0


def test_hold_stops_when_taken_over(hlp_run_on_fake_board, hlp_fake_board, capsys, monkeypatch):
    """Test that a tool with its claim taken over prints the new holder, and neither releases nor gives up."""
    def sleep(seconds):
        hlp_fake_board.holder_token, hlp_fake_board.holder_name = 0xB2, b'Other'.ljust(16, b'\x00')
    monkeypatch.setattr(tools.time, 'sleep', sleep)
    hlp_run_on_fake_board(tools.fill, 'FF0000')
    assert "'Other' took the lights over" in capsys.readouterr().out
    assert not hlp_fake_board.sent(hl.CMD_RELEASE) and hlp_fake_board.holder_token == 0xB2


def test_hold_gives_up_on_interrupt(hlp_run_on_fake_board, hlp_fake_board, capsys, monkeypatch):
    """Test that the give up follows a Ctrl-C during the hold."""
    def sleep(seconds):
        raise KeyboardInterrupt
    monkeypatch.setattr(tools.time, 'sleep', sleep)
    with pytest.raises(SystemExit, match='^130$'):
        hlp_run_on_fake_board(tools.fill, 'FF0000')
    assert (hlp_fake_board.requests[-1][0], hlp_fake_board.requests[-1][3]) == (hl.CMD_CLAIM, hl.CLAIM_GIVE_UP)
    assert hlp_fake_board.holder_token == 0
    assert 'claim given up, on-board animations restored' in capsys.readouterr().out


def test_give_up_leaves_a_new_holder_alone(hlp_run_on_fake_board, hlp_fake_board, capsys, monkeypatch):
    """Test that a hold ending after another application claimed the lapsed lease sends no RELEASE or CONFIGURE."""
    def sleep(seconds):
        hlp_fake_board.holder_token, hlp_fake_board.holder_name = 0xC3, b'MESH'.ljust(16, b'\x00')
        raise KeyboardInterrupt
    monkeypatch.setattr(tools.time, 'sleep', sleep)
    with pytest.raises(SystemExit, match='^130$'):
        hlp_run_on_fake_board(tools.fill, 'FF0000')
    out = capsys.readouterr().out
    assert "'MESH' took the lights over" in out and 'given up' not in out
    # the give up's reply names MESH
    assert (hlp_fake_board.requests[-1][0], hlp_fake_board.requests[-1][3]) == (hl.CMD_CLAIM, hl.CLAIM_GIVE_UP)
    assert not hlp_fake_board.sent(hl.CMD_RELEASE) and len(hlp_fake_board.sent(hl.CMD_CONFIGURE)) == 1
    assert hlp_fake_board.holder_token == 0xC3


def test_a_failed_give_up_is_a_warning(hlp_run_on_fake_board, hlp_fake_board, capsys, caplog):
    """Test that a board lost during the give up leaves a warning, since the keepalive timeout restores it."""
    hlp_fake_board.disconnect_on = {(hl.CMD_CLAIM, hl.CLAIM_GIVE_UP)}
    hlp_run_on_fake_board(tools.fill, '--seconds', '0', 'FF0000')
    assert 'released' not in capsys.readouterr().out
    assert 'could not give the claim up, the board is no longer connected' in caplog.text
    assert 'The keepalive timeout restores the animations' in caplog.text


@pytest.mark.parametrize('action, flags', [(hl.CLAIM_TAKE, 0), (hl.CLAIM_TAKE, hl.FLAG_KEEPALIVE),
                                           (hl.CLAIM_GIVE_UP, 0)])
def test_another_applications_claim_reply_is_passed_over(hlp_run_on_fake_board, hlp_fake_board, capsys,
                                                         monkeypatch, action, flags):
    """Test that the claim, a renewal and the give up each pass over another application's CLAIM reply."""
    answer = hlp_fake_board.answer
    injected = []

    def with_another_applications_reply(command, sequence, request):
        if command == hl.CMD_CLAIM and (request[3], request[2] & hl.FLAG_KEEPALIVE) == (action, flags) and not injected:
            injected.append(True)
            # this request's command and sequence, another application's action and token
            other = bytes([command | hl.REPLY_FLAG, sequence, hl.STATUS_OK, hl.CLAIM_TAKE_OVER])
            other += struct.pack('<II', 0xBBBB, 0xBBBB) + b'app-b'
            hlp_fake_board.before[command] = [other.ljust(hl.REPORT_SIZE, b'\x00')]
        return answer(command, sequence, request)
    monkeypatch.setattr(hlp_fake_board, 'answer', with_another_applications_reply)
    hlp_run_on_fake_board(tools.fill, '--seconds', '1', 'FF0000')
    out = capsys.readouterr().out
    assert injected and 'app-b' not in out and out.endswith('claim given up, on-board animations restored\n')
    assert not hlp_fake_board.sent(hl.CMD_GET_PAGE) and hlp_fake_board.holder_token == 0


def test_set_light_addressing_and_ranges(hlp_run_on_fake_board, hlp_fake_board, capsys):
    """Test --by pin and --by kind encodings, the address ranges, and a negative address without --."""
    hlp_run_on_fake_board(tools.set_light, '--by', 'pin', '2:010203', '9:040506')
    stage = hlp_fake_board.sent(hl.CMD_STAGE)[-1]
    assert stage[3:5] == bytes([hl.ADDRESSING_PIN, 2]) and stage[5:13] == bytes([2, 1, 2, 3, 9, 4, 5, 6])
    hlp_run_on_fake_board(tools.set_light, '--by', 'kind', '1:FFFFFF')
    assert hlp_fake_board.sent(hl.CMD_STAGE)[-1][3:9] == bytes([hl.ADDRESSING_KIND, 1, 1, 0xFF, 0xFF, 0xFF])
    for arguments, message in ((('--by', 'pin', '256:FF0000'), 'pin address is 0 to 255'),
                               (('70000:FF0000',), 'light address is 0 to 65535'),
                               (('--by', 'action', '-40000:FF0000'), 'action address is -32768 to 32767'),
                               (('abc:FF0000',), 'must be a number'),
                               (('3:FF00',), 'six or eight hex digits')):
        with pytest.raises(SystemExit, match=message):
            hlp_run_on_fake_board(tools.set_light, *arguments)
    hlp_run_on_fake_board(tools.set_light, '--by', 'action', '1:FF0000', '-10:00FF00')
    assert hlp_fake_board.sent(hl.CMD_STAGE)[-1][3:5] == bytes([hl.ADDRESSING_ACTION, 2])
    with pytest.raises(SystemExit, match='^2$'):
        hlp_run_on_fake_board(tools.set_light, '--by', 'action', '1:FF0000', '-x:FF0000')
    assert 'unrecognized arguments: -x:FF0000' in capsys.readouterr().err


def test_input_mode_notes_detection(hlp_run_on_fake_board, hlp_fake_board, capsys):
    """Test the note for a mode with an interface that appears only once a PC is detected."""
    hlp_run_on_fake_board(tools.input_mode, '0')
    assert 'presents the lighting interface once the board has detected a PC' in capsys.readouterr().out


def test_tool_errors_are_one_line(hlp_run_on_fake_board, hlp_fake_board):
    """Test that a board vanishing mid-command ends a tool with a message, not a traceback."""
    hlp_fake_board.disconnect_on.add(hl.CMD_GET_PAGE)
    with pytest.raises(SystemExit, match='error: the board is no longer connected'):
        hlp_run_on_fake_board(tools.caps)


def test_stream_stops_when_the_board_has_gone(hlp_run_on_model_board, hlp_model_board):
    """Test that hlp-stream ends when the board leaves the bus, where it used to send every frame unanswered."""
    hlp_model_board.disconnect_on = {hl.CMD_STAGE}
    with pytest.raises(SystemExit, match='error: the board is no longer connected'):
        hlp_run_on_model_board(tools.stream, '--seconds', '1')
    assert len(hlp_model_board.sent(hl.CMD_STAGE)) == 1


def test_stream_resends_and_asks_hello_after_a_missed_frame(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that a frame unanswered twice is not shown, HELLO is asked, and one unanswered too ends the run."""
    answer, dropped = hlp_model_board.answer, []

    def dropping(command, sequence, request):
        if command == hl.CMD_STAGE and len(dropped) < limit[0]:
            dropped.append(sequence)
            return None
        if command == hl.CMD_HELLO and dropped and limit[0] > 2:
            return None
        return answer(command, sequence, request)
    hlp_model_board.answer = dropping
    limit = [2]
    hlp_run_on_model_board(tools.stream, '--seconds', '1')
    out = capsys.readouterr().out
    assert 'rainbow: 59 of 60 frames acknowledged (4 LEDs)' in out
    assert '  the first unacknowledged frame: no acknowledgement, the last report sent twice' in out
    dropped.clear()
    limit = [3]
    with pytest.raises(SystemExit, match='error: the board answers neither a frame nor HELLO'):
        hlp_run_on_model_board(tools.stream, '--seconds', '1')


def test_enum_names_from_the_proto_files(monkeypatch, tmp_path, caplog):
    """Test that enum names are read from enums.proto, given as a file or as its directory."""
    enums = tmp_path / 'enums.proto'
    enums.write_text("syntax = \"proto2\";\n"
                     "// enum Commented { HIDDEN = 9; }\n"
                     "enum GpioAction\n{\n"
                     "    option (nanopb_enumopt).long_names = false;\n"
                     "    NONE = -10; /* none */\n"
                     "    BUTTON_PRESS_UP = 1 [deprecated = true];\n"
                     "    BUTTON_PRESS_UP_ALIAS = 1;\n"
                     "    SUSTAIN = 0x10;\n"
                     "}\n", encoding='utf-8')
    want = {'GpioAction': {-10: 'NONE', 1: 'BUTTON_PRESS_UP', 16: 'SUSTAIN'}}
    monkeypatch.setattr(tools.common_options, 'proto_files_path', [enums])
    assert tools.enum_names() == want
    nanopb = tmp_path / 'nanopb'
    nanopb.mkdir()
    monkeypatch.setattr(tools.common_options, 'proto_files_path', [tmp_path, nanopb])
    assert tools.enum_names() == want
    monkeypatch.setattr(tools.common_options, 'proto_files_path', [nanopb, tmp_path / 'missing.proto'])
    assert tools.enum_names() == {}
    assert 'could not read' in caplog.text and 'no enum names found' in caplog.text


def test_commands_apply_the_common_options(monkeypatch, capsys):
    """Test that parsing a command line fills the common options, and -v names this package."""
    monkeypatch.setattr(tools, 'common_options', tools.argparse.Namespace(debug=False, proto_files_path=[]))
    parser = tools.device_parser("test")
    args, _ = parser.parse_known_args(['-d', '-P', 'one', '-P', 'two', '--board-id', '4330'])
    assert args.board_id == '4330' and tools.common_options.debug
    assert [str(path) for path in tools.common_options.proto_files_path] == ['one', 'two']
    parser.parse_known_args([])
    assert tools.common_options.proto_files_path == [] and not tools.common_options.debug
    with pytest.raises(SystemExit):
        parser.parse_args(['-v'])
    assert capsys.readouterr().out.startswith('gp2040ce-hostlighting ')


def test_settings_shows_and_changes(hlp_run_on_fake_board, hlp_fake_board, capsys, monkeypatch):
    """Test hlp-settings: the display with names, each change's payload, and the board's refusal."""
    monkeypatch.setattr(tools, 'enum_names', lambda: {'AnimationNonPressedEffects': {2: 'EFFECT_RAINBOW_ROTATE'},
                                                      'AnimationPressedEffects': {1: 'PRESSEDEFFECT_RANDOM'}})
    hlp_fake_board.profiles.append([bytes([2, 0]) + b'Alt'.ljust(16, b'\x00')])
    hlp_run_on_fake_board(tools.settings)
    out = capsys.readouterr().out
    assert 'profile 1:\n  * 1 enabled Base\n    2 disabled Alt\n' in out
    assert 'animation 0 (an enabled ordinal, or off); speeds 0-10:' in out
    assert ('* 0 enabled, idle 2 (EFFECT_RAINBOW_ROTATE), pressed 1 (PRESSEDEFFECT_RANDOM), '
            'case 2 (EFFECT_RAINBOW_ROTATE), speeds 5/5/5') in out
    assert 'brightness step 5 of 0-10 (maximum 255)' in out
    hlp_run_on_fake_board(tools.settings, '--profile', '1', '--animation', 'off', '--speed', '0', '--idle', '3',
                          '--brightness', '7')
    out = capsys.readouterr().out
    assert hlp_fake_board.sent(hl.CMD_SET_PROFILE)[-1][3] == 1
    assert hlp_fake_board.sent(hl.CMD_SET_ANIMATION)[-1][3:5] == b'\xff\xff'
    assert hlp_fake_board.sent(hl.CMD_SET_ANIMATION_SPEED)[-1][3:8] == bytes([0, 0, 3, 0xFF, 0xFF])
    assert hlp_fake_board.sent(hl.CMD_SET_BRIGHTNESS)[-1][3:5] == bytes([7, 1])
    assert 'animation off' in out and 'speeds 3/5/5' in out and 'brightness step 7 of 0-10' in out
    with pytest.raises(SystemExit,
                       match=r'--profile 2: command 0x30 \(SET_PROFILE\) rejected: INVALID_ARG at request byte 3'):
        hlp_run_on_fake_board(tools.settings, '--profile', '2')


def test_settings_argument_checks(hlp_run_on_fake_board, hlp_fake_board, capsys):
    """Test that hlp-settings range-checks its values and pairs --speed with a speed."""
    for arguments, message in ((('--brightness', '255'), 'outside 0-254'), (('--animation', '-1'), 'outside 0-65535'),
                               (('--speed', '0', '--idle', '255'), 'outside 0-254')):
        with pytest.raises(SystemExit):
            hlp_run_on_fake_board(tools.settings, *arguments)
        assert message in capsys.readouterr().err
    with pytest.raises(SystemExit, match='need --speed'):
        hlp_run_on_fake_board(tools.settings, '--idle', '3')
    with pytest.raises(SystemExit, match='needs at least one of'):
        hlp_run_on_fake_board(tools.settings, '--speed', '0')
    assert not hlp_fake_board.sent(hl.CMD_SET_BRIGHTNESS) and not hlp_fake_board.sent(hl.CMD_SET_ANIMATION_SPEED)


def test_settings_holds_an_unsaved_brightness_step(hlp_run_on_fake_board, hlp_fake_board, capsys):
    """Test hlp-settings --unsaved: an unsaved SET_BRIGHTNESS held under the claim, then the saved step again."""
    hlp_run_on_fake_board(tools.settings, '--brightness', '2', '--unsaved', '--seconds', '2', '--exclusive')
    out = capsys.readouterr().out
    (brightness,) = hlp_fake_board.sent(hl.CMD_SET_BRIGHTNESS)
    assert brightness[3:5] == bytes([2, 0]) and brightness[2] == hl.FLAG_HOLDER | 2 << hl.FLAG_GENERATION_SHIFT
    claims = hlp_fake_board.sent(hl.CMD_CLAIM)
    assert [claim[2] & hl.FLAG_KEEPALIVE for claim in claims] == [0] + [hl.FLAG_KEEPALIVE] * 3 + [0]
    assert claims[0][24] == hl.CLAIM_EXCLUSIVE and claims[-1][3] == hl.CLAIM_GIVE_UP
    assert hlp_fake_board.holder_token == 0
    assert 'brightness step 2 shown, unsaved, for 2.0 s' in out and 'brightness step 5 of 0-10 (maximum 255)\n' in out
    for arguments, message in ((('--unsaved',), '--unsaved takes --brightness alone'),
                               (('--brightness', '2', '--unsaved', '--profile', '1'), '--unsaved takes --brightness'),
                               (('--brightness', '2', '--exclusive'), '--force and --exclusive go with --unsaved')):
        with pytest.raises(SystemExit, match=message):
            hlp_run_on_fake_board(tools.settings, *arguments)


def test_watch_prints_what_each_event_changes(hlp_run_on_fake_board, hlp_fake_board, capsys):
    """Test hlp-watch: SUBSCRIBE, page 1 first, events older than it and other events dropped, then changes."""
    hlp_fake_board.before[hl.CMD_SUBSCRIBE] = [state_event(profile=4)]   # during the SUBSCRIBE wait
    hlp_fake_board.after[hl.CMD_SUBSCRIBE] = [state_event(profile=5)]    # during the page 1 wait
    hlp_fake_board.after[hl.CMD_GET_PAGE] = [state_event(0x00000002, profile=2), other_event(),
                                             state_event(0x00000002, profile=2),
                                             state_event(0x00000002, profile=2, animation=0xFFFF, holder_token=5,
                                                         application_name=b'MESH')]
    hlp_run_on_fake_board(tools.watch, '--seconds', '0.05')
    lines = capsys.readouterr().out.splitlines()
    assert hlp_fake_board.subscribed == 1
    assert lines[0].endswith('takeover_live False, brightness_unsaved False, animations_idle False, input_mode 0, '
                             'profile 1, player 1, animation 0, brightness_step_shown 5, takeover_number 3, '
                             'saved_brightness_step 5, holder_token 0x00000000, application_name , '
                             'exclusive False, claim_generation 1, takeover 0, keepalive_ms 2000, '
                             'brightness_applied True, token 0x1234ABCD')
    assert 'ordinal' not in lines[0]
    assert lines[1].endswith(' profile 1 -> 2, token 0x1234ABCD -> 0x00000002')
    assert lines[2].endswith(' no change')
    assert lines[3].endswith(' animation 0 -> off, holder_token 0x00000000 -> 0x00000005, '
                             'application_name  -> MESH')
    assert len(lines) == 4


# -- streaming ------------------------------------------------------------------

def test_stream_fills_streams_and_gives_up(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test hlp-stream: three fills and a rainbow of LED runs under a renewed claim, then the give up."""
    hlp_run_on_model_board(tools.stream, '--seconds', '2')
    out = capsys.readouterr().out
    assert 'filled red\nfilled green\nfilled blue\n' in out
    assert 'rainbow: 120 of 120 frames acknowledged (4 LEDs)' in out and 'given up' in out
    held = 2 << hl.FLAG_GENERATION_SHIFT                      # the claim's generation, on every request
    frames = [stage for stage in hlp_model_board.sent(hl.CMD_STAGE) if stage[2] == hl.FLAG_COMMIT_AFTER | held]
    assert len(frames) == 120 and frames[0][3:7] == bytes([hl.ADDRESSING_LED_RUN, 0, 0, 4])
    renewals = [claim for claim in hlp_model_board.sent(hl.CMD_CLAIM) if claim[2] == hl.FLAG_KEEPALIVE | held]
    assert len(renewals) >= 4
    assert not hlp_model_board.live and hlp_model_board.holder_token == 0 and not hlp_model_board.sent(hl.CMD_RELEASE)


def test_stream_timeout_test_times_the_keepalive(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test hlp-stream --timeout-test: one renewal, then silence until the 2 s keepalive and lease lapse."""
    hlp_run_on_model_board(tools.stream, '--seconds', '0.5', '--timeout-test')
    out = capsys.readouterr().out
    assert 'silent, waiting for the keepalive timeout' in out and 'takeover ended after 2.0' in out
    requests = hlp_model_board.requests
    renewal = max(index for index, request in enumerate(requests) if request[0] == hl.CMD_CLAIM)
    assert requests[renewal][2:4] == bytes([hl.FLAG_KEEPALIVE | 2 << hl.FLAG_GENERATION_SHIFT, hl.CLAIM_TAKE])
    # then page 1 reads alone, with no generation: the lapsed claim's would be refused
    assert requests[renewal + 1:] and {request[:3:2] for request in requests[renewal + 1:]} == {
        bytes([hl.CMD_GET_PAGE, 0])}
    assert hlp_model_board.holder_token == 0 and 'given up' not in out


def test_stream_counts_frames_the_board_refuses(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that a refused frame is counted, not fatal: here the extent reaches past the addressable limit."""
    hlp_model_board.summary = summary_record(led_extent=120, grid_width=2, grid_height=2, profiles=1, controls=3,
                                             lit_controls=1, unlit_controls=2, lights=2, animations=1)
    hlp_run_on_model_board(tools.stream, '--seconds', '0.25')
    out = capsys.readouterr().out
    assert 'rainbow: 0 of 15 frames acknowledged (120 LEDs)\n' in out
    assert '  the first unacknowledged frame: command 0x10 (STAGE) rejected: INVALID_ARG at request byte 4\n' in out


def test_stream_frame_size(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test hlp-stream --leds: 40 LEDs are three LED runs per frame, whatever the extent."""
    hlp_run_on_model_board(tools.stream, '--seconds', '0.25', '--leds', '40')
    assert 'rainbow: 15 of 15 frames acknowledged (40 LEDs)\n' in capsys.readouterr().out
    frame = [stage[3:7] for stage in hlp_model_board.sent(hl.CMD_STAGE)][-3:]
    assert frame == [bytes([hl.ADDRESSING_LED_RUN, 0, 0, 19]), bytes([hl.ADDRESSING_LED_RUN, 19, 0, 19]),
                     bytes([hl.ADDRESSING_LED_RUN, 38, 0, 2])]


def test_buttons_runs_three_phases(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test hlp-buttons: lit actions, a light run, then the overlay. Giving up restores whole-frame."""
    hlp_run_on_model_board(tools.buttons, '--seconds', '1')
    out = capsys.readouterr().out
    assert 'phase 1: 1 of 1 lit actions, one hue each' in out
    assert 'phase 2: 2 lights, one hue each' in out
    assert 'phase 3: overlay, the board lights none of B1-B4, L1, R1, L2 and R2' in out and 'given up' in out
    overlays = [configure[3] for configure in hlp_model_board.sent(hl.CMD_CONFIGURE)]
    assert overlays == [0, 1] and hlp_model_board.overlay == 0
    stages = hlp_model_board.sent(hl.CMD_STAGE)
    assert stages[1][3:7] == bytes([hl.ADDRESSING_LIGHT_RUN, 0, 0, 2])
    assert len(stages) == 2                                    # the model board lights none of those buttons
    assert hlp_model_board.holder_token == 0 and not hlp_model_board.live


def test_buttons_overlay_takes_the_buttons_the_board_lights(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that hlp-buttons' third phase stages the lit buttons among B1-B4, L1, R1, L2 and R2, and no others."""
    hlp_model_board.controls[0] = control_record(2, 5, grid_rectangle=(1, 0, 1, 0), first_led=0, first_light=0,
                                                 light_count=1)
    hlp_model_board.controls[1] = control_record(12, 9, grid_rectangle=(0, 1, 0, 1), first_led=1, first_light=1,
                                                 light_count=1)
    hlp_model_board.lights[0] = light_record(0, pin=2, action=5, instance=1, instances=1, x=1, y=0)
    hlp_run_on_model_board(tools.buttons, '--seconds', '1')
    assert 'phase 3: overlay, B1 L1 white over the animations (1 applied)' in capsys.readouterr().out
    stages = hlp_model_board.sent(hl.CMD_STAGE)
    assert stages[2][3:5] == bytes([hl.ADDRESSING_ACTION, 2])
    assert stages[2][5:7] == bytes([5, 0]) and stages[2][10:12] == bytes([9, 0])


def test_overlay_buttons_follow_the_proto_names_when_given():
    """Test that a button's action comes from the .proto names when present, and from enums.proto's value if not."""
    controls = [records.ControlRecord(ordinal=ordinal, input=records.ControlInput(
        key=ordinal, source='gpio', index=ordinal, action=action, duplicated=False, lit=lit))
        for ordinal, (action, lit) in enumerate(((40, True), (6, True), (7, False), (1, True)))]
    assert tools.overlay_buttons(controls, {}) == [('B2', 6)]
    names = {'GpioAction': {40: 'BUTTON_PRESS_B1', 6: 'BUTTON_PRESS_B2', 7: 'BUTTON_PRESS_B3'}}
    assert tools.overlay_buttons(controls, names) == [('B1', 40), ('B2', 6)]


@pytest.mark.parametrize('command, steal_at', [('stream', 0.5), ('stream', 3.5), ('buttons', 0.5)])
def test_streaming_tools_stop_when_taken_over(hlp_run_on_model_board, hlp_model_board, capsys, command, steal_at):
    """Test that hlp-stream, in its fills or its rainbow, and hlp-buttons stop, giving nothing up, when taken over."""
    hlp_model_board.steal_at = steal_at
    hlp_run_on_model_board(getattr(tools, command), '--seconds', '3')
    out = capsys.readouterr().out
    assert "'Other' took the lights over" in out and 'given up' not in out
    assert not hlp_model_board.sent(hl.CMD_RELEASE) and hlp_model_board.holder_token == 0xB2


@pytest.mark.parametrize('command', ['stream', 'buttons'])
def test_streaming_tools_leave_another_holder_alone(hlp_run_on_model_board, hlp_model_board, command):
    """Test that hlp-stream and hlp-buttons refuse while another application holds the claim."""
    hlp_model_board.holder_token, hlp_model_board.holder_name = 0xA1, b'MESH'.ljust(16, b'\x00')
    with pytest.raises(SystemExit, match="'MESH' holds the lights"):
        hlp_run_on_model_board(getattr(tools, command))
    assert not hlp_model_board.sent(hl.CMD_CONFIGURE) and not hlp_model_board.sent(hl.CMD_STAGE)
    assert hlp_model_board.holder_token == 0xA1


@pytest.mark.parametrize('command, after, overlays', [('stream', 3.5, [0]), ('buttons', 2.5, [0, 1])])
def test_streaming_tools_give_up_on_interrupt(hlp_run_on_model_board, hlp_model_board, capsys, monkeypatch,
                                              command, after, overlays):
    """Test that a Ctrl-C in either phase gives up, restoring the animations and the CONFIGURE defaults."""
    sleep = hlp_model_board.sleep

    def interrupt(seconds):
        if hlp_model_board.now >= after:
            raise KeyboardInterrupt
        sleep(seconds)
    monkeypatch.setattr(hlp_model_board, 'sleep', interrupt)
    with pytest.raises(SystemExit, match='^130$'):
        hlp_run_on_model_board(getattr(tools, command), '--seconds', '1')
    assert 'claim given up, on-board animations restored' in capsys.readouterr().out
    assert hlp_model_board.holder_token == 0 and not hlp_model_board.live
    assert [configure[3] for configure in hlp_model_board.sent(hl.CMD_CONFIGURE)] == overlays
    assert (hlp_model_board.overlay, hlp_model_board.keepalive_ms, hlp_model_board.brightness) == (0, 2000, 1)


def test_a_closed_output_pipe_ends_quietly(monkeypatch, capsys):
    """Test that a command with its output reader exited ends with status 1, no message and stdout at devnull."""
    class Output:
        def fileno(self):
            return 1

    redirected: list = []
    monkeypatch.setattr(tools.os, 'open', lambda path, flags: 7)
    monkeypatch.setattr(tools.os, 'dup2', lambda source, target: redirected.append((source, target)))
    monkeypatch.setattr(tools.sys, 'stdout', Output())

    @tools.hlp_command
    def closed():
        raise BrokenPipeError(32, 'Broken pipe')
    with pytest.raises(SystemExit) as stopped:
        closed()
    assert stopped.value.code == 1 and redirected == [(7, 1)]


def test_a_mistyped_option_is_refused(hlp_run_on_fake_board, hlp_fake_board, capsys):
    """Test that an option no command defines ends the command before anything is sent."""
    with pytest.raises(SystemExit) as exit_info:
        hlp_run_on_fake_board(tools.reboot, 'gamepad', '--board-ID', 'FFFF')
    assert exit_info.value.code == 2 and not hlp_fake_board.requests
    assert 'unrecognized arguments: --board-ID FFFF' in capsys.readouterr().err
    with pytest.raises(SystemExit) as exit_info:
        hlp_run_on_fake_board(tools.set_light, '0:FF0000', '--colour', 'red')
    assert exit_info.value.code == 2


def test_ping_and_caps_skip_the_pages_a_board_does_not_list(hlp_run_on_fake_board, capsys, monkeypatch):
    """Test that a board without page 0 or page 2 is still pinged and decoded, without those pages."""
    from gp2040ce_hostlighting import testing

    def without_pages(*arguments, **options):
        reply = bytearray(hello(*arguments, **options))
        reply[35] &= ~0b101                 # pages 0 and 2 not listed
        return bytes(reply)
    hello = testing.hello_reply
    monkeypatch.setattr(testing, 'hello_reply', without_pages)
    hlp_run_on_fake_board(tools.ping)
    hlp_run_on_fake_board(tools.caps)
    out = capsys.readouterr().out
    assert out.startswith('? (?), board ID') and 'page 0' not in out and 'page 2' not in out and 'page 11' in out


def test_debug_logging_prints_each_claim_line_once(hlp_run_on_fake_board, capsys, monkeypatch):
    """Test that with -d the claim's INFO lines are printed on stdout and left out of the log on stderr."""
    import logging
    monkeypatch.setattr(logging.root, 'handlers', [])
    monkeypatch.setattr(logging.root, 'level', logging.root.level)
    hlp_run_on_fake_board(tools.fill, '-d', '--seconds', '0', '00FF00')
    captured = capsys.readouterr()
    assert 'claim given up' in captured.out and 'claim given up' not in captured.err


def test_buttons_stops_when_the_overlay_finds_the_claim_lost(hlp_run_on_model_board, hlp_model_board, monkeypatch,
                                                             capsys):
    """Test that hlp-buttons stages nothing in phase 3 once configure() found another application holding."""
    write = hlp_model_board.write

    def take_over_after_phase_3_clear(data):
        written = write(data)
        if data[1] == hl.CMD_CLEAR and len(hlp_model_board.sent(hl.CMD_CLEAR)) == 2:
            hlp_model_board.take_over()
        return written
    monkeypatch.setattr(hlp_model_board, 'write', take_over_after_phase_3_clear)
    hlp_run_on_model_board(tools.buttons, '--seconds', '1')
    assert 'phase 3' not in capsys.readouterr().out and len(hlp_model_board.sent(hl.CMD_STAGE)) == 2


def test_a_stream_hello_refused_goes_to_the_claim(hlp_model_board):
    """Test that hlp-stream passes a status 4 reply to its HELLO after a missed frame to the claim."""
    from gp2040ce_hostlighting.claim import ClaimState, LightsClaim
    from gp2040ce_hostlighting.testing import fake_device
    claim = LightsClaim(fake_device(hlp_model_board), 'hlp-stream', wait_when_taken_over=False,
                        clock=hlp_model_board.time).take()
    hlp_model_board.take_over()
    tools._answers_hello(claim)
    assert claim.state is ClaimState.LOST and claim.device.generation == 0


def test_caps_prints_every_decoded_page_as_json(hlp_run_on_fake_board, hlp_fake_board, capsys):
    """Test hlp-caps --json: HELLO and each page the board lists, the records' groups by field name."""
    hlp_run_on_fake_board(tools.caps, '--json')
    data = json.loads(capsys.readouterr().out)
    assert data['hello']['major'] == 2 and data['hello']['board_id'] == hlp_fake_board.board_id.hex().upper()
    assert set(data['pages']) == {'0', '1', '2', '8', '9', '10', '11', '12'}
    assert data['pages']['11'][0]['leds'] == {'first_led': 0, 'led_count': 1}
    assert data['pages']['1'][0]['controller']['holder_token'] == 0 and data['pages']['0']['board_config']
