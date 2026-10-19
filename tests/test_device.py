"""Test the device's call for every command, its claim, discovery and the capability checks.

SPDX-FileCopyrightText: © 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import struct
from dataclasses import replace

import pytest

from gp2040ce_hostlighting import protocol as hl
from gp2040ce_hostlighting.testing import (FakeBoard, ModelBoard, attach, fake_device, hello_reply, light_record,
                                           other_event, state_event)


def test_unstage_entries_and_runs(hlp_model_board):
    """Test UNSTAGE by entries and by runs: the payloads, the replies and the LEDs left staged."""
    device = fake_device(hlp_model_board)
    assert hl.unstage_entry_payloads(hl.ADDRESSING_ACTION, [1, -2]) == [
        bytes([hl.ADDRESSING_ACTION, 2]) + struct.pack('<hh', 1, -2)]
    assert [payload[1] for payload in hl.unstage_entry_payloads(hl.ADDRESSING_KIND, list(range(40)))] == [32, 8]
    assert hl.unstage_run_payloads(hl.ADDRESSING_LED_RUN, 3, 40) == [
        bytes([0]) + struct.pack('<HB', 3, 32), bytes([0]) + struct.pack('<HB', 35, 8)]
    with pytest.raises(ValueError):
        hl.unstage_run_payloads(hl.ADDRESSING_LIGHT, 0, 1)
    with pytest.raises(ValueError):
        hl.unstage_run_payloads(hl.ADDRESSING_LED_RUN, 0, 0)
    device.stage_run(hl.ADDRESSING_LED_RUN, 0, [(9, 9, 9)] * 4)
    assert device.unstage_entries(hl.ADDRESSING_LIGHT, [1, 7]) == [
        hl.StageOutcome(applied=1, skipped=1, outcome=0b01)]
    assert device.unstage_run(hl.ADDRESSING_LED_RUN, 0, 1, commit=True) == hl.StageOutcome(
        applied=1, skipped=0, outcome=1)
    assert hlp_model_board.frames == [{}]


def test_clear_commit_and_release(hlp_model_board):
    """Test CLEAR, with and without COMMIT_AFTER, COMMIT and RELEASE."""
    device = fake_device(hlp_model_board)
    device.stage_run(hl.ADDRESSING_LED_RUN, 0, [(1, 1, 1)])
    device.commit()
    assert hlp_model_board.live and hlp_model_board.frames == [{0: (1, 1, 1)}]
    device.clear(commit=True)
    assert hlp_model_board.frames[-1] == {}
    device.clear()
    assert hlp_model_board.sent(hl.CMD_CLEAR)[-1][2] == 0
    device.release()
    assert not hlp_model_board.live


def test_board_feature_calls(hlp_fake_device, hlp_fake_board):
    """Test SET_PROFILE, SET_ANIMATION with off, SET_ANIMATION_SPEED with speeds left, and unsaved SET_BRIGHTNESS."""
    hlp_fake_board.profiles.append([bytes([2, 1]) + b'Two'.ljust(16, b'\x00')])
    hlp_fake_device.set_profile(2)
    hlp_fake_device.set_animation(None)
    hlp_fake_device.set_animation_speed(0, pressed=3)
    hlp_fake_device.set_brightness(2)
    hlp_fake_device.set_brightness(hl.BRIGHTNESS_SAVED, save=True)
    state = hlp_fake_device.read_record(hl.PAGE_STATE).records[0].state
    assert (state.profile, state.animation) == (2, None)
    assert hlp_fake_board.sent(hl.CMD_SET_ANIMATION)[-1][3:5] == struct.pack('<H', hl.ANIMATION_OFF)
    assert hlp_fake_board.sent(hl.CMD_SET_ANIMATION_SPEED)[-1][3:8] == bytes([0, 0, 0xFF, 3, 0xFF])
    brightness = [request[3:5] for request in hlp_fake_board.sent(hl.CMD_SET_BRIGHTNESS)]
    assert brightness == [bytes([2, 0]), bytes([0xFF, 1])]
    hlp_fake_device.set_animation(0)
    assert hlp_fake_device.read_record(hl.PAGE_STATE).records[0].state.animation == 0
    with pytest.raises(hl.HostLightingRejectedError, match=r'0x30 \(SET_PROFILE\)'):
        hlp_fake_device.set_profile(9)


def test_set_input_mode_refuses_a_mode_without_lighting(hlp_fake_device, hlp_fake_board):
    """Test that SET_INPUT_MODE reads page 2 first and refuses a mode without the interface unless allowed."""
    with pytest.raises(ValueError, match='input mode 4 has no lighting interface'):
        hlp_fake_device.set_input_mode(4)
    assert not hlp_fake_board.sent(hl.CMD_SET_INPUT_MODE)
    assert hlp_fake_device.set_input_mode(4, allow_no_lighting=True)
    assert hlp_fake_device.set_input_mode(3)
    assert [request[3:8] for request in hlp_fake_board.sent(hl.CMD_SET_INPUT_MODE)] == [
        bytes([4]) + hl.MAGIC_INPUT_MODE, bytes([3]) + hl.MAGIC_INPUT_MODE]
    # a mode page 2 does not list is sent, and the board refuses it
    device = fake_device(ModelBoard())
    with pytest.raises(hl.HostLightingRejectedError):
        device.set_input_mode(99)


def test_keepalive_and_the_device_in_a_with_block(hlp_fake_board):
    """Test the keepalive of a host that does not claim, and a with block that closes the device."""
    with fake_device(hlp_fake_board) as device:
        device.keepalive()
        assert hlp_fake_board.sent(hl.CMD_HELLO)[-1][2] == hl.FLAG_KEEPALIVE | hl.FLAG_NO_REPLY
        assert not hlp_fake_board.outbox
    assert device.closed and not hlp_fake_board.opened


def _claimed(board, holder_token, generation, exclusive=0):
    """Have the board refuse COMMIT with status 4, showing this holder."""
    answer = board.answer
    controller = struct.pack('<I', holder_token) + b'X'.ljust(16, b'\x00') + bytes([exclusive, generation])

    def claimed(command, sequence, request):
        if command == hl.CMD_COMMIT:
            return bytes([command | hl.REPLY_FLAG, sequence, hl.STATUS_CLAIMED]) + controller + bytes(39)
        return answer(command, sequence, request)
    board.answer = claimed


def test_a_refusal_naming_another_holder_drops_the_generation(hlp_fake_device, hlp_fake_board):
    """Test the status 4 table's first two rows on the device: another token, or token 0, means generation 0."""
    hlp_fake_device.claim(hl.CLAIM_TAKE, 0x51, b'app', exclusive=True)
    assert hlp_fake_device.generation == 2 and hlp_fake_device.holding_exclusive
    for holder in (0xB2, 0):
        hlp_fake_device.generation, hlp_fake_device.holding_exclusive = 2, True
        _claimed(hlp_fake_board, holder, 3)
        with pytest.raises(hl.HostLightingRejectedError):
            hlp_fake_device.commit()
        assert (hlp_fake_device.generation, hlp_fake_device.holding_exclusive) == (0, False)
        hlp_fake_board.answer = FakeBoard.answer.__get__(hlp_fake_board)


def test_a_refusal_showing_its_own_token_takes_the_new_generation(hlp_fake_device, hlp_fake_board):
    """Test the table's third row: its own token under another generation, the claim retaken by its renewal."""
    hlp_fake_device.claim(hl.CLAIM_TAKE, 0x51, b'app')
    _claimed(hlp_fake_board, 0x51, 4)
    with pytest.raises(hl.HostLightingRejectedError) as refused:
        hlp_fake_device.commit()
    assert refused.value.holder.held_by(0x51) and hlp_fake_device.generation == 4
    assert not hlp_fake_device.holding_exclusive


def test_another_applications_refusal_with_its_own_token_is_passed_over(hlp_fake_device, hlp_fake_board):
    """Test the table's fourth row: a status 4 with its own token and generation answers another application."""
    hlp_fake_device.claim(hl.CLAIM_TAKE, 0x51, b'app')
    generation = hlp_fake_device.generation
    controller = struct.pack('<I', 0x51) + b'app'.ljust(16, b'\x00') + bytes([0, generation])
    sequence = hlp_fake_device.sequence + 1
    hlp_fake_board.before[hl.CMD_HELLO] = [
        bytes([hl.CMD_HELLO | hl.REPLY_FLAG, sequence, hl.STATUS_CLAIMED]) + controller + bytes(39)]
    assert hlp_fake_device.hello().board_id                  # the real reply, after the other one
    assert hlp_fake_device.generation == generation
    # the same reply read early, while waiting for another request's, is passed over too
    first = hlp_fake_device.send(hl.CMD_HELLO)
    hlp_fake_board.outbox.insert(0, bytes([hl.CMD_HELLO | hl.REPLY_FLAG, first, hl.STATUS_CLAIMED]) + controller
                                 + bytes(39))
    hlp_fake_device.claim(hl.CLAIM_TAKE, 0x51, b'app')
    assert hlp_fake_device.wait_reply(hl.CMD_HELLO, first)[2] == hl.STATUS_OK


def test_an_exclusive_claim_without_holder_is_its_own_refusal(hlp_fake_device, hlp_fake_board):
    """Test the table's fifth row: its own token and generation, exclusive, refused when HOLDER was not sent."""
    hlp_fake_device.claim(hl.CLAIM_TAKE, 0x51, b'app')
    hlp_fake_device.holding_exclusive = False
    _claimed(hlp_fake_board, 0x51, hlp_fake_device.generation, exclusive=hl.CLAIM_EXCLUSIVE)
    with pytest.raises(hl.HostLightingRejectedError):
        hlp_fake_device.commit()
    assert hlp_fake_device.holding_exclusive


def test_a_stale_read_names_the_current_token(hlp_fake_device, hlp_fake_board):
    """Test that HostLightingStaleTokenError carries the board's current token."""
    token = hlp_fake_board.token
    hlp_fake_board.token += 5
    with pytest.raises(hl.HostLightingStaleTokenError) as stale:
        hlp_fake_device.read_table(hl.PAGE_LIGHTS, token=token)
    assert (stale.value.token, stale.value.current) == (token, token + 5) and 'now 0x' in str(stale.value)


def test_a_refusal_of_an_unnamed_command_has_no_name():
    """Test that a refusal of a command the protocol does not define names only its number."""
    error = hl.HostLightingRejectedError(0x06, hl.STATUS_UNSUPPORTED, 0)
    assert str(error) == 'command 0x06 rejected: UNSUPPORTED'
    assert hl.HostLightingDevice.rejected(0x06, bytes([0x86, 1, hl.STATUS_INVALID])).offset == 0


def test_capabilities_are_named_and_required():
    """Test missing_capabilities() and require_capabilities() against HELLO's bitmaps."""
    hello = hl.decode_hello(hello_reply())
    needs = hl.Requirements(commands=frozenset({hl.CMD_UNSTAGE, 0x06}), request_flags=frozenset({7}),
                            pages=frozenset({3}), addressing_modes=frozenset({hl.ADDRESSING_PIN, 2}),
                            pixel_formats=frozenset({hl.PIXEL_RGB565, 5}), events=frozenset({0, 1}))
    assert hl.missing_capabilities(hello, needs) == [
        'command 0x06', 'request flag bit 7', 'page 3', 'addressing mode 2', 'pixel format 5', 'event 1']
    assert hl.missing_capabilities(hello, hl.Requirements(commands=frozenset({hl.CMD_STAGE}))) == []
    hl.require_capabilities(hello, hl.Requirements(events=frozenset({hl.EVENT_STATE_CHANGED})))
    with pytest.raises(hl.HostLightingIncompatibleError, match=r'does not offer command 0x06, request flag bit 7'):
        hl.require_capabilities(hello, needs)
    with pytest.raises(hl.HostLightingIncompatibleError, match='speaks protocol 3.0'):
        hl.require_capabilities(hl.decode_hello(hello_reply(major=3)), hl.Requirements())
    both = hl.Requirements(pages=frozenset({1})).union(hl.Requirements(pages=frozenset({8}), events=frozenset({0})))
    assert (both.pages, both.events) == ({1, 8}, {0})
    bare = replace(hello, commands=frozenset(), addressing_modes=frozenset(), pixel_formats=frozenset(),
                   events=frozenset())
    named = hl.Requirements(commands=frozenset({0x11}), addressing_modes=frozenset({hl.ADDRESSING_PIN}),
                            pixel_formats=frozenset({hl.PIXEL_RGB565}), events=frozenset({hl.EVENT_STATE_CHANGED}))
    assert hl.missing_capabilities(bare, named) == ['command 0x11 (UNSTAGE)', 'addressing mode 5 (pin)',
                                                    'pixel format 2 (RGB565)', 'event 0 (STATE_CHANGED)']


def test_other_majors_and_the_no_interface_message(monkeypatch):
    """Test find_other_majors() and the error open_device() gives when only other majors are connected."""
    attach(monkeypatch, other_majors=(hl.USAGE_PROTOCOL_1, 3))
    assert hl.find_other_majors() == [hl.USAGE_PROTOCOL_1, 3] and hl.find_devices() == []
    with pytest.raises(hl.HostLightingError, match='2 interface.s. speak another protocol major, among them '
                                                   'pre-release protocol 1.x firmware'):
        hl.open_device()
    attach(monkeypatch)
    with pytest.raises(hl.HostLightingError, match=r'of protocol 2 found\. Connect'):
        hl.open_device()


def test_several_boards_are_listed_by_label(monkeypatch):
    """Test list_boards() and the several-boards error, which name each board's label."""
    first, second, older = FakeBoard(), FakeBoard(), FakeBoard()
    second.board_id = bytes.fromhex('4330313435393032')
    second.identity[0] = 'Haute42 COSMOX'
    older.board_id = bytes(8)
    older.answer = lambda command, sequence, request: hello_reply(sequence, major=1, board_id=bytes(8))
    attach(monkeypatch, first, second, older)
    assert hl.list_boards() == [
        hl.BoardListing(board_id='433030343237362E', board_label='Haute42 COSMOX M Ultra',
                        firmware_version='v0.7.12-88-ga83f820', major=2, minor=0),
        hl.BoardListing(board_id='4330313435393032', board_label='Haute42 COSMOX',
                        firmware_version='v0.7.12-88-ga83f820', major=2, minor=0)]
    with pytest.raises(hl.HostLightingError, match=r'433030343237362E \(Haute42 COSMOX M Ultra\), 4330313435393032 '
                                                   r'\(Haute42 COSMOX\)\. Choose one by a board ID prefix'):
        hl.open_device('43')
    assert not first.opened and not second.opened


def test_a_board_with_a_failing_page_0_is_listed_by_its_id(monkeypatch):
    """Test that list_boards() lists a board without page 0, or with a page 0 read that fails, by its ID alone."""
    board, broken = FakeBoard(), FakeBoard()
    broken.board_id = bytes.fromhex('4330313435393032')
    real = broken._page
    broken._page = lambda sequence, request: (
        bytes([hl.CMD_GET_PAGE | hl.REPLY_FLAG, sequence, hl.STATUS_INVALID, 3]) + bytes(60)
        if request[3] == hl.PAGE_IDENTITY else real(sequence, request))

    def without_page_0(command, sequence, request):
        if command != hl.CMD_HELLO:
            return FakeBoard.answer(board, command, sequence, request)
        reply = bytearray(hello_reply(sequence))
        struct.pack_into('<I', reply, 35, struct.unpack_from('<I', reply, 35)[0] & ~(1 << hl.PAGE_IDENTITY))
        return bytes(reply)
    board.answer = without_page_0
    attach(monkeypatch, board, broken)
    assert [(listing.board_label, listing.firmware_version) for listing in hl.list_boards()] == [(None, None)] * 2


def test_latest_state_and_subtractive_white():
    """Test that latest_state() picks the newest STATE_CHANGED, and the subtractive white conversion."""
    assert hl.latest_state([]) is None and hl.latest_state([other_event()]) is None
    newest = hl.latest_state([state_event(profile=2), state_event(profile=3), other_event()])
    assert newest is not None and newest.record.state.profile == 3
    assert hl.subtractive_white((200, 120, 40)) == (160, 80, 0, 40)
    assert hl.subtractive_white((255, 255, 255, 100)) == (0, 0, 0, 255)


def test_light_records_reach_the_fakes_through_attach(monkeypatch):
    """Test that a fake board on the fake bus answers the whole library, as an opened device."""
    board = ModelBoard()
    board.lights = [light_record(0, pin=2, action=1), light_record(1, 2, kind=1)]
    attach(monkeypatch, board)
    with hl.open_device() as device:
        assert [light.leds.led_count for light in device.read_table(hl.PAGE_LIGHTS).records] == [1, 2]


def test_a_closed_device_and_a_closed_hidapi_handle_are_disconnected(hlp_fake_board):
    """Test that a closed device raises HostLightingDisconnectedError, as does hidapi's ValueError when not open."""
    from types import SimpleNamespace
    device = fake_device(hlp_fake_board)
    device.close()
    with pytest.raises(hl.HostLightingDisconnectedError, match='the device is closed'):
        device.request(hl.CMD_HELLO)
    with pytest.raises(hl.HostLightingDisconnectedError, match='the device is closed'):
        device.send(hl.CMD_HELLO)

    def not_open(*arguments):
        raise ValueError('not open')
    device = hl.HostLightingDevice(handle=SimpleNamespace(write=not_open, read=not_open, close=lambda: None))
    with pytest.raises(hl.HostLightingDisconnectedError, match='sending command 0x01 failed with not open'):
        device.send(hl.CMD_HELLO)
    with pytest.raises(hl.HostLightingDisconnectedError, match='reading failed with not open'):
        device.drain()


def test_a_device_keeps_the_newest_events_up_to_its_limit(hlp_fake_board):
    """Test that a host that never takes events keeps the newest EVENTS_KEPT of them, the oldest dropped."""
    device = fake_device(hlp_fake_board)
    hlp_fake_board.outbox.extend(state_event(token) for token in range(1, hl.EVENTS_KEPT + 6))
    device.drain()
    assert len(device.events) == hl.EVENTS_KEPT
    assert [hl.decode_state_changed(event).token for event in device.events[::hl.EVENTS_KEPT - 1]] == [
        6, hl.EVENTS_KEPT + 5]


def test_get_page_takes_only_its_own_reply(hlp_fake_board):
    """Test that a reply echoing fewer groups, or a stale one to a request that named no token, is another's."""
    from gp2040ce_hostlighting.testing import page_reply
    device = fake_device(hlp_fake_board)
    lights = hlp_fake_board.lights
    hlp_fake_board.before[hl.CMD_GET_PAGE] = [page_reply(hl.PAGE_LIGHTS, lights, mask=0b10, sequence=1)]
    assert hl.decode_page_header(device.get_page(hl.PAGE_LIGHTS, 0, 0b11)).groups == 0b11
    hlp_fake_board.before[hl.CMD_GET_PAGE] = [page_reply(hl.PAGE_STATE, [], sequence=2, status=hl.STATUS_STALE_TOKEN)]
    assert len(device.read_record(hl.PAGE_STATE).records) == 1
    token = hlp_fake_board.token
    hlp_fake_board.before[hl.CMD_GET_PAGE] = [page_reply(hl.PAGE_LIGHTS, lights, token=token ^ 1, sequence=3)]
    reply = device.get_page(hl.PAGE_LIGHTS, 0, 0, token)
    assert reply[2] == hl.STATUS_OK and hl.decode_page_header(reply).token == token


def test_a_mask_naming_no_defined_group_reads_no_records(hlp_fake_device):
    """Test that a mask naming only a later group reads the total and no records, not a stalled walk."""
    assert hlp_fake_device.read_table(hl.PAGE_LIGHTS, 1 << 6).records == ()
