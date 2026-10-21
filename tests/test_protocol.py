"""Test the Host Lighting protocol library.

SPDX-FileCopyrightText: © 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import logging
import struct
import sys
from dataclasses import fields
from types import SimpleNamespace

import pytest

from gp2040ce_hostlighting import protocol as hl
from gp2040ce_hostlighting import records
from gp2040ce_hostlighting.testing import (FakeBoard, control_record, fake_device, hello_reply, identity_reply,
                                           light_record, name_record, other_event, page_reply, state_event,
                                           state_record, summary_record)


def test_build_request_frames_flags_and_payload():
    """Test that a request is one 64-byte report: command, sequence, flags, payload, zero padding."""
    report = hl.build_request(hl.CMD_STAGE, 0x42, b'\x04\x01', hl.FLAG_COMMIT_AFTER)
    assert len(report) == hl.REPORT_SIZE
    assert report[:5] == bytes([hl.CMD_STAGE, 0x42, hl.FLAG_COMMIT_AFTER, 0x04, 0x01])
    assert not any(report[5:])


def test_build_request_rejects_oversized_payload():
    """Test that a payload past [63] is refused."""
    hl.build_request(hl.CMD_STAGE, 1, bytes(61))
    with pytest.raises(ValueError):
        hl.build_request(hl.CMD_STAGE, 1, bytes(62))


def test_match_reply_and_events():
    """Test reply matching by command echo and sequence, and event recognition."""
    reply = bytes([hl.CMD_COMMIT | hl.REPLY_FLAG, 0x17, 0]) + bytes(61)
    assert hl.match_reply(reply, hl.CMD_COMMIT, 0x17)
    assert not hl.match_reply(reply, hl.CMD_COMMIT, 0x18)
    assert not hl.match_reply(reply, hl.CMD_RELEASE, 0x17)
    assert not hl.match_reply(b'', hl.CMD_COMMIT, 0x17)
    event = bytes([hl.EVENT_MARKER, hl.EVENT_STATE_CHANGED, 0]) + bytes(61)
    assert hl.is_event(event) and not hl.is_event(reply)


def test_stage_capacity_matches_the_spec_table():
    """Test the per-report capacities against docs/host-lighting.md, Staging."""
    table = {  # addressing: (RGB, RGBW, RGB565, UNSTAGE)
        hl.ADDRESSING_LED_RUN: (19, 14, 28, 32), hl.ADDRESSING_LIGHT_RUN: (19, 14, 28, 32),
        hl.ADDRESSING_LIGHT: (11, 9, 14, 29), hl.ADDRESSING_ACTION: (11, 9, 14, 29),
        hl.ADDRESSING_PIN: (14, 11, 19, 32), hl.ADDRESSING_KIND: (14, 11, 19, 32),
    }
    formats = (hl.PIXEL_RGB, hl.PIXEL_RGBW, hl.PIXEL_RGB565, None)
    for addressing, expected in table.items():
        assert tuple(hl.stage_capacity(addressing, fmt) for fmt in formats) == expected, addressing


def test_encode_pixel_formats():
    """Test RGB, RGBW and RGB565 pixels as the spec lays them out."""
    assert hl.encode_pixel((1, 2, 3), hl.PIXEL_RGB) == b'\x01\x02\x03'
    assert hl.encode_pixel((1, 2, 3, 4), hl.PIXEL_RGBW) == b'\x01\x02\x03\x04'
    assert hl.encode_pixel((1, 2, 3), hl.PIXEL_RGBW) == b'\x01\x02\x03\x00'
    assert hl.encode_pixel((0xFF, 0x00, 0x00), hl.PIXEL_RGB565) == struct.pack('<H', 0xF800)
    assert hl.encode_pixel((0x00, 0xFF, 0x00), hl.PIXEL_RGB565) == struct.pack('<H', 0x07E0)
    assert hl.encode_pixel((0x00, 0x00, 0xFF), hl.PIXEL_RGB565) == struct.pack('<H', 0x001F)
    with pytest.raises(ValueError):
        hl.encode_pixel((1, 2, 3), 7)


def test_encode_address_widths():
    """Test entry addresses: 16-bit ordinal, 8-bit pin and kind, signed 16-bit action."""
    assert hl.encode_address(hl.ADDRESSING_LIGHT, 0x0102) == b'\x02\x01'
    assert hl.encode_address(hl.ADDRESSING_PIN, 7) == b'\x07'
    assert hl.encode_address(hl.ADDRESSING_KIND, 3) == b'\x03'
    assert hl.encode_address(hl.ADDRESSING_ACTION, -10) == struct.pack('<h', -10)


def test_decode_hello():
    """Test the HELLO reply: magic, version, board ID, queue limit and capability sets."""
    hello = hl.decode_hello(hello_reply())
    assert hello.magic == b'GPHL' and (hello.major, hello.minor) == (2, 0)
    assert hello.board_id == '433030343237362E' and hello.reply_queue_limit == 16
    assert hello.request_flags == {0, 1, 2, 3, 4, 5, 6}     # four flags and the claim generation's three bits
    assert 0x33 in hello.commands and 0x71 in hello.commands and 0x06 not in hello.commands
    assert hello.pages == {0, 1, 2, 3, 8, 9, 10, 11, 12} and hello.addressing_modes == {0, 1, 4, 5, 6, 7}
    assert hello.pixel_formats == {0, 1, 2} and hello.events == {0} and hello.events_enabled == frozenset()


def test_decode_state_all_groups():
    """Test page 1's three groups."""
    reply = page_reply(hl.PAGE_STATE, [state_record(takeover_live=1, input_mode=14, profile=2, player=0,
                                                    animation=0xFFFF, brightness_step_shown=7,
                                                    takeover_number=9, holder_token=0xA1, application_name=b'MESH',
                                                    takeover=1, keepalive_ms=300,
                                                    brightness_applied=0, brightness_unsaved=1, exclusive=1,
                                                    saved_brightness_step=5, claim_generation=3)])
    header = hl.decode_page_header(reply)
    assert (header.page, header.total, header.count, header.stride, header.groups) == (1, 1, 1, 35, 7)
    (record,) = hl.decode_records(reply)
    assert record == records.StateRecord(
        ordinal=0,
        state=records.State(takeover_live=True, brightness_unsaved=True, animations_idle=False, input_mode=14,
                            profile=2, player=0, animation=None, brightness_step_shown=7, takeover_number=9,
                            saved_brightness_step=5),
        controller=hl.Controller(holder_token=0xA1, application_name='MESH', exclusive=True, claim_generation=3),
        settings=hl.Settings(takeover=hl.TAKEOVER_OVERLAY, keepalive_ms=300, brightness_applied=False))
    idle = hl.decode_records(page_reply(hl.PAGE_STATE, [state_record(animations_idle=1)]))[0].state
    assert idle.animations_idle and not idle.takeover_live and not idle.brightness_unsaved


def test_decode_records_honours_the_echoed_groups():
    """Test that only echoed groups are decoded, at the stride the reply states."""
    lights = [light_record(i, pin=2 + i, action=5, x=i, y=1, slot=0xFF) for i in range(30)]
    reply = page_reply(hl.PAGE_LIGHTS, lights, start=0, mask=0b11100)
    header = hl.decode_page_header(reply)
    assert (header.stride, header.count, header.groups) == (9, 5, 0b11100)
    decoded = hl.decode_records(reply)
    assert decoded[4] == records.LightRecord(ordinal=4, owner=records.Owner(pin=6, action=5),
                                             owner_instances=records.Instances(instance=None, instances=None),
                                             grid=records.GridCell(x=4, y=1))
    assert decoded[4].groups == {'owner', 'owner_instances', 'grid'}
    reply = page_reply(hl.PAGE_LIGHTS, lights, start=0, mask=0b10000)
    assert hl.decode_page_header(reply).count == 24
    assert hl.decode_records(reply)[23] == records.LightRecord(ordinal=23, grid=records.GridCell(x=23, y=1))
    # a later reply's records count from its start
    reply = page_reply(hl.PAGE_LIGHTS, lights, start=24, mask=0b10000)
    assert [record.ordinal for record in hl.decode_records(reply)] == list(range(24, 30))


def test_decode_controls_and_lights_none_values():
    """Test the none values: unlit controls, lights without an owner or palette slot."""
    reply = page_reply(hl.PAGE_CONTROLS,
                       [control_record(12, 13, lit=0), control_record(2, 1, duplicated=1, instances=2)])
    unlit, lit = hl.decode_records(reply)
    assert unlit.position == records.Position(first_led=None, grid_rectangle=None)
    assert unlit.lights.first_light is None and not unlit.input.lit and unlit.input.action == 13
    assert lit.input.duplicated and lit.instances.instances == 2 and lit.position.grid_rectangle == (1, 0, 1, 0)
    (case,) = hl.decode_records(page_reply(hl.PAGE_LIGHTS, [light_record(12, 3, kind=1, slot=0)]))
    assert case.owner == records.Owner(pin=None, action=None) and case.palette.slot == 0
    assert (case.leds.first_led, case.leds.led_count, case.kind.value) == (12, 3, 1)


def test_decode_keys():
    """Test page 10 keys: GPIO pins, Hall-effect triggers, expander pins and keys 2.0 does not define."""
    for key, decoded, name in ((0, ('gpio', 0), 'GP0'), (47, ('gpio', 47), 'GP47'),
                               (48, ('unknown', 48), 'key 0x30'), (0x80, ('hall_effect', 0), 'HE0'),
                               (0x9F, ('hall_effect', 31), 'HE31'), (0xA0, ('unknown', 0xA0), 'key 0xA0'),
                               (0xC0, ('expander', 0), 'PCF0'), (0xCF, ('expander', 15), 'PCF15'),
                               (0xD0, ('unknown', 0xD0), 'key 0xD0'), (0xFF, ('unknown', 0xFF), 'key 0xFF')):
        assert hl.decode_key(key) == decoded and hl.key_name(key) == name, key
    (control,) = hl.decode_records(page_reply(hl.PAGE_CONTROLS, [control_record(hl.KEY_TRIGGER + 3, 5, lit=0)]))
    assert (control.input.key, control.input.source, control.input.index, control.input.lit) == (
        0x83, 'hall_effect', 3, False)


def test_decode_other_pages():
    """Test pages 2, 8, 9 and 12."""
    modes = [[bytes([0, 0x03]), b'XINPUT'.ljust(22, b'\x00')], [bytes([14, 0x01]), b'GENERIC'.ljust(22, b'\x00')]]
    assert hl.decode_records(page_reply(hl.PAGE_INPUT_MODES, modes)) == (
        records.InputModeRecord(ordinal=0, mode=records.InputModeFlags(value=0, lighting=True, detected=True),
                                name='XINPUT'),
        records.InputModeRecord(ordinal=1, mode=records.InputModeFlags(value=14, lighting=True, detected=False),
                                name='GENERIC'))
    (summary,) = hl.decode_records(page_reply(hl.PAGE_SUMMARY, [summary_record()]))
    assert summary.summary == records.Summary(
        led_extent=19, led_limit=100, render_hz=40, colour_format=0, white_channel=False, brightness_maximum=255,
        brightness_steps=10, animation_speed_steps=10, button_layout=0, button_layout_right=0,
        button_layout_orientation=0, grid_width=9, grid_height=6, profiles=3, controls=12, lit_controls=10,
        unlit_controls=2, lights=16, animations=3)
    record = summary_record(colour_format=3, white_channel=1, button_layout=18, button_layout_right=9,
                            button_layout_orientation=1, counters=bytes([1, 0, 2, 3, 255, 0, 0, 0]))
    (summary,) = hl.decode_records(page_reply(hl.PAGE_SUMMARY, [record]))
    assert (summary.summary.colour_format, summary.summary.white_channel) == (3, True)
    assert (summary.summary.button_layout, summary.summary.button_layout_right,
            summary.summary.button_layout_orientation) == (18, 9, 1)
    assert summary.changes.counters == (1, 0, 2, 3, 255, 0, 0, 0) and summary.changes.counter(12) == 255
    with pytest.raises(ValueError, match='page 16 is not a configuration page'):
        summary.changes.counter(16)
    (changes,) = hl.decode_records(page_reply(hl.PAGE_SUMMARY, [record], mask=0b10))
    assert changes.groups == {'changes'}
    profiles = [[bytes([1, 1]) + b'Base'.ljust(16, b'\x00')], [bytes([2, 0]) + b'Two'.ljust(16, b'\x00')]]
    assert hl.decode_records(page_reply(hl.PAGE_PROFILES, profiles))[1] == records.ProfileRecord(
        ordinal=1, profile=records.Profile(number=2, enabled=False, label='Two'))
    animations = [[bytes([1, 2, 1, 2]), bytes([5, 5, 5])]]
    assert hl.decode_records(page_reply(hl.PAGE_ANIMATIONS, animations)) == (records.AnimationRecord(
        ordinal=0, effects=records.Effects(enabled=True, idle=2, pressed=1, case=2),
        speeds=records.Speeds(idle=5, pressed=5, case=5)),)


def test_a_group_not_read_raises_and_records_are_immutable():
    """Test the checked groups: a group not read raises, has() and hasattr() tell, merged() fills groups in."""
    lights = [light_record(4, 2, pin=3, action=5, x=1, y=2)]
    whole = hl.decode_records(page_reply(hl.PAGE_LIGHTS, lights))[0]
    owners = hl.decode_records(page_reply(hl.PAGE_LIGHTS, lights, mask=0b1110))[0]
    with pytest.raises(hl.HostLightingGroupNotReadError, match='page 11 record 0: the grid group was not read'):
        owners.grid
    assert isinstance(hl.HostLightingGroupNotReadError(11, 'grid', 0), AttributeError)
    assert not hasattr(owners, 'grid') and hasattr(owners, 'owner') and owners.has('owner') and not owners.has('leds')
    with pytest.raises(ValueError, match="page 11 has no field group 'colour'"):
        owners.has('colour')
    with pytest.raises(TypeError, match="page 11 has no field group 'colour'"):
        records._Record.__init__(owners, 0, {'colour': 1})
    assert whole.merged(owners) == whole and owners.merged(whole) == whole and hash(whole.merged(owners)) == hash(whole)
    moved = hl.decode_records(page_reply(hl.PAGE_LIGHTS, [light_record(4, 2, pin=3, action=6)], mask=0b100))[0]
    assert whole.merged(moved).owner.action == 6 and whole.merged(moved).grid == whole.grid
    with pytest.raises(ValueError, match='same ordinal'):
        whole.merged(hl.decode_records(page_reply(hl.PAGE_LIGHTS, lights * 2, start=1))[0])
    with pytest.raises(AttributeError, match='immutable'):
        whole.ordinal = 3
    with pytest.raises(AttributeError, match='immutable'):
        del whole.ordinal
    assert whole != owners and whole != 'record' and 'grid=GridCell(x=1, y=2)' in repr(whole)
    assert records.LightRecord.grid.name == 'grid'           # the class attribute is the group itself


def test_decode_identity_strings():
    """Test page 0: NUL-terminated strings, a 48-byte string with no NUL, and the echoed groups."""
    strings = ['Haute42 COSMOX M Ultra', 'Haute42COSMOXMUltra', 'rp2040', 'v0.7.12-88-ga83f820', '0.7.12',
               'a83f820', 'Release', 'GP2040-CE_0.7.12_Haute42COSMOXMUltra']
    assert hl.decode_identity(identity_reply(strings)) == records.Identity(board_label=strings[0],
                                                                           board_config=strings[1])
    long = 'X' * 48
    assert hl.decode_identity(identity_reply(['', '', '', long], mask=0x08)) == records.Identity(firmware_version=long)
    cut = 'X' * 47 + '\u00e9'                        # two bytes, the 48th byte cuts it
    assert hl.decode_identity(identity_reply([cut], mask=0x01)) == records.Identity(board_label='X' * 47 + '\ufffd')


def test_request_skips_other_applications_and_keeps_events(hlp_fake_device, hlp_fake_board):
    """Test that a reply to another application and an event are passed over while waiting."""
    hlp_fake_board.before[hl.CMD_HELLO] = [bytes([hl.CMD_HELLO | hl.REPLY_FLAG, 99, 0]) + bytes(61),
                                           bytes([hl.EVENT_MARKER, hl.EVENT_STATE_CHANGED, 0]) + bytes(61)]
    assert hlp_fake_device.hello().board_id == '433030343237362E'
    assert len(hlp_fake_device.events) == 1 and not hlp_fake_board.outbox


def test_request_drops_reports_received_before_it(hlp_fake_device, hlp_fake_board):
    """Test that a reply received before the request is not taken for its reply, even one that echoes it."""
    # a late reply to an earlier request: this application's action and token, another holder then
    earlier = bytes([hl.CMD_CLAIM | hl.REPLY_FLAG, 1, hl.STATUS_OK, hl.CLAIM_TAKE]) + struct.pack('<II', 0xAAAA, 0xBBBB)
    earlier += b'app-b'
    hlp_fake_board.outbox += [earlier.ljust(hl.REPORT_SIZE, b'\x00'),
                              bytes([hl.EVENT_MARKER, hl.EVENT_STATE_CHANGED, 0]) + bytes(61)]
    assert hlp_fake_device.claim(hl.CLAIM_TAKE, 0xAAAA, b'app-a').holder_token == 0xAAAA
    assert len(hlp_fake_device.events) == 1 and not hlp_fake_board.outbox


def test_drain_keeps_events_and_take_events_hands_them_over(hlp_fake_device, hlp_fake_board):
    """Test the public drain, which keeps events and awaited replies, and the event hand-over."""
    sequence = hlp_fake_device.send(hl.CMD_HELLO)
    hlp_fake_board.outbox += [state_event(profile=2), hello_reply(200)]       # the second, another application's
    hlp_fake_device.drain()
    assert not hlp_fake_board.outbox and len(hlp_fake_device.events) == 1 and len(hlp_fake_device._pending) == 1
    hlp_fake_board.outbox.append(state_event(profile=3))                     # received, not yet read
    events = hlp_fake_device.take_events()
    assert [hl.decode_state_changed(event).record.state.profile for event in events] == [2, 3]
    assert hlp_fake_device.events == [] and hlp_fake_device.take_events() == []
    assert hlp_fake_device.wait_reply(hl.CMD_HELLO, sequence)[1] == sequence


def test_replies_read_early_are_kept_for_their_requests(hlp_fake_device, hlp_fake_board):
    """Test that a pipelined request's reply read while waiting for another's, or before a request, is kept."""
    hlp_fake_device.send(hl.CMD_HELLO, flags=hl.FLAG_NO_REPLY)
    first, second = hlp_fake_device.send(hl.CMD_HELLO), hlp_fake_device.send(hl.CMD_HELLO)
    assert hlp_fake_device.wait_reply(hl.CMD_HELLO, second)[1] == second
    third = hlp_fake_device.send(hl.CMD_HELLO)
    hlp_fake_board.outbox.append(hello_reply(200))      # another application's
    assert hlp_fake_device.claim(hl.CLAIM_TAKE, 0xAAAA, b'app-a').holder_token == 0xAAAA
    assert hlp_fake_device.wait_reply(hl.CMD_HELLO, first)[1] == first
    assert hlp_fake_device.wait_reply(hl.CMD_HELLO, third)[1] == third
    assert not hlp_fake_device._pending and not hlp_fake_device._awaited


def test_claim_takes_an_invalid_argument_reply_without_an_echo(hlp_fake_device):
    """Test that CLAIM's invalid argument reply, which carries no echo, is taken as its reply."""
    with pytest.raises(hl.HostLightingRejectedError) as rejected:
        hlp_fake_device.claim(3, 0xAAAA)                  # no such action
    assert (rejected.value.status, rejected.value.offset) == (hl.STATUS_INVALID, 3)


def test_claim_refuses_a_token_it_cannot_send(hlp_fake_device, hlp_fake_board):
    """Test that CLAIM refuses token 0 and one over 32 bits before sending, keeping the claim token."""
    for token in (0, 1 << 32):
        with pytest.raises(ValueError, match='token'):
            hlp_fake_device.claim(hl.CLAIM_TAKE, token)
    assert hlp_fake_device.claim_token == 0 and not hlp_fake_board.sent(hl.CMD_CLAIM)


def test_exclusive_claim_sends_the_flag_and_holder(hlp_fake_device, hlp_fake_board):
    """Test that an exclusive claim's holder sends FLAG_HOLDER and the generation, a shared one the generation."""
    assert hlp_fake_device.claim(hl.CLAIM_TAKE, 0xAAAA, b'app-a', exclusive=True) == hl.Controller(
        holder_token=0xAAAA, application_name='app-a', exclusive=True, claim_generation=2)
    assert hlp_fake_board.sent(hl.CMD_CLAIM)[-1][24] == hl.CLAIM_EXCLUSIVE and hlp_fake_device.holding_exclusive
    assert hlp_fake_board.sent(hl.CMD_CLAIM)[-1][2] == 0 and hlp_fake_device.generation == 2
    holder = hlp_fake_device.read_record(hl.PAGE_STATE).records[0].controller
    assert holder.holder_token == 0xAAAA and holder.exclusive
    assert hlp_fake_board.sent(hl.CMD_GET_PAGE)[-1][2] == hl.FLAG_HOLDER | 2 << hl.FLAG_GENERATION_SHIFT
    assert not hlp_fake_device.claim(hl.CLAIM_TAKE, 0xAAAA, b'app-a').exclusive     # renewed shared
    assert hlp_fake_board.sent(hl.CMD_CLAIM)[-1][24] == 0 and not hlp_fake_device.holding_exclusive
    hlp_fake_device.hello()
    assert hlp_fake_board.sent(hl.CMD_HELLO)[-1][2] == 2 << hl.FLAG_GENERATION_SHIFT
    # a request's own generation is sent as given
    hlp_fake_device.request(hl.CMD_HELLO, flags=5 << hl.FLAG_GENERATION_SHIFT)
    assert hlp_fake_board.sent(hl.CMD_HELLO)[-1][2] == 5 << hl.FLAG_GENERATION_SHIFT
    holder = hlp_fake_device.claim(hl.CLAIM_GIVE_UP, 0xAAAA)
    assert (holder.holder_token, holder.application_name) == (0, '')
    hlp_fake_device.hello()
    assert hlp_fake_device.generation == 0 and hlp_fake_board.sent(hl.CMD_HELLO)[-1][2] == 0


def test_a_refused_exclusive_claim_sends_no_holder_flag(hlp_fake_device, hlp_fake_board):
    """Test that requests under another application's exclusive claim carry no FLAG_HOLDER and no generation."""
    hlp_fake_board.holder_token, hlp_fake_board.holder_name = 0xB2, b'MESH'.ljust(16, b'\x00')
    holder = hlp_fake_device.claim(hl.CLAIM_TAKE, 0xAAAA, b'app-a', exclusive=True)
    assert (holder.holder_token, holder.application_name) == (0xB2, 'MESH')
    hlp_fake_device.hello()
    assert not hlp_fake_device.holding_exclusive and hlp_fake_board.sent(hl.CMD_HELLO)[-1][2] == 0


def test_a_claimed_refusal_names_the_holder(hlp_fake_device, hlp_fake_board):
    """Test that a status 4 refusal carries the claim's holder and generation, as page 1's Controller group."""
    holder = struct.pack('<I', 0xB2) + b'MESH'.ljust(16, b'\x00') + bytes([hl.CLAIM_EXCLUSIVE, 3])
    answer = hlp_fake_board.answer

    def claimed(command, sequence, request):
        if command == hl.CMD_RELEASE:
            return bytes([command | hl.REPLY_FLAG, sequence, hl.STATUS_CLAIMED]) + holder + bytes(39)
        return answer(command, sequence, request)
    hlp_fake_board.answer = claimed
    with pytest.raises(hl.HostLightingRejectedError) as rejected:
        hlp_fake_device.request_ok(hl.CMD_RELEASE)
    assert rejected.value.status == hl.STATUS_CLAIMED and rejected.value.holder == hl.Controller(
        holder_token=0xB2, application_name='MESH', exclusive=True, claim_generation=3)
    assert str(rejected.value) == ("command 0x21 (RELEASE) rejected: CLAIMED by 'MESH' (claim 0x000000B2, "
                                   "generation 3)")
    holder = bytes(20) + bytes([0, 4])                    # the claim lapsed: no holder, the generation now
    hlp_fake_device.generation = 3                        # sent under the generation that lapsed
    with pytest.raises(hl.HostLightingRejectedError) as rejected:
        hlp_fake_device.request_ok(hl.CMD_RELEASE)
    assert rejected.value.holder == hl.Controller(holder_token=0, application_name='', exclusive=False,
                                                  claim_generation=4)
    assert str(rejected.value) == 'command 0x21 (RELEASE) rejected: CLAIMED, the claim having ended under generation 4'


def test_rejected_builds_the_error_from_a_reply():
    """Test the public refusal builder: the byte at fault for INVALID_ARG, the holder for CLAIMED."""
    invalid = bytes([hl.CMD_STAGE | hl.REPLY_FLAG, 1, hl.STATUS_INVALID, 7]) + bytes(60)
    error = hl.HostLightingDevice.rejected(hl.CMD_STAGE, invalid)
    assert (error.status, error.offset, error.holder) == (hl.STATUS_INVALID, 7, None)
    holder = struct.pack('<I', 0xB2) + b'MESH'.ljust(16, b'\x00') + bytes([hl.CLAIM_EXCLUSIVE, 3])
    claimed = bytes([hl.CMD_STAGE | hl.REPLY_FLAG, 1, hl.STATUS_CLAIMED]) + holder + bytes(39)
    assert hl.HostLightingDevice.rejected(hl.CMD_STAGE, claimed).holder == hl.Controller(
        holder_token=0xB2, application_name='MESH', exclusive=True, claim_generation=3)


def test_a_claim_name_over_16_bytes_is_refused(hlp_fake_device, hlp_fake_board):
    """Test that claim() refuses a name the board would cut short, sending nothing."""
    assert hlp_fake_device.claim(hl.CLAIM_TAKE, 0x51, b'x' * 16).application_name == 'x' * 16
    with pytest.raises(ValueError, match='17 bytes, over the 16'):
        hlp_fake_device.claim(hl.CLAIM_TAKE, 0x51, b'x' * 17)
    assert len(hlp_fake_board.sent(hl.CMD_CLAIM)) == 1


def test_opening_reads_without_blocking_from_a_random_sequence(monkeypatch):
    """Test that a device opens with non-blocking reads, for dropping earlier reports, and a random sequence."""
    class Handle(FakeBoard):
        def open_path(self, path):
            self.path = path

        def set_nonblocking(self, enabled):
            self.nonblocking = enabled
    monkeypatch.setattr(hl, '_import_hid', lambda: SimpleNamespace(device=Handle))
    monkeypatch.setattr(hl.secrets, 'randbelow', lambda limit: 41 if limit == 255 else 0)
    device = hl.HostLightingDevice(b'path')
    assert device.handle.path == b'path' and device.handle.nonblocking
    assert device.send(hl.CMD_HELLO, flags=hl.FLAG_NO_REPLY) == 42


def test_request_times_out(hlp_fake_device, hlp_fake_board):
    """Test that a missing reply raises."""
    hlp_fake_board.answer = lambda *_: None
    with pytest.raises(hl.HostLightingError):
        hlp_fake_device.request(hl.CMD_HELLO, timeout=0.01)
    assert not hlp_fake_device._awaited              # its late reply would be dropped


def test_rejection_carries_status_and_offset(hlp_fake_device):
    """Test that a status other than 0 raises with the offending byte."""
    with pytest.raises(hl.HostLightingRejectedError) as caught:
        hlp_fake_device.get_page(13)
    assert caught.value.status == hl.STATUS_INVALID and caught.value.offset == 3
    assert 'at request byte 3' in str(caught.value)


def test_get_page_ignores_another_applications_page_reply(hlp_fake_device, hlp_fake_board):
    """Test that a reply with our command and sequence but another page is not taken for ours."""
    hlp_fake_board.before[hl.CMD_GET_PAGE] = [page_reply(hl.PAGE_LIGHTS, hlp_fake_board.lights, sequence=1)]
    assert hl.decode_page_header(hlp_fake_device.get_page(hl.PAGE_SUMMARY)).page == hl.PAGE_SUMMARY


def test_read_table_walks_every_record(hlp_fake_device, hlp_fake_board):
    """Test a walk across several replies, naming the first reply's token in the later reads."""
    hlp_fake_board.lights = [light_record(i, pin=2) for i in range(7)]
    read = hlp_fake_device.read_table(hl.PAGE_LIGHTS)
    token = read.token
    assert [light.leds.first_led for light in read.records] == list(range(7)) and token == hlp_fake_board.token
    reads = [struct.unpack_from('<BHIH', read, 3) for read in hlp_fake_board.sent(hl.CMD_GET_PAGE)]
    assert [(start, expected) for _, start, expected, _ in reads] == [(0, 0), (3, token), (6, token)]


def test_read_table_restarts_when_the_state_changes(hlp_fake_device, hlp_fake_board):
    """Test that a stale token mid-walk restarts the walk, so the records never mix two states."""
    hlp_fake_board.lights = [light_record(i) for i in range(5)]
    hlp_fake_board.stale_once_at = 3
    read = hlp_fake_device.read_table(hl.PAGE_LIGHTS)
    assert len(read.records) == 5 and read.token == 0x1234ABCD + 1
    starts = [struct.unpack_from('<H', read, 4)[0] for read in hlp_fake_board.sent(hl.CMD_GET_PAGE)]
    assert starts == [0, 3, 0, 3]


def test_read_table_under_a_token_reads_under_the_callers_token(hlp_fake_device, hlp_fake_board):
    """Test that every read names the caller's token and an out-of-date token raises without a retry."""
    hlp_fake_board.lights = [light_record(i, pin=2) for i in range(7)]
    token = hlp_fake_board.token
    lights = hlp_fake_device.read_table(hl.PAGE_LIGHTS, token=token).records
    assert [light.leds.first_led for light in lights] == list(range(7))
    reads = [struct.unpack_from('<BHIH', read, 3) for read in hlp_fake_board.sent(hl.CMD_GET_PAGE)]
    assert [(start, expected) for _, start, expected, _ in reads] == [(0, token), (3, token), (6, token)]
    hlp_fake_board.stale_once_at = 3
    with pytest.raises(hl.HostLightingStaleTokenError) as raised:
        hlp_fake_device.read_table(hl.PAGE_LIGHTS, token=token)
    assert (raised.value.page, raised.value.token) == (hl.PAGE_LIGHTS, token)
    assert issubclass(hl.HostLightingStaleTokenError, hl.HostLightingError)
    assert len(hlp_fake_board.sent(hl.CMD_GET_PAGE)) == 3 + 2


def test_read_identity_asks_again_for_groups_that_did_not_fit(hlp_fake_device, hlp_fake_board):
    """Test that page 0 is read until every string has come back."""
    names = [item.name for item in fields(records.Identity)]
    assert hlp_fake_device.read_identity() == records.Identity(**dict(zip(names, hlp_fake_board.identity)))
    masks = [struct.unpack_from('<H', read, 10)[0] for read in hlp_fake_board.sent(hl.CMD_GET_PAGE)]
    assert masks[0] == 0xFF and len(masks) > 1


def test_read_identity_reads_only_the_groups_asked_for(hlp_fake_device, hlp_fake_board):
    """Test that a mask reads only its groups."""
    assert hlp_fake_device.read_identity(0b1001) == records.Identity(board_label=hlp_fake_board.identity[0],
                                                                     firmware_version=hlp_fake_board.identity[3])
    assert [struct.unpack_from('<H', read, 10)[0] for read in hlp_fake_board.sent(hl.CMD_GET_PAGE)] == [0b1001]


def test_stage_entries_chunks_and_commits_once(hlp_fake_device, hlp_fake_board):
    """Test chunking to the capacity, COMMIT_AFTER on the last report only, and the outcome masks."""
    entries = [(ordinal, (ordinal, 0, 0)) for ordinal in range(13)]
    outcomes = hlp_fake_device.stage_entries(hl.ADDRESSING_LIGHT, entries, hl.PIXEL_RGB, commit=True)
    stages = hlp_fake_board.sent(hl.CMD_STAGE)
    assert [stage[4] for stage in stages] == [11, 2]
    assert [stage[2] for stage in stages] == [0, hl.FLAG_COMMIT_AFTER]
    assert stages[1][5:10] == struct.pack('<H', 11) + b'\x0b\x00\x00'
    assert outcomes == [hl.StageOutcome(applied=2, skipped=9, outcome=0b11),
                        hl.StageOutcome(applied=0, skipped=2, outcome=0)]


def test_stage_payloads_are_built_without_sending(hlp_fake_device, hlp_fake_board):
    """Test that the payload builders send nothing and give the bytes the staging methods send."""
    entries = [(ordinal, (ordinal, 0, 0)) for ordinal in range(13)]
    payloads = hl.stage_entry_payloads(hl.ADDRESSING_LIGHT, entries, hl.PIXEL_RGB)
    colours = [(index, 1, 2) for index in range(40)]
    runs = hl.stage_run_payloads(hl.ADDRESSING_LED_RUN, 5, colours, hl.PIXEL_RGB)
    assert not hlp_fake_board.requests and hl.stage_entry_payloads(hl.ADDRESSING_LIGHT, []) == []
    assert [payload[1] for payload in payloads] == [11, 2]
    assert [struct.unpack_from('<HB', run, 1) for run in runs] == [(5, 19), (24, 19), (43, 2)]
    hlp_fake_device.stage_entries(hl.ADDRESSING_LIGHT, entries, hl.PIXEL_RGB)
    assert [bytes(stage[3:3 + len(payload)]) for stage, payload in zip(hlp_fake_board.sent(hl.CMD_STAGE), payloads)] \
        == payloads
    with pytest.raises(ValueError):
        hl.stage_run_payloads(hl.ADDRESSING_LIGHT, 0, colours)


def test_fill_and_configure(hlp_fake_device, hlp_fake_board):
    """Test FILL's format nibble and pixel, and CONFIGURE's reply."""
    hlp_fake_device.fill((1, 2, 3, 4), hl.PIXEL_RGBW, commit=True)
    (fill,) = hlp_fake_board.sent(hl.CMD_FILL)
    assert fill[2] == hl.FLAG_COMMIT_AFTER and fill[3:8] == bytes([0x10, 1, 2, 3, 4])
    assert hlp_fake_device.configure(hl.TAKEOVER_OVERLAY, keepalive_ms=300, brightness_applied=False) == hl.Settings(
        takeover=hl.TAKEOVER_OVERLAY, keepalive_ms=300, brightness_applied=False)


def test_a_late_reply_is_a_timeout(hlp_fake_device, hlp_fake_board):
    """Test that no reply in time raises HostLightingTimeoutError, which is a HostLightingError."""
    sequence = hlp_fake_device.send(hl.CMD_HELLO)
    hlp_fake_board.outbox.clear()
    with pytest.raises(hl.HostLightingTimeoutError, match='no reply to command 0x01'):
        hlp_fake_device.wait_reply(hl.CMD_HELLO, sequence, timeout=0.01)
    assert issubclass(hl.HostLightingTimeoutError, hl.HostLightingError)
    assert not issubclass(hl.HostLightingTimeoutError, hl.HostLightingDisconnectedError)


def test_a_failed_write_is_a_departed_board(hlp_fake_device, hlp_fake_board):
    """Test that a write hidapi reports as failed raises at once and leaves no request awaited."""
    hlp_fake_board.departed = True
    with pytest.raises(hl.HostLightingDisconnectedError):
        hlp_fake_device.send(hl.CMD_HELLO)
    assert not hlp_fake_device._awaited and not hlp_fake_board.requests
    assert issubclass(hl.HostLightingDisconnectedError, hl.HostLightingError)


def test_a_failed_read_is_a_departed_board(hlp_fake_device, hlp_fake_board):
    """Test that a read error raises HostLightingDisconnectedError from every call that reads."""
    sequence = hlp_fake_device.send(hl.CMD_HELLO)
    hlp_fake_board.departed = True
    with pytest.raises(hl.HostLightingDisconnectedError) as raised:
        hlp_fake_device.wait_reply(hl.CMD_HELLO, sequence)
    assert isinstance(raised.value.__cause__, OSError)
    with pytest.raises(hl.HostLightingDisconnectedError):
        hlp_fake_device.collect(0.01)
    with pytest.raises(hl.HostLightingDisconnectedError):
        hlp_fake_device.next_event(0.01)


def test_reboot_tolerates_a_departed_board(hlp_fake_device, hlp_fake_board):
    """Test that a board already gone still means the reboot is under way."""
    hlp_fake_board.departed = True
    assert not hlp_fake_device.reboot(hl.REBOOT_BOOTSEL)


def test_reboot_tolerates_the_board_leaving(hlp_fake_device, hlp_fake_board, hlp_model_board):
    """Test that a vanished board means the reboot is under way, and a refusal still raises."""
    hlp_fake_board.disconnect_on.add(hl.CMD_REBOOT)
    assert not hlp_fake_device.reboot(hl.REBOOT_BOOTSEL)
    hlp_fake_board.disconnect_on.clear()
    assert hlp_fake_device.reboot(hl.REBOOT_BOOTSEL)
    assert hlp_fake_board.sent(hl.CMD_REBOOT)[-1][3:8] == bytes([hl.REBOOT_BOOTSEL]) + hl.MAGIC_REBOOT
    with pytest.raises(hl.HostLightingRejectedError):
        fake_device(hlp_model_board).reboot(0xFF)


def test_open_device_selects_by_board_id_and_major(monkeypatch):
    """Test that boards are chosen by board ID prefix and that other majors are skipped."""
    boards = {b'a': FakeBoard(), b'b': FakeBoard(), b'c': FakeBoard()}
    boards[b'b'].answer = lambda command, sequence, request: hello_reply(sequence, board_id=bytes(8))
    boards[b'c'].answer = lambda command, sequence, request: hello_reply(sequence, major=1, board_id=bytes(8))
    monkeypatch.setattr(hl, 'find_devices', lambda: [{'path': path} for path in boards])
    monkeypatch.setattr(hl, 'HostLightingDevice', lambda path: fake_device(boards[path]))
    assert hl.open_device('4330').handle is boards[b'a']
    with pytest.raises(hl.HostLightingBoardSelectionError, match='multiple boards') as several:
        hl.open_device('')
    assert [board.board_id for board in several.value.matched] == ['433030343237362E', '0000000000000000']
    with pytest.raises(hl.HostLightingBoardSelectionError, match='no board') as none:
        hl.open_device('FF')
    assert none.value.board_id_prefix == 'FF' and none.value.matched == () and len(none.value.boards) == 2
    assert none.value.interfaces == 3
    monkeypatch.setattr(hl, 'find_devices', lambda: [])
    monkeypatch.setattr(hl, 'find_other_majors', lambda: [hl.USAGE_PROTOCOL_1])
    with pytest.raises(hl.HostLightingBoardSelectionError, match='pre-release protocol 1.x') as absent:
        hl.open_device()
    assert absent.value.boards == () and absent.value.other_majors == (hl.USAGE_PROTOCOL_1,)
    assert absent.value.interfaces == 0


def test_find_board_opens_only_the_whole_id(monkeypatch):
    """Test that find_board returns the board with that ID, or None, and keeps no other board open."""
    boards = {b'a': FakeBoard(), b'b': FakeBoard()}
    boards[b'a'].answer = lambda command, sequence, request: hello_reply(sequence, board_id=bytes(8))
    monkeypatch.setattr(hl, 'find_devices', lambda: [{'path': path} for path in boards])
    monkeypatch.setattr(hl, 'HostLightingDevice', lambda path: fake_device(boards[path]))
    assert hl.find_board('433030343237362e').handle is boards[b'b']
    assert not boards[b'a'].opened and boards[b'b'].opened
    assert hl.find_board('FFFFFFFFFFFFFFFF') is None
    with pytest.raises(ValueError):
        hl.find_board('4330')


def test_request_refuses_no_reply(hlp_fake_device):
    """Test that request() will not wait for a reply that cannot come."""
    with pytest.raises(ValueError):
        hlp_fake_device.request(hl.CMD_HELLO, flags=hl.FLAG_NO_REPLY)


def test_hello_rejects_other_magic(hlp_fake_device, hlp_fake_board):
    """Test that a reply without the GPHL magic is not taken for a board."""
    hlp_fake_board.answer = lambda command, sequence, request: (bytes([command | hl.REPLY_FLAG, sequence, 0])
                                                                + b'XXXX' + bytes(57))
    with pytest.raises(hl.HostLightingMagicError) as raised:
        hlp_fake_device.hello()
    assert raised.value.magic == b'XXXX' and isinstance(raised.value, hl.HostLightingIncompatibleError)
    assert str(raised.value) == ('This interface is not a Host Lighting one. Its HELLO reply carries the magic '
                                 '58 58 58 58 (XXXX), not GPHL.')


def test_open_device_skips_interfaces_that_fail(monkeypatch):
    """Test that an interface that will not open or answer does not stop the search."""
    good = FakeBoard()
    silent = FakeBoard()
    silent.answer = lambda *_: None

    def open_path(path):
        if path == b'locked':
            raise hl.HostLightingError('the interface will not open (open failed)')
        return fake_device(good if path == b'good' else silent)
    monkeypatch.setattr(hl, 'find_devices', lambda: [{'path': path} for path in (b'locked', b'silent', b'good')])
    monkeypatch.setattr(hl, 'HostLightingDevice', open_path)
    assert hl.open_device().handle is good


def test_an_interface_that_will_not_open_is_a_host_lighting_error(monkeypatch):
    """Test that hidapi's OSError when the device does not open becomes a HostLightingError naming the udev rule."""
    def refuse(path):
        raise OSError('open failed')
    monkeypatch.setattr(hl, '_import_hid', lambda: SimpleNamespace(device=lambda: SimpleNamespace(open_path=refuse)))
    with pytest.raises(hl.HostLightingError, match=r'will not open \(open failed\)\. On Linux, check its udev rule'):
        hl.HostLightingDevice(b'locked')


def test_open_device_names_interfaces_that_did_not_answer(monkeypatch):
    """Test that interfaces that will not open or answer are told apart from a prefix no board matches."""
    silent = FakeBoard()
    silent.answer = lambda *_: None

    def open_path(path):
        if path == b'locked':
            raise hl.HostLightingError('the interface will not open (open failed)')
        return fake_device(silent)
    monkeypatch.setattr(hl, 'find_devices', lambda: [{'path': b'locked'}, {'path': b'silent'}])
    monkeypatch.setattr(hl, 'HostLightingDevice', open_path)
    with pytest.raises(hl.HostLightingBoardSelectionError,
                       match=r"^2 Host Lighting interface\(s\) found, but none opened and answered as a protocol 2 "
                             r"board\. On Linux, check the udev rule$") as error:
        hl.open_device('43')
    assert error.value.boards == () and error.value.board_id_prefix == '43' and error.value.interfaces == 2


def test_reboot_treats_a_missing_reply_as_rebooting(hlp_fake_device, hlp_fake_board):
    """Test that a reply that never comes means the reboot is under way."""
    hlp_fake_board.answer = lambda *_: None
    assert not hlp_fake_device.reboot(hl.REBOOT_BOOTSEL)


def test_read_table_edge_cases(hlp_fake_device, hlp_fake_board):
    """Test an empty table, a board that stops making progress, and a state that keeps changing."""
    hlp_fake_board.profiles = []
    assert hlp_fake_device.read_table(hl.PAGE_PROFILES) == hl.PageRead(records=(), token=hlp_fake_board.token)
    hlp_fake_board.profiles = [[bytes([1, 1]) + bytes(16)]] * 5
    real_page = hlp_fake_board._page
    hlp_fake_board._page = lambda sequence, request: (
        page_reply(hl.PAGE_PROFILES, [], 0, 0, hlp_fake_board.token, sequence)[:6] + struct.pack('<H', 5) + bytes([0])
        + real_page(sequence, request)[9:])
    with pytest.raises(hl.HostLightingError, match='stalled at 0 of 5'):
        hlp_fake_device.read_table(hl.PAGE_PROFILES)
    hlp_fake_board._page = real_page

    def always_stale(sequence, request):
        hlp_fake_board.token += 1
        return page_reply(hl.PAGE_PROFILES, [], struct.unpack_from('<H', request, 4)[0], 0, hlp_fake_board.token,
                          sequence, hl.STATUS_STALE_TOKEN)
    hlp_fake_board._page = lambda sequence, request: (always_stale(sequence, request)
                                                      if struct.unpack_from('<I', request, 6)[0]
                                                      else real_page(sequence, request))
    hlp_fake_board.profiles = [[bytes([n, 1]) + bytes(16)] for n in range(1, 5)]
    with pytest.raises(hl.HostLightingError, match='kept changing'):
        hlp_fake_device.read_table(hl.PAGE_PROFILES)


def test_get_page_takes_only_the_groups_it_asked_for(hlp_fake_device, hlp_fake_board):
    """Test that a reply with our page and start but fewer groups (another application's mask) is skipped."""
    hlp_fake_board.before[hl.CMD_GET_PAGE] = [page_reply(hl.PAGE_LIGHTS, hlp_fake_board.lights, mask=0b1,
                                                         sequence=1)]
    decoded = hl.decode_records(hlp_fake_device.get_page(hl.PAGE_LIGHTS))
    assert decoded[0].has('grid')
    assert hl.decode_page_header(hlp_fake_device.get_page(hl.PAGE_LIGHTS, mask=0b1)).groups == 1


def test_sequence_wraps_past_zero(hlp_fake_device, hlp_fake_board):
    """Test that sequence numbers run 1-255 and never 0."""
    hlp_fake_device.sequence = 254
    sequences = [hlp_fake_device.send(hl.CMD_HELLO, flags=hl.FLAG_NO_REPLY) for _ in range(3)]
    assert sequences == [255, 1, 2]


def test_decode_partial_groups_of_controls_and_animations():
    """Test page 10 with groups 0-2 (14 bytes, three per reply) and page 12 with the speeds alone."""
    controls = [control_record(pin, 1) for pin in range(5)]
    reply = page_reply(hl.PAGE_CONTROLS, controls, mask=0b0111)
    header = hl.decode_page_header(reply)
    assert (header.stride, header.count) == (14, 3)
    assert hl.decode_records(reply)[2].groups == {'input', 'instances', 'position'}
    animations = [[bytes([1, 2, 1, 2]), bytes([n, 5, 5])] for n in range(20)]
    reply = page_reply(hl.PAGE_ANIMATIONS, animations, mask=0b10)
    assert hl.decode_page_header(reply).count == 16
    assert hl.decode_records(reply)[15] == records.AnimationRecord(
        ordinal=15, speeds=records.Speeds(idle=15, pressed=5, case=5))


def test_subscribe_and_next_event(hlp_fake_device, hlp_fake_board):
    """Test SUBSCRIBE's reply, events kept while waiting for a reply, and the event wait, which keeps replies."""
    assert hlp_fake_device.subscribe([hl.EVENT_STATE_CHANGED]) == {0} and hlp_fake_device.subscribe([]) == {0}
    assert hlp_fake_device.subscribe(range(16)) == {0}
    assert struct.unpack_from('<H', hlp_fake_board.sent(hl.CMD_SUBSCRIBE)[-1], 3)[0] == 0xFFFF
    hlp_fake_board.before[hl.CMD_HELLO] = [state_event(profile=2)]    # arrives before the HELLO reply: kept
    hlp_fake_device.hello()
    hlp_fake_board.outbox.append(state_event(profile=3))
    first = hl.decode_state_changed(hlp_fake_device.next_event(0.01))
    second = hl.decode_state_changed(hlp_fake_device.next_event(0.01))
    assert (first.record.state.profile, second.record.state.profile, second.token) == (2, 3, 0x1234ABCD)
    hlp_fake_board.outbox.append(hello_reply(9))              # another application's reply: skipped
    assert hlp_fake_device.next_event(0.01) is None
    sequence = hlp_fake_device.send(hl.CMD_HELLO)             # its reply, read while waiting for an event: kept
    assert hlp_fake_device.next_event(0.01) is None
    assert hlp_fake_device.wait_reply(hl.CMD_HELLO, sequence)[1] == sequence


def test_decode_state_changed_matches_page_1():
    """Test that a STATE_CHANGED event decodes as the page 1 record it carries."""
    changed = hl.decode_state_changed(state_event(0xCAFE, animation=0xFFFF, application_name=b'MESH', holder_token=7))
    assert changed.token == 0xCAFE and changed.record.state.animation is None
    assert changed.record.controller.holder_token == 7 and changed.record.controller.application_name == 'MESH'
    with pytest.raises(hl.HostLightingError, match='event 1 is not STATE_CHANGED'):
        hl.decode_state_changed(other_event())


def test_send_with_a_chosen_sequence(hlp_fake_device, hlp_fake_board):
    """Test that a chosen sequence number is sent as given and leaves the running sequence alone."""
    assert hlp_fake_device.send(hl.CMD_HELLO, flags=hl.FLAG_NO_REPLY, sequence=0) == 0
    assert hlp_fake_device.send(hl.CMD_HELLO) == 1
    assert [request[1] for request in hlp_fake_board.sent(hl.CMD_HELLO)] == [0, 1]


def test_collect_returns_every_report(hlp_fake_device, hlp_fake_board):
    """Test that collect returns replies, other applications' replies and events, in arrival order."""
    hlp_fake_board.outbox += [hello_reply(9), state_event(profile=2), hello_reply(10)]
    reports = hlp_fake_device.collect(0.01)
    assert [(report[0], report[1]) for report in reports] == [(0x81, 9), (hl.EVENT_MARKER, 0), (0x81, 10)]
    assert hlp_fake_device.collect(0.01) == []


def test_stage_run_answers_only_the_last_report(hlp_model_board):
    """Test that a run is chunked to the capacity, sent without replies but the last, which commits."""
    device = fake_device(hlp_model_board)
    assert device.stage_run(hl.ADDRESSING_LED_RUN, 0, [(1, 2, 3)] * 40, commit=True) == hl.StageOutcome(
        applied=2, skipped=0, outcome=0b11)
    stages = hlp_model_board.sent(hl.CMD_STAGE)
    assert [(stage[2], stage[4:7]) for stage in stages] == [
        (hl.FLAG_NO_REPLY, bytes([0, 0, 19])), (hl.FLAG_NO_REPLY, bytes([19, 0, 19])),
        (hl.FLAG_COMMIT_AFTER, bytes([38, 0, 2]))]
    assert hlp_model_board.live
    assert device.stage_run(hl.ADDRESSING_LIGHT_RUN, 1, [(9, 9, 9, 9)] * 2, hl.PIXEL_RGBW) == hl.StageOutcome(
        applied=1, skipped=1, outcome=1)
    with pytest.raises(ValueError):
        device.stage_run(hl.ADDRESSING_LIGHT, 0, [(1, 2, 3)])
    with pytest.raises(ValueError):
        device.stage_run(hl.ADDRESSING_LED_RUN, 0, [])


def test_a_claimed_page_read_names_the_holder(hlp_fake_device, hlp_fake_board):
    """Test that a page read refused with status 4 carries the holder and generation too."""
    holder = struct.pack('<I', 0xB2) + b'MESH'.ljust(16, b'\x00') + bytes([0, 5])
    answer = hlp_fake_board.answer

    def claimed(command, sequence, request):
        if command == hl.CMD_GET_PAGE:
            return bytes([command | hl.REPLY_FLAG, sequence, hl.STATUS_CLAIMED]) + holder + bytes(39)
        return answer(command, sequence, request)
    hlp_fake_board.answer = claimed
    hlp_fake_device.generation = 4                        # a read under a generation no longer current
    with pytest.raises(hl.HostLightingRejectedError) as rejected:
        hlp_fake_device.get_page(hl.PAGE_STATE)
    assert rejected.value.command == hl.CMD_GET_PAGE and rejected.value.holder == hl.Controller(
        holder_token=0xB2, application_name='MESH', exclusive=False, claim_generation=5)


def test_a_device_needs_a_path_or_a_handle():
    """Test that HostLightingDevice refuses to start with neither a path nor a handle."""
    with pytest.raises(ValueError, match="path or an open handle"):
        hl.HostLightingDevice()


def test_a_fake_board_refuses_a_report_framed_wrongly(hlp_fake_board, hlp_model_board):
    """Test that the fake boards refuse a write that is not report ID 0 then 64 bytes."""
    for board in (hlp_fake_board, hlp_model_board):
        with pytest.raises(ValueError, match="not 10 bytes starting 0x00"):
            board.write(bytes(10))
        with pytest.raises(ValueError, match="not 65 bytes starting 0x01"):
            board.write(b'\x01' + bytes(64))


def test_the_readme_example_runs(hlp_run_on_model_board, hlp_model_board, monkeypatch):
    """Test that the README's library example lights every LED on the model board, then gives the claim up."""
    import pathlib
    import re
    readme = (pathlib.Path(__file__).parent.parent / 'README.md').read_text(encoding='utf-8')
    example = re.search(r'```python\n(.*?)```', readme, re.S).group(1)
    monkeypatch.setattr(hl, 'open_device', lambda prefix='': fake_device(hlp_model_board))
    exec(compile(example, 'README.md', 'exec'), {})     # the repository's own README
    stages = [request for request in hlp_model_board.requests if request[0] == hl.CMD_STAGE]
    assert stages and stages[-1][2] & hl.FLAG_COMMIT_AFTER and hlp_model_board.holder_token == 0


def test_a_short_reply_is_an_error_naming_what_was_short():
    """Test that the decoders refuse a reply too short for its fields, rather than reading past its end."""
    with pytest.raises(hl.HostLightingError, match='the HELLO reply is 20 bytes, shorter than the 47'):
        hl.decode_hello(hello_reply()[:20])
    with pytest.raises(hl.HostLightingError, match='the GET_PAGE reply is 10 bytes'):
        hl.decode_page_header(bytes(10))
    with pytest.raises(hl.HostLightingError, match='the Controller group is 4 bytes'):
        hl.decode_controller(bytes(4))
    with pytest.raises(hl.HostLightingError, match='the page 0 reply is 8 bytes'):
        hl.decode_identity(bytes(8))


def test_the_claim_generation_is_its_bytes_low_three_bits():
    """Test that the Controller group's generation ignores the byte's other bits, which a later 2.x board may use."""
    group = (0xA1).to_bytes(4, 'little') + b'Alpha'.ljust(16, b'\x00') + bytes([hl.CLAIM_EXCLUSIVE, 0xF3])
    holder = hl.decode_controller(group)
    assert (holder.holder_token, holder.exclusive, holder.claim_generation) == (0xA1, True, 3)


def test_records_that_do_not_fit_their_reply_are_refused():
    """Test a stride narrower than the echoed groups, more records than the reply holds, and a non-table page."""
    reply = bytearray(page_reply(hl.PAGE_LIGHTS, [light_record(0), light_record(1)]))
    narrow = bytearray(reply)
    narrow[9] = 4
    with pytest.raises(hl.HostLightingError, match='page 11 records are 4 bytes apart, narrower than the 16'):
        hl.decode_records(bytes(narrow))
    crowded = bytearray(reply)
    crowded[8] = 4
    with pytest.raises(hl.HostLightingError, match='holds 4 records of 16 bytes, more than its 48-byte records'):
        hl.decode_records(bytes(crowded))
    other = bytearray(reply)
    other[3] = hl.PAGE_IDENTITY
    with pytest.raises(hl.HostLightingError, match='page 0 is not a table page'):
        hl.decode_records(bytes(other))


def test_a_disconnect_names_what_failed():
    """Test that a failed write names the command and the OS error, and a failed read the OS error."""
    class Gone:
        def write(self, data):
            raise OSError('device gone')

        def read(self, size, timeout_ms=0):
            raise OSError('read error')

    class Refusing(Gone):
        def write(self, data):
            return -1

    with pytest.raises(hl.HostLightingDisconnectedError, match=r'sending command 0x01 failed with device gone\)'):
        hl.HostLightingDevice(handle=Gone()).send(hl.CMD_HELLO)
    with pytest.raises(hl.HostLightingDisconnectedError, match=r'\(sending command 0x01 failed\)$'):
        hl.HostLightingDevice(handle=Refusing()).send(hl.CMD_HELLO)
    with pytest.raises(hl.HostLightingDisconnectedError, match=r'\(reading failed with read error\)'):
        hl.HostLightingDevice(handle=Gone()).collect(0.01)


def test_a_device_closes_once():
    """Test that a second close does not reach the HID device."""
    class Counting:
        closes = 0

        def close(self):
            self.closes += 1

    handle = Counting()
    device = hl.HostLightingDevice(handle=handle)
    device.close()
    device.close()
    assert handle.closes == 1 and device.closed


def test_the_builders_refuse_wrong_arguments_with_value_error():
    """Test ValueError for entries by a run's addressing, an unknown format, a run past index 65535 and a range."""
    with pytest.raises(ValueError, match='does not take entries'):
        hl.stage_entry_payloads(hl.ADDRESSING_LED_RUN, [(0, (1, 2, 3))])
    with pytest.raises(ValueError, match='does not take entries'):
        hl.stage_entry_payloads(hl.ADDRESSING_LED_RUN, [])
    with pytest.raises(ValueError, match='does not take entries'):
        hl.unstage_entry_payloads(hl.ADDRESSING_LIGHT_RUN, [0])
    with pytest.raises(ValueError, match='unknown pixel format 7'):
        hl.stage_run_payloads(hl.ADDRESSING_LED_RUN, 0, [(1, 2, 3)], 7)
    with pytest.raises(ValueError, match='unknown addressing mode 9'):
        hl.stage_capacity(9, None)
    with pytest.raises(ValueError, match='passes index 65535'):
        hl.stage_run_payloads(hl.ADDRESSING_LED_RUN, 0xFFF0, [(1, 2, 3)] * 40)
    with pytest.raises(ValueError, match='passes index 65535'):
        hl.unstage_run_payloads(hl.ADDRESSING_LIGHT_RUN, 0xFFFF, 2)
    with pytest.raises(ValueError, match='outside 0-255'):
        hl.encode_pixel((0, 300, 0), hl.PIXEL_RGB565)
    with pytest.raises(ValueError, match='pin address 256 is outside 0 to 255'):
        hl.encode_address(hl.ADDRESSING_PIN, 256)
    assert hl.stage_run_payloads(hl.ADDRESSING_LED_RUN, 0xFFFF, [(1, 2, 3)])[0][1:3] == bytes([0xFF, 0xFF])


def test_records_copy_and_pickle(hlp_fake_device):
    """Test that a page record survives copy, deepcopy and pickle, the groups not read included."""
    import copy
    import pickle
    record = hlp_fake_device.read_table(hl.PAGE_LIGHTS, 0b11).records[0]
    for again in (copy.copy(record), copy.deepcopy(record), pickle.loads(pickle.dumps(record))):
        assert again == record and again.groups == {'leds', 'kind'}


def test_page_10_instances_are_plain_numbers(hlp_fake_device):
    """Test that a control's instance and instances decode as numbers, which page 10 never sends as none."""
    control = hlp_fake_device.read_table(hl.PAGE_CONTROLS).records[0]
    assert control.instances == records.ControlInstances(instance=1, instances=1)


def test_a_state_changed_event_must_carry_one_page_1_record():
    """Test that an event without a record, or with another page's, raises HostLightingError and is passed over."""
    good = state_event()
    for offset, value in ((8, 0), (3, hl.PAGE_CONTROLS)):
        bad = bytearray(good)
        bad[offset] = value
        with pytest.raises(hl.HostLightingError, match='STATE_CHANGED event carries'):
            hl.decode_state_changed(bytes(bad))
        assert hl.latest_state([good, bytes(bad)]) == hl.decode_state_changed(good)


def test_a_status_4_reply_is_taken_only_when_the_holder_shown_refuses_the_request(hlp_fake_device, hlp_fake_board):
    """Test the firmware's rule: generation 0 is refused under an exclusive claim alone, and HELLO not even then."""
    answer = hlp_fake_board.answer

    def refused_first(holder):
        def respond(command, sequence, request):
            refusal = bytes([command | hl.REPLY_FLAG, sequence, hl.STATUS_CLAIMED]) + holder + bytes(39)
            hlp_fake_board.outbox.append(refusal)       # another application's, or this request's
            return answer(command, sequence, request)
        return respond
    name = struct.pack('<I', 0xA1) + b'Alpha'.ljust(16, b'\x00')
    hlp_fake_board.answer = refused_first(name + bytes([0, 2]))
    hlp_fake_device.set_brightness(3)                   # a shared claim refuses no generation 0 request
    assert hlp_fake_board.state[0][6] == 3
    hlp_fake_board.answer = refused_first(name + bytes([hl.CLAIM_EXCLUSIVE, 2]))
    with pytest.raises(hl.HostLightingRejectedError, match="CLAIMED by 'Alpha'"):
        hlp_fake_device.set_brightness(4)
    assert hlp_fake_device.hello().magic == hl.MAGIC     # HELLO is open under an exclusive claim


def test_device_calls_refuse_values_their_fields_cannot_hold(hlp_fake_device, hlp_fake_board):
    """Test ValueError, not struct.error, for an argument out of its field's range, before anything is sent."""
    device = hlp_fake_device
    calls = [lambda: device.get_page(256), lambda: device.get_page(1, start=0x10000),
             lambda: device.get_page(1, mask=-1), lambda: device.get_page(1, token=1 << 32),
             lambda: device.read_table(hl.PAGE_LIGHTS, mask=0x10000), lambda: device.configure(256),
             lambda: device.configure(keepalive_ms=70000), lambda: device.subscribe([16]),
             lambda: device.set_animation(0x10000), lambda: device.set_animation_speed(0x10000),
             lambda: device.set_animation_speed(0, idle=300), lambda: device.claim(256, 1),
             lambda: hl.encode_pixel((1, 2), hl.PIXEL_RGB)]
    for call in calls:
        with pytest.raises(ValueError, match='outside|fewer than three'):
            call()
    assert not hlp_fake_board.requests and device.claim_token == 0


def test_staging_no_entries_with_commit_sends_commit(hlp_fake_device, hlp_fake_board):
    """Test that no entries with commit publish with COMMIT, and without commit send nothing."""
    assert hlp_fake_device.stage_entries(hl.ADDRESSING_LIGHT, [], commit=True) == []
    assert hlp_fake_device.unstage_entries(hl.ADDRESSING_LIGHT, [], commit=True) == []
    hlp_fake_device.stage_entries(hl.ADDRESSING_LIGHT, [])
    assert [request[0] for request in hlp_fake_board.requests] == [hl.CMD_COMMIT, hl.CMD_COMMIT]


@pytest.mark.parametrize('platform, expected', [('linux', 'hidraw'), ('win32', 'hid'), ('darwin', 'hid')])
def test_linux_uses_hidapi_s_hidraw_backend(monkeypatch, platform, expected):
    """Test that on Linux the hidraw backend is imported, which reads usage pages and shares the interface."""
    monkeypatch.setattr(hl.sys, 'platform', platform)
    monkeypatch.setitem(sys.modules, 'hidraw', SimpleNamespace(name='hidraw'))
    monkeypatch.setitem(sys.modules, 'hid', SimpleNamespace(name='hid'))
    assert hl._import_hid().name == expected


def test_linux_without_the_hidraw_module_falls_back_to_hid(monkeypatch):
    """Test that a hidapi built without the hidraw module still gives its hid module on Linux."""
    monkeypatch.setattr(hl.sys, 'platform', 'linux')
    monkeypatch.setitem(sys.modules, 'hidraw', None)
    monkeypatch.setitem(sys.modules, 'hid', SimpleNamespace(name='hid'))
    assert hl._import_hid().name == 'hid'


def test_each_report_is_traced_at_debug(hlp_fake_device, caplog):
    """Test the trace logger: each report written and read, in hex without the report ID."""
    with caplog.at_level(logging.DEBUG, logger='gp2040ce_hostlighting.trace'):
        hlp_fake_device.hello()
    lines = [record.getMessage() for record in caplog.records if record.name == 'gp2040ce_hostlighting.trace']
    assert lines[0].startswith('> 01') and len(lines[0]) == 2 + 2 * hl.REPORT_SIZE
    assert any(line.startswith('< 81') for line in lines)


def test_jsonable_turns_records_and_groups_into_json_values(hlp_fake_device):
    """Test jsonable(): a record's ordinal and groups, sets sorted, tuples as lists, bytes as hex."""
    jsonable = records.jsonable
    hello = jsonable(hlp_fake_device.hello())
    assert hello['commands'] == sorted(hello['commands']) and hello['magic'] == '4750484c'
    record = jsonable(hlp_fake_device.read_table(hl.PAGE_LIGHTS, mask=0b1).records[0])
    assert record == {'ordinal': 0, 'leds': {'first_led': 0, 'led_count': 1}}
    assert jsonable({1: (2, bytes([10]))}) == {'1': [2, '0a']}


def test_read_names_gives_page_3_as_read_enum_names_does(hlp_fake_device, hlp_fake_board):
    """Test page 3: the records decoded, and read_names() in read_enum_names()'s shape, an unknown enum left out."""
    record = hlp_fake_device.read_table(hl.PAGE_NAMES).records[0]
    assert record.entry == records.EnumValue(enum=hl.NAMES_GPIO_ACTION, value=-10) and record.name == 'NONE'
    hlp_fake_board.names = hlp_fake_board.name_records() + [name_record(7, 1, 'LATER')]
    names = hlp_fake_device.read_names()
    assert set(names) == {'GpioAction', 'LightType'} and names['GpioAction'][1] == 'BUTTON_PRESS_UP'
    assert names['LightType'] == {0: 'ActionButton', 1: 'Case', 2: 'Turbo', 3: 'Player1Light', 4: 'Player2Light',
                                  5: 'Player3Light', 6: 'Player4Light'}


def test_read_names_reads_page_3_without_a_token(hlp_fake_device, hlp_fake_board):
    """Test that read_names() sends no token, so a change of state mid-walk does not start it again."""
    hlp_fake_board.stale_once_at = 2
    hlp_fake_device.read_names()
    reads = [request for request in hlp_fake_board.requests
             if request[0] == hl.CMD_GET_PAGE and request[3] == hl.PAGE_NAMES]
    assert len(reads) == len(hlp_fake_board.name_records())
    assert all(struct.unpack_from('<I', request, 6)[0] == 0 for request in reads)
