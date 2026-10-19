"""Test the fake boards' additions: frames, another application, idle animations, reconfiguring and the bus.

SPDX-FileCopyrightText: © 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import pytest

from gp2040ce_hostlighting import protocol as hl
from gp2040ce_hostlighting.testing import FakeBoard, ModelBoard, attach, control_record, fake_device, light_record


@pytest.mark.parametrize('board_type', [FakeBoard, ModelBoard])
def test_frames_keep_what_each_publish_showed(board_type):
    """Test that a board records each published frame by LED, entries in order with the later one winning."""
    board = board_type()
    device = fake_device(board)
    device.claim(hl.CLAIM_TAKE, 0x51, b'app')
    device.stage_entries(hl.ADDRESSING_LIGHT, [(0, (0, 9, 0))])
    device.stage_entries(hl.ADDRESSING_KIND, [(hl.KIND_CASE, (0, 0, 9))], commit=True)
    assert board.frames == [{0: (0, 9, 0), 1: (0, 0, 9), 2: (0, 0, 9), 3: (0, 0, 9)}]
    device.stage_entries(hl.ADDRESSING_LIGHT, [(1, (1, 2, 3, 4))], hl.PIXEL_RGBW, commit=True)
    assert board.frames[-1] == {0: (0, 9, 0), 1: (1, 2, 3, 4), 2: (1, 2, 3, 4), 3: (1, 2, 3, 4)}
    device.request_ok(hl.CMD_CLEAR, flags=hl.FLAG_COMMIT_AFTER)
    assert board.frames[-1] == {}
    device.fill((0xFF, 0x00, 0x00), hl.PIXEL_RGB565, commit=True)
    assert board.frames[-1][0] == (0xFF, 0x00, 0x00) and len(board.frames[-1]) == board.led_limit()


@pytest.mark.parametrize('board_type', [FakeBoard, ModelBoard])
def test_runs_and_unstage_stage_and_clear_leds(board_type):
    """Test that a board stages LED and light runs, and that UNSTAGE by entry or run clears the LEDs it reaches."""
    board = board_type()
    device = fake_device(board)
    device.stage_run(hl.ADDRESSING_LED_RUN, 0, [(5, 5, 5)] * 2)
    device.stage_run(hl.ADDRESSING_LIGHT_RUN, 1, [(7, 7, 7)], commit=True)
    assert board.frames == [{0: (5, 5, 5), 1: (7, 7, 7), 2: (7, 7, 7), 3: (7, 7, 7)}]
    device.request_ok(hl.CMD_UNSTAGE, bytes([hl.ADDRESSING_LIGHT, 1, 1, 0]), hl.FLAG_COMMIT_AFTER)
    assert board.frames[-1] == {0: (5, 5, 5)}
    assert device.unstage_run(hl.ADDRESSING_LED_RUN, 0, 1, commit=True).applied == 1 and board.frames[-1] == {}


@pytest.mark.parametrize('board_type', [FakeBoard, ModelBoard])
def test_a_board_refuses_staging_past_the_limit_or_the_capacity(board_type):
    """Test status 2 for an LED run past the addressable LED limit and for more entries than a report holds."""
    board = board_type()
    device = fake_device(board)
    last = board.led_limit() - 1
    with pytest.raises(hl.HostLightingRejectedError) as rejected:
        device.stage_run(hl.ADDRESSING_LED_RUN, last, [(1, 1, 1)] * 2, commit=True)
    assert (rejected.value.status, rejected.value.offset) == (hl.STATUS_INVALID, 6) and not board.frames
    for command in (hl.CMD_STAGE, hl.CMD_UNSTAGE):
        pixel_format = hl.PIXEL_RGB if command == hl.CMD_STAGE else None
        count = hl.stage_capacity(hl.ADDRESSING_LIGHT, pixel_format) + 1
        reply = device.request(command, bytes([hl.ADDRESSING_LIGHT, count]))
        assert (reply[2], reply[3]) == (hl.STATUS_INVALID, 4)


@pytest.mark.parametrize('board_type', [FakeBoard, ModelBoard])
def test_a_board_answers_page_0_stale_or_past_its_record(board_type):
    """Test page 0 under a stale token (status 3) and from start 1 (the start echoed, no strings), as the firmware."""
    device = fake_device(board_type())
    stale = hl.decode_page_header(device.get_page(hl.PAGE_IDENTITY, token=1))
    assert (stale.page, stale.groups, stale.count) == (hl.PAGE_IDENTITY, 0, 0)
    past = hl.decode_page_header(device.get_page(hl.PAGE_IDENTITY, start=1))
    assert (past.start, past.total, past.count, past.groups) == (1, 1, 0, 0)


def test_another_application_takes_over_and_lets_go():
    """Test take_over() and let_go(): the holder, the claim flags and the generation step."""
    for board in (FakeBoard(), ModelBoard()):
        device = fake_device(board)
        generation = device.read_record(hl.PAGE_STATE).records[0].controller.claim_generation
        board.take_over(0xB2, b'MESH', exclusive=True)
        holder = device.read_record(hl.PAGE_STATE).records[0].controller
        assert (holder.holder_token, holder.application_name, holder.exclusive) == (0xB2, 'MESH', True)
        assert holder.claim_generation == generation % hl.GENERATION_MAX + 1
        board.let_go()
        assert device.read_record(hl.PAGE_STATE).records[0].controller.holder_token == 0


def test_idle_animations_show_on_page_1_and_raise_an_event():
    """Test idle(): page 1's flag bit 2, and on the model board a STATE_CHANGED event."""
    board = ModelBoard()
    device = fake_device(board)
    device.subscribe([hl.EVENT_STATE_CHANGED])
    device.read_record(hl.PAGE_STATE)
    board.idle()
    assert device.read_record(hl.PAGE_STATE).records[0].state.animations_idle
    event = device.next_event(0.01)
    assert event is not None and hl.decode_state_changed(event).record.state.animations_idle
    board.idle(False)
    assert not device.read_record(hl.PAGE_STATE).records[0].state.animations_idle
    fake = FakeBoard()
    fake.idle()
    assert fake_device(fake).read_record(hl.PAGE_STATE).records[0].state.animations_idle


@pytest.mark.parametrize('board_type', [FakeBoard, ModelBoard])
def test_reconfigure_changes_the_pages_token_and_counters(board_type):
    """Test reconfigure(): the Summary's totals, a new token, the changed pages' counters and an event."""
    board = board_type()
    device = fake_device(board)
    device.subscribe([hl.EVENT_STATE_CHANGED])
    before = device.read_record(hl.PAGE_SUMMARY)
    board.reconfigure(lights=[light_record(0, pin=2), light_record(1), light_record(2)],
                      controls=[control_record(2, 1), control_record(3, 2, lit=0)])
    after = device.read_record(hl.PAGE_SUMMARY)
    summary, changes = after.records[0].summary, after.records[0].changes
    assert (summary.lights, summary.controls, summary.lit_controls, summary.unlit_controls) == (3, 2, 1, 1)
    assert after.token != before.token
    assert changes.counter(hl.PAGE_LIGHTS) == before.records[0].changes.counter(hl.PAGE_LIGHTS) + 1
    assert changes.counter(hl.PAGE_CONTROLS) == before.records[0].changes.counter(hl.PAGE_CONTROLS) + 1
    assert device.next_event(0.01) is not None


@pytest.mark.parametrize('board_type', [FakeBoard, ModelBoard])
def test_unplug_and_replug(board_type):
    """Test that an unplugged board fails reads and writes, and a replugged one starts a new session."""
    board = board_type()
    device = fake_device(board)
    device.subscribe([hl.EVENT_STATE_CHANGED])
    device.claim(hl.CLAIM_TAKE, 0x51, b'app')
    token = device.read_record(hl.PAGE_STATE).token
    board.unplug()
    with pytest.raises(hl.HostLightingDisconnectedError):
        device.hello()
    board.replug()
    again = fake_device(board)
    assert again.handle is board and not again.hello().events_enabled
    read = again.read_record(hl.PAGE_STATE)
    assert read.records[0].controller.holder_token == 0 and read.token == token
    assert again.read_record(hl.PAGE_SUMMARY).records[0].changes.counters == (0,) * 8


def test_attach_puts_boards_on_a_fake_bus(monkeypatch):
    """Test that attach() lets discovery find, open and choose fake boards, and leaves unplugged ones out."""
    first, second = FakeBoard(), FakeBoard()
    second.board_id = bytes.fromhex('4330313435393032')
    attach(monkeypatch, first, second, other_majors=(0x4C,))
    assert len(hl.find_devices()) == 2
    assert hl.open_device('43303134').handle.handle is second
    assert hl.find_board('433030343237362E').handle.handle is first
    second.unplug()
    assert hl.find_board('4330313435393032') is None and len(hl.find_devices()) == 1
    with pytest.raises(hl.HostLightingError, match='multiple boards'):
        second.replug()
        hl.open_device()


def test_a_fake_board_answers_as_the_firmware_does():
    """Test FakeBoard's CONFIGURE, takeover, change of holder and argument checks against the firmware's rules."""
    board = FakeBoard()
    device = fake_device(board)
    assert device.configure(keepalive_ms=0).keepalive_ms == 2000
    assert device.configure(keepalive_ms=50, takeover=hl.TAKEOVER_OVERLAY).keepalive_ms == 100
    state = device.read_record(hl.PAGE_STATE).records[0]
    assert state.settings == hl.Settings(takeover=hl.TAKEOVER_OVERLAY, keepalive_ms=100)
    number = state.state.takeover_number
    device.fill((1, 2, 3), commit=True)
    state = device.read_record(hl.PAGE_STATE).records[0].state
    assert state.takeover_live and state.takeover_number == number + 1
    board.take_over()
    state = device.read_record(hl.PAGE_STATE).records[0]
    assert not state.state.takeover_live and state.settings == hl.Settings() and not board.staged
    assert device.request(hl.CMD_STAGE, bytes([9, 1]))[2:4] == bytes([hl.STATUS_INVALID, 3])
    assert device.request(hl.CMD_FILL, bytes([5 << 4, 1, 2, 3]))[2:4] == bytes([hl.STATUS_INVALID, 3])
    assert device.request(hl.CMD_SET_INPUT_MODE, bytes([200]) + hl.MAGIC_INPUT_MODE)[2] == hl.STATUS_INVALID
    assert device.request(0x06)[2] == hl.STATUS_UNSUPPORTED


def test_a_second_handle_closes_after_a_replug():
    """Test that closing another application's handle after replug() raises nothing, as hidapi's close()."""
    board = FakeBoard()
    fake_device(board)
    second = fake_device(board)
    board.unplug()
    board.replug()
    second.close()
    second.handle.close()


def test_a_model_board_serves_a_second_handle_while_the_first_is_idle():
    """Test that a read on any handle sends what waits on the board, and an event reaches every handle."""
    board = ModelBoard()
    first = fake_device(board)
    second = fake_device(board)
    for _ in range(20):
        second.hello()
    second.subscribe([hl.EVENT_STATE_CHANGED])
    second.take_events()
    board.take_over()
    assert [hl.decode_state_changed(event).record.controller.holder_token for event in second.take_events()] == [0xB2]
    assert len(first.take_events()) == 1
