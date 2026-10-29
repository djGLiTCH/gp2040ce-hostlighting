# gp2040ce-hostlighting (GP2040-CE Host Lighting library and tools)
# Filename: test_conformance.py
# Folder: tests
# https://github.com/djGLiTCH/gp2040ce-hostlighting
# SPDX-FileCopyrightText: (C) 2026 Jacob Simpson (DJ GLiTCH)
# SPDX-License-Identifier: GPL-3.0-or-later
# See LICENSES/GPL-3.0-or-later.txt at the repository root.

"""Test hlp-conformance and hlp-bench against the model board."""
import json
import struct
from types import SimpleNamespace

import pytest

from gp2040ce_hostlighting import claim as claim_module
from gp2040ce_hostlighting import conformance as hl_conformance
from gp2040ce_hostlighting import protocol as hl
from gp2040ce_hostlighting import testing
from gp2040ce_hostlighting.claim import ClaimState, LightsClaim
from gp2040ce_hostlighting.testing import (control_record, fake_device, hello_reply, light_record, name_record,
                                           summary_record)
from gp2040ce_hostlighting.tools import conformance


def test_conformance_passes_on_a_conforming_board(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that every check passes on a board that follows the rules, and the board is left as found."""
    hlp_model_board.after[0xFF] = [hello_reply(77)]     # another application's reply during the framing checks
    hlp_run_on_model_board(conformance.conformance, '--quick')
    out = capsys.readouterr().out
    assert 'FAIL' not in out and 'RESULT: ALL PASS (189 passed, 0 failed)' in out
    assert 'input modes: 0 XINPUT (lighting once a PC is detected), 3 KEYBOARD (lighting), 4 PS4\n' in out
    assert '1 reports for another application ignored' in out and '20 pipelined: 16 answered' in out
    assert 'expiry seen after 3' in out and 'round trip: median 1.00 ms' in out
    assert not hlp_model_board.live and hlp_model_board.holder_token == 0
    assert (hlp_model_board.overlay, hlp_model_board.keepalive_ms, hlp_model_board.brightness) == (0, 2000, 1)


def test_conformance_settings_change_and_restore(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test --settings: brightness, animation and a speed change live, send events and are put back."""
    before = (hlp_model_board.state[0][2:7], list(hlp_model_board.animations))
    hlp_run_on_model_board(conformance.conformance, '--quick', '--settings')
    out = capsys.readouterr().out
    assert 'Settings (writes the settings flash):' in out and 'RESULT: ALL PASS (200 passed, 0 failed)' in out
    hlp_model_board._refresh()
    assert (hlp_model_board.state[0][2:7], list(hlp_model_board.animations)) == before


def test_conformance_reports_a_board_that_breaks_a_rule(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that a board keeping a takeover alive on any command fails the keepalive checks."""
    apply_flags = hlp_model_board._apply_flags

    def refresh_on_everything(command, flags):
        apply_flags(command, flags)
        hlp_model_board.refreshed = hlp_model_board.now
    hlp_model_board._apply_flags = refresh_on_everything
    with pytest.raises(SystemExit) as exited:
        hlp_run_on_model_board(conformance.conformance, '--quick')
    out = capsys.readouterr().out
    assert exited.value.code == 1 and 'FAIL  HELLO alone does not: it expires' in out
    assert 'RESULT: FAIL' in out


def test_conformance_holds_the_claim_while_it_runs(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that every command that drives the LEDs or configures is sent under the claim, but the give up."""
    answer, sent = hlp_model_board.answer, []

    def record(command, sequence, request):
        sent.append((command, hlp_model_board.holder_token))
        return answer(command, sequence, request)
    hlp_model_board.answer = record
    hlp_run_on_model_board(conformance.conformance, '--quick')
    assert 'RESULT: ALL PASS' in capsys.readouterr().out
    first = hlp_model_board.sent(hl.CMD_CLAIM)[0]
    token = struct.unpack_from('<I', first, 4)[0]
    assert first[3] == hl.CLAIM_TAKE and first[8:24].rstrip(b'\x00') == b'hlp-conformance'
    drives = (hl.CMD_CONFIGURE, hl.CMD_STAGE, hl.CMD_UNSTAGE, hl.CMD_FILL, hl.CMD_CLEAR, hl.CMD_COMMIT,
              hl.CMD_RELEASE)
    # unclaimed, only the CONFIGURE a first claim is then seen to put back
    assert {command for command, holder in sent if command in drives and not holder} == {hl.CMD_CONFIGURE}
    driving = [holder for command, holder in sent if command in drives and holder]
    # under its own claim, or under the other token the claim checks take over with
    other = token ^ 0xFFFFFFFF
    assert set(driving) == {token, other} and driving.count(token) > 10 * driving.count(other)
    assert sent[-1] == (hl.CMD_CLAIM, token)                # the give up: nothing follows it


def test_conformance_catches_a_claim_that_keeps_the_takeover(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that a board where the holder's CLAIM keeps a takeover alive fails the check for it."""
    claim = hlp_model_board._claim

    def refreshing(ok, request):
        hlp_model_board.refreshed = hlp_model_board.now
        return claim(ok, request)
    hlp_model_board._claim = refreshing
    with pytest.raises(SystemExit):
        hlp_run_on_model_board(conformance.conformance, '--quick')
    assert "FAIL  the holder's CLAIM alone does not keep it: it expires, the claim stays" in capsys.readouterr().out


def test_conformance_catches_a_takeover_that_never_expires(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that the claim's renewals leave a takeover's expiry to its own timer: one that never expires fails."""
    tick = hlp_model_board._tick

    def no_expiry():
        hlp_model_board.refreshed = hlp_model_board.now
        tick()
    hlp_model_board._tick = no_expiry
    with pytest.raises(SystemExit):
        hlp_run_on_model_board(conformance.conformance, '--quick')
    out = capsys.readouterr().out
    assert 'FAIL  HELLO alone does not: it expires' in out and 'FAIL  the takeover ends at the timeout' in out
    assert 'PASS  the lease lapses after the timeout' in out


def test_conformance_catches_a_board_ignoring_the_exclusive_claim(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that a board refusing nothing under an exclusive claim fails the refusal checks."""
    hlp_model_board._refused = lambda command, flags: False
    with pytest.raises(SystemExit):
        hlp_run_on_model_board(conformance.conformance, '--quick')
    out = capsys.readouterr().out
    assert ('FAIL  without HOLDER, changes are refused before their arguments are checked: status 4 with the holder'
            in out and 'FAIL  with KEEPALIVE or COMMIT_AFTER they are' in out)
    assert 'PASS  page 1 shows the exclusive claim' in out


def test_conformance_catches_an_unsaved_step_that_never_ends(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that a board keeping an unsaved brightness step past its keepalive fails the check for it."""
    tick = hlp_model_board._tick

    def no_lapse():
        hlp_model_board.unsaved_refreshed = hlp_model_board.now
        tick()
    hlp_model_board._tick = no_lapse
    with pytest.raises(SystemExit):
        hlp_run_on_model_board(conformance.conformance, '--quick')
    out = capsys.readouterr().out
    assert 'FAIL  it ends a keepalive timeout after the last refresh: the saved step again' in out
    assert 'PASS  0xFF shows the saved step at once' in out and 'PASS  a change of holder ends it' in out


def test_conformance_catches_a_board_that_saves_every_step(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that a board saving an unsaved SET_BRIGHTNESS fails the unsaved checks."""
    set_brightness = hlp_model_board._set_brightness

    def saving(request):
        return set_brightness(request[:4] + bytes([1]) + request[5:])
    hlp_model_board._set_brightness = saving
    with pytest.raises(SystemExit):
        hlp_run_on_model_board(conformance.conformance, '--quick')
    assert 'FAIL  an unsaved step is shown at once, flagged unsaved' in capsys.readouterr().out


def test_conformance_catches_a_refusal_without_the_holder(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that a status 4 reply without the holder at [3..23] fails the refusal check."""
    hlp_model_board._claimed = lambda command, sequence: bytes([command | hl.REPLY_FLAG, sequence, hl.STATUS_CLAIMED,
                                                               *bytes(61)])
    with pytest.raises(SystemExit):
        hlp_run_on_model_board(conformance.conformance, '--quick')
    out = capsys.readouterr().out
    assert 'FAIL  without HOLDER, changes are refused before their arguments are checked' in out
    assert 'PASS  with KEEPALIVE or COMMIT_AFTER they are' in out


def test_conformance_catches_a_take_over_with_flags_that_do_not_act(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that a board ignoring the COMMIT_AFTER of a take over sent without HOLDER fails the check for it."""
    hlp_model_board._flags_act = lambda command, request, guarded: not guarded
    with pytest.raises(SystemExit):
        hlp_run_on_model_board(conformance.conformance, '--quick')
    out = capsys.readouterr().out
    assert 'FAIL  without HOLDER, a take over has its COMMIT_AFTER act for the taker' in out
    assert "PASS  a take over records the taker's claim flags" in out


def test_conformance_passes_holding_an_exclusive_claim(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test hlp-conformance --exclusive: every check passes under its own exclusive claim, given up after."""
    hlp_run_on_model_board(conformance.conformance, '--quick', '--exclusive')
    out = capsys.readouterr().out
    assert 'FAIL' not in out and 'RESULT: ALL PASS (189 passed, 0 failed)' in out
    assert hlp_model_board.sent(hl.CMD_CLAIM)[0][24] == hl.CLAIM_EXCLUSIVE and hlp_model_board.holder_token == 0


def test_conformance_pipelines_to_the_stated_queue_limit(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that the pipelining check sends as many requests as HELLO's queue limit, and only notes more."""
    hlp_model_board.queue_limit = 8
    hlp_run_on_model_board(conformance.conformance, '--quick')
    out = capsys.readouterr().out
    assert 'PASS  pipelined requests up to the queue limit all answered' in out and '12 pipelined: 8 answered' in out


def test_conformance_reports_a_board_that_refuses_at_a_higher_offset(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that a CLAIM with a wrong action and token 0 must be refused at the action, the lower offset."""
    claim = hlp_model_board._claim

    def token_first(ok, request):
        if not struct.unpack_from('<I', request, 4)[0]:
            return hlp_model_board._invalid(ok, 4)
        return claim(ok, request)
    hlp_model_board._claim = token_first
    with pytest.raises(SystemExit):
        hlp_run_on_model_board(conformance.conformance, '--quick')
    assert 'FAIL  several invalid bytes: status 2 at the lowest  (0x03 0300000000: status 2 at [4])' in (
        capsys.readouterr().out)


def test_conformance_passes_a_board_with_later_additions(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that a board with a command, page, pixel format, event and field groups beyond the library's passes.

    The unlisted probes skip what it lists, and mask 0 takes its added groups.
    """
    answer = hlp_model_board.answer
    hlp_model_board.later_groups = {hl.PAGE_SUMMARY: b'\x01\x02', hl.PAGE_ANIMATIONS: b'\x03'}
    event_1 = []

    def later(command, sequence, request):
        if command == 0x06:                                 # a later session command
            return bytes([command | hl.REPLY_FLAG, sequence, hl.STATUS_OK]).ljust(hl.REPORT_SIZE, b'\x00')
        reply = answer(command, sequence, request)
        if command == hl.CMD_SUBSCRIBE:
            if request[3] & 0b10:
                event_1.append(True)                        # a later event 1, enabled when asked
            reply = reply[:3] + bytes([reply[3] | (0b10 if event_1 else 0)]) + reply[4:]
        if command == hl.CMD_HELLO:
            reply = bytearray(reply)
            reply[19] |= 1 << 6                             # commands bitmap: 0x06
            reply[35] |= 1 << 4                             # pages bitmap: page 4
            reply[41] |= 1 << 3                             # pixel formats bitmap: format 3
            reply[43] |= 1 << 1                             # events bitmap: event 1
            reply[45] |= 0b10 if event_1 else 0
            reply = bytes(reply)
        return reply
    hlp_model_board.answer = later
    hlp_run_on_model_board(conformance.conformance, '--quick')
    out = capsys.readouterr().out
    assert 'RESULT: ALL PASS (189 passed, 0 failed)' in out
    assert 'commands beyond 2.0: [6]' in out and 'pages beyond 2.0: [4]' in out
    assert 'pixel formats beyond 2.0: [3]' in out and 'events beyond 2.0: [1]' in out
    assert 'unlisted command 0x07: status 1' in out and 'unlisted page 5: status 2 at [3]' in out
    formats = [request[3] >> 4 for request in hlp_model_board.sent(hl.CMD_STAGE) + hlp_model_board.sent(hl.CMD_FILL)]
    assert 3 not in formats and 4 in formats and 15 in formats


def test_conformance_reports_a_missing_capability(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that a HELLO leaving out a command and request flag the library expects fails both bitmaps."""
    answer = hlp_model_board.answer

    def without_brightness(command, sequence, request):
        reply = answer(command, sequence, request)
        if command == hl.CMD_HELLO:
            reply = bytearray(reply)
            reply[19 + hl.CMD_SET_BRIGHTNESS // 8] &= ~(1 << hl.CMD_SET_BRIGHTNESS % 8)
            reply[18] &= ~hl.FLAG_HOLDER
            reply = bytes(reply)
        return reply
    hlp_model_board.answer = without_brightness
    with pytest.raises(SystemExit):
        hlp_run_on_model_board(conformance.conformance, '--quick')
    out = capsys.readouterr().out
    assert "FAIL  commands bitmap lists all of 2.0's  (missing [51])" in out
    assert "FAIL  request flags bitmap lists all of 2.0's  (missing [3])" in out


def test_conformance_fails_a_page_stride_and_goes_on(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that a page reply at a stride its groups cannot have fails that page's check, and the pages after run."""
    answer = hlp_model_board.answer

    def wide_profiles(command, sequence, request):
        reply = answer(command, sequence, request)
        if command == hl.CMD_GET_PAGE and reply[3] == hl.PAGE_PROFILES:
            reply = reply[:9] + bytes([reply[9] + 1]) + reply[10:]
        return reply
    hlp_model_board.answer = wide_profiles
    with pytest.raises(SystemExit):
        hlp_run_on_model_board(conformance.conformance, '--quick')
    out = capsys.readouterr().out
    assert 'FAIL  page 9: record of 18 bytes  (page 9 has records of 19 bytes for field groups 0x0001' in out
    assert 'PASS  page 10: every mask, stride, count, echo and token' in out


def test_conformance_catches_a_no_reply_request_left_undone(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that a board skipping a request sent with NO_REPLY fails the check that it still executes."""
    answer = hlp_model_board.answer

    def skip_unanswered(command, sequence, request):
        if request[2] & hl.FLAG_NO_REPLY and command == hl.CMD_COMMIT:
            return None
        return answer(command, sequence, request)
    hlp_model_board.answer = skip_unanswered
    with pytest.raises(SystemExit):
        hlp_run_on_model_board(conformance.conformance, '--quick')
    assert 'FAIL  a NO_REPLY request still executes: COMMIT starts a takeover' in capsys.readouterr().out


def test_conformance_reports_a_timeout_not_clamped(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that a board keeping a CONFIGURE timeout below 100 ms fails the timeout rule."""
    configure = hlp_model_board._configure

    def unclamped(ok, request):
        reply = configure(ok, request)
        if struct.unpack_from('<H', request, 4)[0] == 50:
            hlp_model_board.keepalive_ms = 50
            reply = reply[:4] + struct.pack('<H', 50) + reply[6:]
        return reply
    hlp_model_board._configure = unclamped
    with pytest.raises(SystemExit):
        hlp_run_on_model_board(conformance.conformance, '--quick')
    assert 'FAIL  CONFIGURE timeout: 0 is 2000 ms, clamped to 100-10000  ([2000, 50, 10000])' in (
        capsys.readouterr().out)


def test_conformance_reports_pages_that_disagree(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that a Summary total that disagrees with its page fails the cross-checks."""
    hlp_model_board.summary = summary_record(led_extent=4, grid_width=2, grid_height=2, profiles=1, controls=3,
                                             lit_controls=1, unlit_controls=2, lights=3, animations=1)
    with pytest.raises(SystemExit):
        hlp_run_on_model_board(conformance.conformance, '--quick')
    assert 'FAIL  page 12 total = Summary lights' in capsys.readouterr().out


def test_conformance_reports_page_flags_and_ranges(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test a page 9 flag beyond bit 0, a page 13 speed above the Summary's steps and an unlisted profile."""
    hlp_model_board.profiles = [[bytes([2, 0x03]) + b'Base'.ljust(16, b'\x00')]]
    hlp_model_board.animations = [[bytes([1, 2, 1, 2]), bytes([5, 11, 5])]]
    with pytest.raises(SystemExit):
        hlp_run_on_model_board(conformance.conformance, '--quick')
    out = capsys.readouterr().out
    assert 'FAIL  page 9 flags: defined bits alone' in out and 'FAIL  page 1 profile listed on page 9' in out
    assert 'FAIL  page 13 speeds within the Summary speed steps' in out
    assert 'PASS  page 10 flags: defined bits alone' in out
    assert 'PASS  Holder claim flags: defined bits alone' in out


def test_conformance_reports_flags_the_specification_does_not_define(hlp_run_on_model_board, hlp_model_board,
                                                                     capsys):
    """Test a white channel on a GRB chain, a Summary flag beyond bit 0 and a State flag beyond bit 2."""
    hlp_model_board.summary = summary_record(white_channel=0x03)
    answer = hlp_model_board.answer

    def with_flag_bit_3(command, sequence, request):
        reply = answer(command, sequence, request)
        if reply and command == hl.CMD_GET_PAGE and request[3] == hl.PAGE_STATE and reply[2] == hl.STATUS_OK:
            reply = reply[:12] + bytes([reply[12] | 0x08]) + reply[13:]
        return reply
    hlp_model_board.answer = with_flag_bit_3
    with pytest.raises(SystemExit):
        hlp_run_on_model_board(conformance.conformance, '--quick')
    out = capsys.readouterr().out
    assert 'FAIL  Summary flags: bit 0 alone' in out and 'FAIL  State flags: bits 0 to 2 alone' in out
    assert 'FAIL  Summary white channel: set for a GRBW or RGBW chain alone' in out


def test_conformance_reports_an_ownerless_light_with_an_action(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that a light with no owner pin must report no owner action (0x8000)."""
    hlp_model_board.lights[1] = light_record(1, 3, kind=1, action=5, x=0, y=1, slot=0)
    with pytest.raises(SystemExit):
        hlp_run_on_model_board(conformance.conformance, '--quick')
    out = capsys.readouterr().out
    assert 'FAIL  Lights owner action, flag and instances agree with Controls' in out
    assert ('light 1: (5, False, Instances(instance=None, instances=None)) vs '
            '(None, False, Instances(instance=None, instances=None))') in out


def test_conformance_reports_an_unlit_control_with_a_position(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that an unlit control must send first LED 0xFFFF and a zero rectangle."""
    hlp_model_board.controls[1][2] = struct.pack('<H', 0xFFFF) + bytes([1, 0, 1, 0])
    with pytest.raises(SystemExit):
        hlp_run_on_model_board(conformance.conformance, '--quick')
    out = capsys.readouterr().out
    assert 'FAIL  Controls agree with the Lights page (flags, instances, position, lights)' in out
    assert 'control GP12: unlit, but its position group is not FFFF 0000 0000' in out


def test_conformance_reports_a_lit_expander_control(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that an expander control, which no light follows, must not be flagged lit."""
    hlp_model_board.controls[2] = control_record(hl.KEY_EXPANDER, 1, duplicated=1, instance=2, instances=2,
                                                 light_count=0)
    with pytest.raises(SystemExit):
        hlp_run_on_model_board(conformance.conformance, '--quick')
    assert 'FAIL  only GPIO and Hall-effect controls are lit' in capsys.readouterr().out


def test_conformance_accepts_a_light_on_a_hall_effect_trigger(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that a Hall-effect trigger can own a light: its control is lit and STAGE by its key reaches it."""
    hlp_model_board.controls[2] = control_record(hl.KEY_TRIGGER, 1, duplicated=1, instance=2, instances=2,
                                                 grid_rectangle=(0, 1, 0, 1), first_led=1, first_light=1, light_count=1)
    hlp_model_board.lights[1] = light_record(1, 3, pin=hl.KEY_TRIGGER, action=1, duplicated=1, instance=2, instances=2,
                                             x=0, y=1)
    hlp_model_board.summary = summary_record(led_extent=4, grid_width=2, grid_height=2, profiles=1, controls=3,
                                             lit_controls=2, unlit_controls=1, lights=2, animations=1)
    hlp_run_on_model_board(conformance.conformance, '--quick')
    out = capsys.readouterr().out
    assert 'FAIL' not in out
    assert 'PASS  Lights owner pins are GPIO pins or Hall-effect triggers' in out
    assert 'PASS  add-on keys that own no light match no light' in out


def test_conformance_reports_controls_out_of_key_order(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that page 10 must list its controls in ascending key order."""
    board = hlp_model_board
    board.controls = [board.controls[2], board.controls[0], board.controls[1]]
    with pytest.raises(SystemExit):
        hlp_run_on_model_board(conformance.conformance, '--quick')
    assert 'FAIL  Controls in ascending key order, each key once' in capsys.readouterr().out


def test_conformance_notes_an_unknown_key(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that a control key the library does not define passes and is named: newer firmware may use it."""
    hlp_model_board.controls[1] = control_record(0x40, 13, lit=0, light_count=0)
    hlp_run_on_model_board(conformance.conformance, '--quick')
    out = capsys.readouterr().out
    assert 'keys beyond 2.0: key 0x40' in out and 'FAIL' not in out


def test_conformance_reports_a_none_key(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that 0xFF, the none value, is never a control key."""
    board = hlp_model_board
    board.controls = [board.controls[0], board.controls[2], control_record(0xFF, 13, lit=0, light_count=0)]
    with pytest.raises(SystemExit):
        hlp_run_on_model_board(conformance.conformance, '--quick')
    assert 'FAIL  no control key is 0xFF (none)' in capsys.readouterr().out


def test_conformance_reports_a_key_listed_twice(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that page 10 must list each key once."""
    board = hlp_model_board
    board.controls = [board.controls[0], board.controls[1], board.controls[1], board.controls[2]]
    with pytest.raises(SystemExit):
        hlp_run_on_model_board(conformance.conformance, '--quick')
    assert 'FAIL  Controls in ascending key order, each key once' in capsys.readouterr().out


def test_conformance_reports_a_light_owned_by_a_key_past_the_gpio_pins(hlp_run_on_model_board, hlp_model_board,
                                                                       capsys):
    """Test that a light's owner must be a GPIO pin or a Hall-effect trigger: no other input drives lights."""
    hlp_model_board.lights[1] = light_record(1, 3, pin=0x40, action=13, x=0, y=1)
    with pytest.raises(SystemExit):
        hlp_run_on_model_board(conformance.conformance, '--quick')
    assert 'FAIL  Lights owner pins are GPIO pins or Hall-effect triggers  (0x40)' in capsys.readouterr().out


def test_conformance_reports_a_lit_control_no_light_owns(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that a control flagged lit must be an owner pin on the Lights page."""
    hlp_model_board.controls[1] = control_record(12, 13, grid_rectangle=(0, 0, 0, 0), first_led=1, first_light=1,
                                                 light_count=1)
    with pytest.raises(SystemExit):
        hlp_run_on_model_board(conformance.conformance, '--quick')
    assert 'FAIL  lit control keys = the Lights owner pins that are controls' in capsys.readouterr().out


def test_conformance_reports_a_light_on_an_expander_key(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that an expander pin must own no light: the owner is refused and STAGE by its key must skip."""
    hlp_model_board.lights[1] = light_record(1, 3, pin=hl.KEY_EXPANDER, action=1, x=0, y=1)
    with pytest.raises(SystemExit):
        hlp_run_on_model_board(conformance.conformance, '--quick')
    out = capsys.readouterr().out
    assert 'FAIL  Lights owner pins are GPIO pins or Hall-effect triggers  (0xC0)' in out
    assert 'FAIL  add-on keys that own no light match no light' in out
    assert 'FAIL  UNSTAGE: add-on keys that own no light match no light' in out


def test_conformance_probes_the_guard_with_the_current_input_mode(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that the wrong-magic SET_INPUT_MODE names the mode the board is in, so a broken guard changes nothing."""
    hlp_model_board.set_state(1, bytes([3]))                # KEYBOARD, not page 2's first mode
    hlp_run_on_model_board(conformance.conformance, '--quick')
    assert 'RESULT: ALL PASS' in capsys.readouterr().out
    # the wrong-magic probe, then the lowest-offset probes
    assert [request[3] for request in hlp_model_board.sent(hl.CMD_SET_INPUT_MODE)] == [255, 3, 255, 3]


def test_conformance_sends_the_reboot_magic_only_with_target_0xff(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that the REBOOT probes send the magic only with target 0xFF, so a reboot mode added later never runs."""
    hlp_run_on_model_board(conformance.conformance, '--quick')
    assert 'RESULT: ALL PASS' in capsys.readouterr().out
    probes = [(request[3], request[4:8] == hl.MAGIC_REBOOT) for request in hlp_model_board.sent(hl.CMD_REBOOT)]
    assert probes == [(0xFF, True), (0xFF, True), (0xFF, False)]


def test_conformance_leaves_another_holder_alone(hlp_run_on_model_board, hlp_model_board):
    """Test that hlp-conformance refuses, without touching the lights, while another application holds them."""
    hlp_model_board.holder_token, hlp_model_board.holder_name = 0xA1, b'MESH'.ljust(16, b'\x00')
    with pytest.raises(SystemExit, match="'MESH' holds the lights"):
        hlp_run_on_model_board(conformance.conformance)
    assert not hlp_model_board.sent(hl.CMD_RELEASE) and not hlp_model_board.sent(hl.CMD_CONFIGURE)


def test_conformance_takes_over_with_force(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that --force takes the claim over from another application, then runs every check."""
    hlp_model_board.holder_token, hlp_model_board.holder_name = 0xA1, b'MESH'.ljust(16, b'\x00')
    hlp_model_board.leased = float('inf')                   # MESH keeps renewing its lease
    hlp_run_on_model_board(conformance.conformance, '--quick', '--force')
    assert 'RESULT: ALL PASS' in capsys.readouterr().out
    assert hlp_model_board.sent(hl.CMD_CLAIM)[0][3] == hl.CLAIM_TAKE_OVER and hlp_model_board.holder_token == 0


def test_conformance_stops_when_taken_over(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that the run ends as a failed check, giving nothing up, once another application takes over."""
    hlp_model_board.steal_at = 0.5
    with pytest.raises(SystemExit) as exited:
        hlp_run_on_model_board(conformance.conformance, '--quick')
    out = capsys.readouterr().out
    assert exited.value.code == 1 and "FAIL  the checks ran to the end  ('Other' took the lights over)" in out
    assert not hlp_model_board.sent(hl.CMD_RELEASE) and hlp_model_board.holder_token == 0xB2


def test_conformance_counts_a_lost_board_as_a_failure(hlp_run_on_model_board, hlp_model_board, capsys, caplog):
    """Test that an interface error ends the run as a failed check, and the claim is still given up."""
    hlp_model_board.disconnect_on = {hl.CMD_RELEASE}
    with pytest.raises(SystemExit) as exited:
        hlp_run_on_model_board(conformance.conformance, '--quick')
    out = capsys.readouterr().out
    lost = ('FAIL  the checks ran to the end  (the board is no longer connected (sending command 0x21 failed with '
            'device disconnected))')
    assert exited.value.code == 1 and lost in out
    assert 'could not give the claim up' not in caplog.text and hlp_model_board.holder_token == 0
    assert hlp_model_board.requests[-1][0] == hl.CMD_CLAIM and hlp_model_board.requests[-1][3] == hl.CLAIM_GIVE_UP


def test_conformance_on_a_board_without_led_output(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that a board without LED output passes: no lights, animations or steps. --settings changes nothing."""
    board = hlp_model_board
    board.controls = [control_record(2, 1, lit=0, light_count=0), control_record(12, 13, lit=0, light_count=0)]
    board.lights, board.animations = [], []
    board.summary = summary_record(led_extent=0, brightness_maximum=0, brightness_steps=0, animation_speed_steps=0,
                                   grid_width=0, grid_height=0, profiles=1, controls=2,
                                   lit_controls=0, unlit_controls=2, lights=0, animations=0)
    board.set_state(4, struct.pack('<HB', hl.ANIMATION_OFF, 0))     # animation off, brightness step 0
    board.saved_step = 0
    board.set_state(8, bytes([0]))                                  # saved step 0, as the firmware reports
    hlp_run_on_model_board(conformance.conformance, '--quick', '--settings')
    out = capsys.readouterr().out
    # no lights: the light run, entry and UNSTAGE checks have nothing to address
    assert 'no LED output, so no brightness, animation or speed to change' in out
    assert 'RESULT: ALL PASS (178 passed, 0 failed)' in out


def test_conformance_passes_a_board_with_lights_past_the_limit(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that lights wholly past the addressable LED limit, light 0 among them, fail no staging check."""
    board = hlp_model_board
    board.lights = [light_record(120, kind=3, slot=1), light_record(0, pin=2, action=1, instance=1, instances=1,
                                                                    x=1, y=0),
                    board.lights[1], light_record(121, pin=12, action=13, instance=1, instances=1)]
    board.controls = [control_record(2, 1, grid_rectangle=(1, 0, 1, 0), first_led=0, first_light=1, light_count=1),
                      control_record(12, 13, grid_rectangle=(0, 0, 0, 0), first_led=121, first_light=3, light_count=1)]
    board.summary = summary_record(led_extent=4, grid_width=2, grid_height=2, profiles=1, controls=2, lit_controls=2,
                                   unlit_controls=0, lights=4, animations=1)
    hlp_run_on_model_board(conformance.conformance, '--quick')
    out = capsys.readouterr().out
    assert 'RESULT: ALL PASS (189 passed, 0 failed)' in out
    assert 'PASS  light run: every light within the limit applies, the rest are skipped' in out


def test_conformance_passes_a_board_with_a_light_of_no_leds(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that a light with no LEDs, placed past the other lights' LEDs, adds nothing to the LED extent."""
    board = hlp_model_board
    board.lights = [*board.lights, light_record(10, led_count=0, kind=1, slot=1, x=0, y=1)]
    board.summary = summary_record(led_extent=4, grid_width=2, grid_height=2, profiles=1, controls=3, lit_controls=1,
                                   unlit_controls=2, lights=3, animations=1)
    hlp_run_on_model_board(conformance.conformance, '--quick')
    out = capsys.readouterr().out
    assert 'PASS  extent = highest LED below the limit + 1' in out
    assert 'RESULT: ALL PASS (189 passed, 0 failed)' in out


def test_bench_prints_the_figures(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test hlp-bench: round trip, paced and unthrottled frame rates under a renewed claim, then the give up."""
    hlp_run_on_model_board(conformance.bench, '--quick')
    out = capsys.readouterr().out
    assert out.startswith('claim taken\nround trip: 100 HELLO requests\nround trip: median ')
    assert '60 fps: 60 frames (4 LEDs, 1 reports per frame)\n60 fps: 60 of 60 frames acknowledged' in out
    assert 'unthrottled, pipelined: frames back to back for 1.0 s\nunthrottled, pipelined: ' in out
    assert 'round trip: median 1.00 ms, p95 1.00 ms (100 of 100 answered)' in out
    assert '60 fps: 60 of 60 frames acknowledged (4 LEDs, 1 reports per frame)' in out
    assert 'unthrottled, every report answered: ' in out and 'unthrottled, pipelined: ' in out
    assert out.endswith('claim given up, on-board animations restored\n')
    renewals = [claim[2] & 0x0F for claim in hlp_model_board.sent(hl.CMD_CLAIM)]
    assert renewals.count(hl.FLAG_KEEPALIVE) >= 2
    assert [configure[6] for configure in hlp_model_board.sent(hl.CMD_CONFIGURE)] == [0]
    assert hlp_model_board.holder_token == 0 and not hlp_model_board.live and hlp_model_board.brightness == 1


def test_bench_frame_size_and_refused_frames(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test --leds: 40 LEDs are three reports. 120 reach past the limit of 100: refused, nothing sent."""
    hlp_run_on_model_board(conformance.bench, '--quick', '--leds', '40')
    assert '(40 LEDs, 3 reports per frame)' in capsys.readouterr().out
    sent = len(hlp_model_board.requests)
    with pytest.raises(SystemExit):
        hlp_run_on_model_board(conformance.bench, '--quick', '--leds', '120')
    assert "--leds is at most the board's addressable LED limit, 100" in capsys.readouterr().err
    assert not [r for r in hlp_model_board.requests[sent:] if r[0] in (hl.CMD_CLAIM, hl.CMD_STAGE)]


def test_bench_stops_when_taken_over(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that hlp-bench stops, and gives nothing up, when another application takes the lights over."""
    hlp_model_board.steal_at = 0.5
    hlp_run_on_model_board(conformance.bench, '--quick')
    assert "'Other' took the lights over" in capsys.readouterr().out
    assert not hlp_model_board.sent(hl.CMD_RELEASE) and hlp_model_board.holder_token == 0xB2


def test_bench_gives_up_on_interrupt(hlp_run_on_model_board, hlp_model_board, capsys, monkeypatch):
    """Test that a Ctrl-C during hlp-bench gives the claim up, which restores the CONFIGURE defaults."""
    def interrupt(seconds):
        raise KeyboardInterrupt
    monkeypatch.setattr(hlp_model_board, 'sleep', interrupt)
    with pytest.raises(SystemExit, match='^130$'):
        hlp_run_on_model_board(conformance.bench, '--quick')
    assert 'claim given up, on-board animations restored' in capsys.readouterr().out
    assert hlp_model_board.holder_token == 0 and not hlp_model_board.live
    assert (hlp_model_board.overlay, hlp_model_board.keepalive_ms, hlp_model_board.brightness) == (0, 2000, 1)


def test_conformance_catches_a_generation_that_does_not_step(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that a board with a claim generation that stays the same across a change of holder fails the check."""
    set_holder = hlp_model_board._set_holder

    def same_generation(token, name, flags=0):
        generation = hlp_model_board.generation
        set_holder(token, name, flags)
        hlp_model_board.generation = generation
    hlp_model_board._set_holder = same_generation
    with pytest.raises(SystemExit):
        hlp_run_on_model_board(conformance.conformance, '--quick')
    out = capsys.readouterr().out
    assert 'FAIL  a change of holder steps the claim generation  (1 after 1)' in out
    assert ("FAIL  a request under the previous holder's generation is refused, reads included: status 4 with "
            "the holder now") in out


def test_conformance_catches_settings_that_outlast_their_holder(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that a board that keeps the CONFIGURE settings across a change of holder fails the checks for it."""
    set_holder = hlp_model_board._set_holder

    def keeping_settings(token, name, flags=0):
        settings = hlp_model_board.overlay, hlp_model_board.keepalive_ms, hlp_model_board.brightness
        set_holder(token, name, flags)
        hlp_model_board.overlay, hlp_model_board.keepalive_ms, hlp_model_board.brightness = settings
    hlp_model_board._set_holder = keeping_settings
    with pytest.raises(SystemExit):
        hlp_run_on_model_board(conformance.conformance, '--quick')
    out = capsys.readouterr().out
    assert ('FAIL  and puts the CONFIGURE settings back to their defaults  '
            '(Settings(takeover=0, keepalive_ms=300, brightness_applied=True))') in out
    assert 'FAIL  a lapse is a change of holder too: the generation steps, the settings return' in out


def test_conformance_catches_a_board_that_ignores_the_generation(hlp_run_on_model_board, hlp_model_board, capsys,
                                                                 monkeypatch):
    """Test that a board acting on a request under an outdated generation fails, shared and exclusive."""
    write = hlp_model_board.write

    def without_generation(data):
        # as a board that reads no generation: the holder's requests keep theirs, so its claim still works
        report = bytearray(data)
        if (report[3] & hl.FLAG_GENERATION) >> hl.FLAG_GENERATION_SHIFT != hlp_model_board.generation:
            report[3] &= ~hl.FLAG_GENERATION
        return write(bytes(report))
    monkeypatch.setattr(hlp_model_board, 'write', without_generation)
    with pytest.raises(SystemExit):
        hlp_run_on_model_board(conformance.conformance, '--quick')
    out = capsys.readouterr().out
    assert ("FAIL  a request under the previous holder's generation is refused, reads included: status 4 with "
            "the holder now") in out
    assert 'FAIL  and changes nothing, answered or not' in out
    assert 'FAIL  with no claim held an outdated generation is refused too: status 4, holder 0' in out
    assert "FAIL  but not a request under the previous holder's generation: status 4 with the holder now" in out


def test_conformance_catches_an_exclusive_claim_that_needs_no_generation(hlp_run_on_model_board, hlp_model_board,
                                                                         capsys, monkeypatch):
    """Test that a board with an exclusive claim HOLDER alone satisfies fails the check for it."""
    write = hlp_model_board.write

    def holder_is_enough(data):
        report = bytearray(data)
        if report[3] & hl.FLAG_HOLDER and not report[3] & hl.FLAG_GENERATION:
            report[3] |= hlp_model_board.generation << hl.FLAG_GENERATION_SHIFT
        return write(bytes(report))
    monkeypatch.setattr(hlp_model_board, 'write', holder_is_enough)
    with pytest.raises(SystemExit):
        hlp_run_on_model_board(conformance.conformance, '--quick')
    out = capsys.readouterr().out
    assert 'FAIL  HOLDER without the current generation, and the generation without HOLDER, are refused too' in out


def test_conformance_catches_a_change_counter_that_does_not_step(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test --settings on a board with Summary change counters that never move."""
    refresh = hlp_model_board._refresh

    def no_counters():
        refresh()
        hlp_model_board.summary = [hlp_model_board.summary[0], bytes(8)]
    hlp_model_board._refresh = no_counters
    with pytest.raises(SystemExit):
        hlp_run_on_model_board(conformance.conformance, '--quick', '--settings')
    out = capsys.readouterr().out
    assert "FAIL  page 13's change counter steps, and no other" in out
    assert 'FAIL  the counter steps again: it counts changes, the token follows the content' in out
    assert 'PASS  speeds restored: the token returns' in out


def test_conformance_catches_a_saved_step_that_follows_the_step_shown(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that a board with a page 1 saved step equal to the step shown fails the unsaved-brightness check."""
    show_step = hlp_model_board._show_step

    def saved_is_shown(step):
        show_step(step)
        hlp_model_board.set_state(8, bytes([step]))
    hlp_model_board._show_step = saved_is_shown
    with pytest.raises(SystemExit):
        hlp_run_on_model_board(conformance.conformance, '--quick')
    assert 'FAIL  page 1 still shows the saved step  (4 vs 5)' in capsys.readouterr().out


def test_conformance_checks_delivery_to_a_second_handle(hlp_run_on_model_board, hlp_model_board, capsys, monkeypatch):
    """Test that the two-handle check passes, fails on one-handle delivery and notes a missing second handle."""
    from gp2040ce_hostlighting import testing
    hlp_run_on_model_board(conformance.conformance, '--quick')
    out = capsys.readouterr().out
    assert "PASS  a second handle reads the reply to the first's request" in out
    assert "PASS  and the first the reply to the second's" in out and not hlp_model_board.handles
    deliver = testing._Outbox.deliver
    monkeypatch.setattr(testing._Outbox, 'deliver', lambda self, reports: None)
    with pytest.raises(SystemExit):
        hlp_run_on_model_board(conformance.conformance, '--quick')
    out = capsys.readouterr().out
    assert "FAIL  a second handle reads the reply to the first's request  (0 copies)" in out
    assert not hlp_model_board.handles                      # closed, though the check failed

    def no_second_handle(prefix=''):
        if hlp_model_board.opened:
            raise OSError('open failed')
        return testing.fake_device(hlp_model_board)
    monkeypatch.setattr(testing._Outbox, 'deliver', deliver)
    monkeypatch.setattr(conformance, 'open_device', no_second_handle)
    hlp_run_on_model_board(conformance.conformance, '--quick')
    out = capsys.readouterr().out
    assert 'not checked, since a second handle could not be opened (open failed)' in out and 'RESULT: ALL PASS' in out


def test_conformance_catches_a_first_claim_that_keeps_the_settings(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that a board with a first claim that leaves the CONFIGURE settings as they were fails the check for it."""
    set_holder = hlp_model_board._set_holder

    def keeping(token, name, flags=0):
        kept = hlp_model_board.overlay, hlp_model_board.keepalive_ms, hlp_model_board.brightness
        first = not hlp_model_board.holder_token and token
        set_holder(token, name, flags)
        if first:
            hlp_model_board.overlay, hlp_model_board.keepalive_ms, hlp_model_board.brightness = kept
    hlp_model_board._set_holder = keeping
    with pytest.raises(SystemExit):
        hlp_run_on_model_board(conformance.conformance, '--quick')
    assert 'FAIL  and each puts the CONFIGURE settings back to their defaults' in capsys.readouterr().out


def test_round_trips_count_only_the_answered(hlp_model_board):
    """Test that a HELLO refused, as after a change of holder, is not timed, and a lost board still raises."""
    class Device:
        replies = [None, hl.HostLightingRejectedError(hl.CMD_HELLO, hl.STATUS_CLAIMED, 0, hl.Holder(
            holder_token=0xB2, application_name='MESH', exclusive=False, claim_generation=3)), None]

        def request_ok(self, command):
            reply = self.replies.pop(0)
            if reply:
                raise reply

    assert len(hl_conformance.measure_round_trips(Device(), 3)) == 2
    Device.replies = [hl.HostLightingDisconnectedError('gone')]
    with pytest.raises(hl.HostLightingDisconnectedError):
        hl_conformance.measure_round_trips(Device(), 1)


def test_checks_collect_and_count_the_results():
    """Test that Checks keeps every result, counting passes and failures but not notes."""
    checks = hl_conformance.Checks()
    passed = hl_conformance.CheckResult(section='Pages', what='page 8 read', passed=True)
    assert checks.add(passed) is passed
    checks.add(hl_conformance.CheckResult(section='Pages', what='page 9 read', passed=False, detail='status 5'))
    checks.add(hl_conformance.CheckResult(section='Pages', what='3 profiles', passed=None))
    assert (checks.passed, checks.failed, len(checks.results)) == (1, 1, 3)


def claimed_on_the_board_clock(board, monkeypatch):
    """Open the model board with time on its clock, and take a claim on it."""
    timing = SimpleNamespace(sleep=board.sleep, monotonic=board.time, perf_counter=board.time)
    for module in (hl, hl_conformance, claim_module):
        monkeypatch.setattr(module, 'time', timing)
    device = fake_device(board)
    return device, LightsClaim(device, 'host', wait_when_taken_over=False).take()


def test_run_checks_yields_each_result_as_it_is_made(hlp_model_board, monkeypatch):
    """Test the library's run: results in their sections, KEEPALIVE off while it runs, and the claim still held."""
    device, claim = claimed_on_the_board_clock(hlp_model_board, monkeypatch)
    results = hl_conformance.run_checks(device, claim, quick=True)
    first = next(results)
    assert first.section == 'HELLO' and first.passed and not claim.keepalive
    rest = list(results)
    assert all(result.passed is not False for result in rest) and rest[-1].section == 'Timing'
    assert claim.keepalive and claim.state is ClaimState.HELD and hlp_model_board.holder_token == claim.token


def test_measurements_stop_once_the_claim_is_lost(hlp_model_board, monkeypatch):
    """Test that a frame refused after a takeover ends the measurement with the holder, as does a claim ended."""
    device, claim = claimed_on_the_board_clock(hlp_model_board, monkeypatch)
    assert hl_conformance.measure_paced_frames(device, claim, 4, 3) == 3
    assert hl_conformance.measure_unthrottled_frames(device, claim, 40, 0.1, False) > 0
    hlp_model_board.take_over()
    with pytest.raises(hl.HostLightingClaimedError, match="'Other' holds the lights"):
        hl_conformance.measure_paced_frames(device, claim, 4, 3)
    assert claim.state is ClaimState.LOST
    hlp_model_board.let_go()
    claim.take().give_up()
    with pytest.raises(hl.HostLightingError, match='the claim is no longer held'):
        hl_conformance.measure_unthrottled_frames(device, claim, 4, 0.1, True)
    hlp_model_board.disconnect_on = {hl.CMD_STAGE}
    with pytest.raises(hl.HostLightingDisconnectedError):
        hl_conformance.measure_paced_frames(device, claim, 4, 1)


def test_conformance_settings_put_back_after_a_lost_reply(hlp_run_on_model_board, hlp_model_board, monkeypatch,
                                                          capsys):
    """Test that --settings puts the animation back when a reply is lost while it is off."""
    write, lost = hlp_model_board.write, []

    def lose_one_reply(data):
        sent = hlp_model_board.sent(hl.CMD_SET_ANIMATION)
        if data[1] == hl.CMD_GET_PAGE and sent and sent[-1][3:5] == b'\xff\xff' and not lost:
            lost.append(True)
            hlp_model_board.requests.append(bytes(data[1:]))
            return len(data)
        return write(data)
    monkeypatch.setattr(hlp_model_board, 'write', lose_one_reply)
    with pytest.raises(SystemExit):
        hlp_run_on_model_board(conformance.conformance, '--quick', '--settings')
    assert 'FAIL  the checks ran to the end' in capsys.readouterr().out
    assert hlp_model_board.sent(hl.CMD_SET_ANIMATION)[-1][3:5] == bytes(2) and hlp_model_board.state[0][4:6] == bytes(2)


def test_conformance_sends_no_saved_step_as_a_step_above_254(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test that a board of 254 brightness steps gets no step 255 check, since 255 is the saved step."""
    summary = bytearray(hlp_model_board.summary[0])
    summary[9] = 254                # the brightness steps
    hlp_model_board.summary = [bytes(summary), hlp_model_board.summary[1]]
    try:
        hlp_run_on_model_board(conformance.conformance, '--quick')
    except SystemExit:
        pass
    assert 'SET_BRIGHTNESS above the steps' not in capsys.readouterr().out


def test_round_trips_stop_once_the_claim_is_lost(hlp_model_board):
    """Test that measure_round_trips() passes a status 4 refusal to the claim and stops once it is lost."""
    claim = LightsClaim(fake_device(hlp_model_board), 'hlp-bench', wait_when_taken_over=False,
                        clock=hlp_model_board.time).take()
    hlp_model_board.take_over()
    with pytest.raises(hl.HostLightingClaimedError, match="'Other' holds the lights"):
        hl_conformance.measure_round_trips(claim.device, 5, claim)
    assert claim.state is ClaimState.LOST


def test_conformance_prints_json_and_the_usual_lines_to_stderr(hlp_run_on_model_board, capsys):
    """Test hlp-conformance --json: the results as JSON on stdout, the usual lines on stderr."""
    hlp_run_on_model_board(conformance.conformance, '--quick', '--json')
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert (data['passed'], data['failed'], data['quick'], data['settings']) == (189, 0, True, False)
    assert sum(1 for result in data['results'] if result['passed']) == 189
    assert {'section', 'what', 'passed', 'detail'} == set(data['results'][0])
    assert 'RESULT: ALL PASS (189 passed, 0 failed)' in captured.err


def test_conformance_fails_a_keyboard_mapping_that_checks_nothing(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test page 11 listing an action twice, and SET_KEYBOARD_MAPPING that takes any range and keeps no code."""
    hlp_model_board.keyboard_actions[1] = hlp_model_board.keyboard_actions[0]
    hlp_model_board._set_keyboard_mapping = lambda request: 0
    with pytest.raises(SystemExit):
        hlp_run_on_model_board(conformance.conformance, '--quick', '--settings')
    out = capsys.readouterr().out
    assert 'FAIL  page 11 lists each action once  ([1])' in out
    assert 'FAIL  SET_KEYBOARD_MAPPING count 0: status 2 at [6]' in out
    assert 'FAIL  SET_KEYBOARD_MAPPING first record at the total under another profile' in out
    assert 'FAIL  SET_KEYBOARD_MAPPING another profile: status 3' in out
    assert 'FAIL  several invalid bytes: status 2 at the lowest' in out
    assert 'FAIL  SET_KEYBOARD_MAPPING: page 11 and the token change' in out
    assert 'PASS  SET_KEYBOARD_MAPPING of the codes shown: OK, and the token and counters unchanged' in out


def test_conformance_passes_a_board_without_keyboard_mapping_changes(hlp_run_on_model_board, hlp_model_board,
                                                                     capsys):
    """Test a board with settings that do not allow keyboard mapping changes: its checks skipped, 0x34 status 1."""
    hlp_model_board.commands.discard(hl.CMD_SET_KEYBOARD_MAPPING)
    hlp_run_on_model_board(conformance.conformance, '--quick', '--settings')
    out = capsys.readouterr().out
    assert "        command 0x34 (SET_KEYBOARD_MAPPING) not listed: the board's settings do not allow keyboard " \
           "mapping changes, so its checks are skipped" in out
    assert 'commands beyond 2.0' not in out
    assert 'PASS  unlisted command 0x34: status 1' in out
    assert 'PASS  SET_KEYBOARD_MAPPING unlisted: status 1 without HOLDER too' in out
    assert 'FAIL' not in out and 'RESULT: ALL PASS (191 passed, 0 failed)' in out
    # the framing check's request and the exclusive claim's, and no other
    assert len(hlp_model_board.sent(hl.CMD_SET_KEYBOARD_MAPPING)) == 2


def test_conformance_fails_an_unlisted_keyboard_mapping_refused_as_claimed(hlp_run_on_model_board, hlp_model_board,
                                                                           capsys):
    """Test a board without 0x34 that runs the exclusive claim's refusal before it finds the command unlisted."""
    hlp_model_board.commands.discard(hl.CMD_SET_KEYBOARD_MAPPING)
    refused = hlp_model_board._refused
    hlp_model_board._refused = lambda command, flags: command == hl.CMD_SET_KEYBOARD_MAPPING or refused(command, flags)
    with pytest.raises(SystemExit):
        hlp_run_on_model_board(conformance.conformance, '--quick')
    out = capsys.readouterr().out
    assert 'FAIL  SET_KEYBOARD_MAPPING unlisted: status 1 without HOLDER too' in out
    assert 'PASS  unlisted command 0x34: status 1' in out
    assert 'RESULT: FAIL (182 passed, 1 failed)' in out


def test_conformance_checks_keyboard_mapping_changes_a_board_lists(hlp_run_on_model_board, capsys):
    """Test that a board listing SET_KEYBOARD_MAPPING has it checked, not noted as beyond 2.0 or skipped."""
    hlp_run_on_model_board(conformance.conformance, '--quick')
    out = capsys.readouterr().out
    assert 'commands beyond 2.0' not in out and '(SET_KEYBOARD_MAPPING) not listed' not in out
    assert 'PASS  SET_KEYBOARD_MAPPING count 0: status 2 at [6]' in out


def test_conformance_fails_a_board_that_ignores_the_profile(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test a board with range checks that are right, which writes the active profile whatever [3] names."""
    checked = hlp_model_board._set_keyboard_mapping
    hlp_model_board._set_keyboard_mapping = lambda request: checked(
        request[:3] + bytes([hlp_model_board.applied_profile()]) + request[4:])
    with pytest.raises(SystemExit):
        hlp_run_on_model_board(conformance.conformance, '--quick')
    out = capsys.readouterr().out
    assert 'FAIL  SET_KEYBOARD_MAPPING another profile: status 3' in out
    assert 'RESULT: FAIL (188 passed, 1 failed)' in out


def test_conformance_fails_a_board_checking_the_profile_first(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test a board that refuses another profile with status 3 before it checks the first record and count."""
    checked = hlp_model_board._set_keyboard_mapping
    hlp_model_board._set_keyboard_mapping = lambda request: (
        testing._STALE if request[3] != hlp_model_board.applied_profile() else checked(request))
    with pytest.raises(SystemExit):
        hlp_run_on_model_board(conformance.conformance, '--quick')
    out = capsys.readouterr().out
    assert 'FAIL  SET_KEYBOARD_MAPPING first record at the total under another profile' in out
    assert 'RESULT: FAIL (188 passed, 1 failed)' in out


def test_conformance_reports_a_refused_keyboard_put_back_and_runs_on(hlp_run_on_model_board, hlp_model_board,
                                                                     capsys, caplog):
    """Test a profile hotkey just after the keyboard code change: the put-back is refused, warned and failed."""
    hlp_model_board.profiles.append([bytes([2, 1]) + b'Second'.ljust(16, b'\x00')])
    hlp_model_board.summary = summary_record(led_extent=4, grid_width=2, grid_height=2, profiles=2, controls=3,
                                             lit_controls=1, unlit_controls=2, lights=2, animations=1)
    changed = (hlp_model_board.keyboard_codes[1][0] + 1) & 0xFF

    def hotkey(board, request):
        last = board.requests[-1] if board.requests else b''
        if last[:1] == bytes([hl.CMD_SET_KEYBOARD_MAPPING]) and last[7] == changed and board.stored_profile == 1:
            board.store_profile(2)
    hlp_model_board.hooks.append(hotkey)
    with pytest.raises(SystemExit):
        hlp_run_on_model_board(conformance.conformance, '--quick', '--settings')
    out = capsys.readouterr().out
    assert 'FAIL  keyboard code restored: the token returns  (command 0x34 (SET_KEYBOARD_MAPPING) rejected: STALE)' \
        in out
    assert 'could not put back the keyboard code changed' in caplog.text
    assert 'Timing:' in out and 'FAIL  the checks ran to the end' not in out


def test_conformance_passes_a_keyboard_mapping_of_one_record_or_none(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test the SET_KEYBOARD_MAPPING range checks on a page 11 of one record, and of none."""
    for actions in ([1], []):
        hlp_model_board.keyboard_actions = actions
        hlp_model_board.keyboard_codes = {1: [0x52] * len(actions)}
        hlp_run_on_model_board(conformance.conformance, '--quick', '--settings')
        out = capsys.readouterr().out
        assert 'FAIL' not in out and 'RESULT: ALL PASS' in out


def test_conformance_fails_a_write_of_the_codes_shown(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test a board that writes its flash for SET_KEYBOARD_MAPPING of the codes it has. That reply or the next waits."""
    every = struct.pack('<BHB', 1, 0, len(hlp_model_board.keyboard_actions))
    for at_the_next_pass in (False, True):
        written = []

        def flash_write(board, request, at_the_next_pass=at_the_next_pass, written=written):
            if written:
                written.clear()
                board.now += 0.2            # the reply pause of a settings flash write
            if request[0] == hl.CMD_SET_KEYBOARD_MAPPING and request[3:7] == every:
                if at_the_next_pass:
                    written.append(True)
                else:
                    board.now += 0.2
        hlp_model_board.hooks[:] = [flash_write]
        with pytest.raises(SystemExit):
            hlp_run_on_model_board(conformance.conformance, '--quick')
        out = capsys.readouterr().out
        assert 'FAIL  SET_KEYBOARD_MAPPING of the codes shown writes nothing to flash: no reply pause' in out
        assert 'RESULT: FAIL (188 passed, 1 failed)' in out


def test_conformance_fails_an_action_page_3_does_not_name(hlp_run_on_model_board, hlp_model_board, capsys):
    """Test the page 3 checks: an action on page 10 without a name, and a value listed twice."""
    named = [record for record in hlp_model_board.name_records() if record[0] != name_record(0, 13, '')[0]]
    hlp_model_board.names = named + [named[0]]
    with pytest.raises(SystemExit):
        hlp_run_on_model_board(conformance.conformance, '--quick')
    out = capsys.readouterr().out
    assert 'FAIL  every action on pages 10 to 12 has a name on page 3  ([13])' in out
    assert 'FAIL  page 3 lists each value once' in out
