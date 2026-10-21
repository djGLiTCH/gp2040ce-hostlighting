"""Test the fake boards' additions: frames, another application, idle animations, reconfiguring and the bus.

SPDX-FileCopyrightText: © 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import struct

import pytest

from gp2040ce_hostlighting import protocol as hl
from gp2040ce_hostlighting.testing import (HELLO_COMMANDS, FakeBoard, ModelBoard, attach, control_record, fake_device,
                                           light_record)


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


@pytest.mark.parametrize('board_type', [FakeBoard, ModelBoard])
def test_an_unknown_page_is_refused_before_the_token_is_checked(board_type):
    """Test GET_PAGE of a page the board does not list under a stale token: status 2 at [3], as the firmware."""
    device = fake_device(board_type())
    reply = device.request(hl.CMD_GET_PAGE, struct.pack('<BHIH', 13, 0, 1, 0))
    assert reply[2:4] == bytes([hl.STATUS_INVALID, 3])


@pytest.mark.parametrize('exclusive', [False, True])
def test_a_claims_flags_act_only_when_it_leaves_the_sender_holding(exclusive):
    """Test that another token's CLAIM publishes nothing with COMMIT_AFTER, and the holder's does, either claim."""
    board = ModelBoard()
    device = fake_device(board)
    device.claim(hl.CLAIM_TAKE, 0x51, b'app', exclusive=exclusive)
    device.fill((1, 2, 3))
    number = device.read_record(hl.PAGE_STATE).records[0].state.takeover_number
    other = bytes([hl.CLAIM_TAKE]) + struct.pack('<I', 0x52) + b'other'.ljust(16, b'\x00')
    assert device.request(hl.CMD_CLAIM, other, hl.FLAG_COMMIT_AFTER)[2] == hl.STATUS_OK
    state = device.read_record(hl.PAGE_STATE).records[0].state
    assert not state.takeover_live and state.takeover_number == number
    own = bytes([hl.CLAIM_TAKE]) + struct.pack('<I', 0x51) + b'app'.ljust(16, b'\x00') + bytes([exclusive])
    assert device.request(hl.CMD_CLAIM, own, hl.FLAG_COMMIT_AFTER)[2] == hl.STATUS_OK
    state = device.read_record(hl.PAGE_STATE).records[0].state
    assert state.takeover_live and state.takeover_number == number + 1


@pytest.mark.parametrize('board_type', [FakeBoard, ModelBoard])
def test_hello_lists_what_the_board_lists_and_the_rest_answers_as_absent(board_type):
    """Test HELLO from the attributes: magic, version and bitmaps; an unlisted command and page answer as absent."""
    board = board_type()
    device = fake_device(board)
    hello = device.hello()
    assert (hello.major, hello.minor, hello.commands) == (2, 0, frozenset(HELLO_COMMANDS))
    board.version = (2, 3)
    board.commands.discard(hl.CMD_SET_PROFILE)
    board.pages.discard(hl.PAGE_ANIMATIONS)
    board.events.clear()
    board.addressing_modes.discard(hl.ADDRESSING_ACTION)
    hello = device.hello()
    assert hello.minor == 3 and hl.CMD_SET_PROFILE not in hello.commands and not hello.events
    assert hl.PAGE_ANIMATIONS not in hello.pages and hl.ADDRESSING_ACTION not in hello.addressing_modes
    assert device.request(hl.CMD_SET_PROFILE, bytes([1]))[2] == hl.STATUS_UNSUPPORTED
    page = device.request(hl.CMD_GET_PAGE, struct.pack('<BHIH', hl.PAGE_ANIMATIONS, 0, 0, 0))
    assert page[2:4] == bytes([hl.STATUS_INVALID, 3])
    board.magic = b'NOPE'
    with pytest.raises(hl.HostLightingMagicError):
        device.hello()


@pytest.mark.parametrize('board_type', [FakeBoard, ModelBoard])
def test_drop_and_reject_run_nothing(board_type):
    """Test drop (neither run nor answered) and reject (the status named, not run), by command and by [3]."""
    board = board_type()
    device = fake_device(board)
    stage = bytes([hl.ADDRESSING_LED_RUN]) + struct.pack('<HB', 0, 1) + bytes([1, 2, 3])
    board.drop.add(hl.CMD_STAGE)
    device.send(hl.CMD_STAGE, stage, hl.FLAG_COMMIT_AFTER)
    assert not board.frames and not board.staged
    board.drop.clear()
    board.reject[hl.CMD_STAGE] = hl.STATUS_INVALID
    assert device.request(hl.CMD_STAGE, stage, hl.FLAG_COMMIT_AFTER)[2:4] == bytes([hl.STATUS_INVALID, 3])
    assert not board.frames and not board.staged
    device.claim(hl.CLAIM_TAKE, 0x51, b'app')
    board.reject = {(hl.CMD_CLAIM, hl.CLAIM_GIVE_UP): hl.STATUS_INVALID}
    reply = device.request(hl.CMD_CLAIM, bytes([hl.CLAIM_GIVE_UP]) + struct.pack('<I', 0x51))
    assert reply[2:4] == bytes([hl.STATUS_INVALID, 3])
    assert board.holder_token == 0x51
    board.reject.clear()
    assert device.request(hl.CMD_STAGE, stage, hl.FLAG_COMMIT_AFTER)[2] == hl.STATUS_OK and board.frames


def test_hooks_run_before_each_request():
    """Test that a hook sees each request first and can change the board at that point."""
    board = ModelBoard()
    device = fake_device(board)
    seen = []

    def hook(on, request):
        seen.append(request[0])
        if request[0] == hl.CMD_CONFIGURE:
            on.take_over()
    board.hooks.append(hook)
    device.claim(hl.CLAIM_TAKE, 0x51, b'app')
    with pytest.raises(hl.HostLightingRejectedError) as refused:
        device.configure(keepalive_ms=500)
    assert refused.value.status == hl.STATUS_CLAIMED and refused.value.holder.holder_token == 0xB2
    assert seen == [hl.CMD_CLAIM, hl.CMD_CONFIGURE]


@pytest.mark.parametrize('board_type', [FakeBoard, ModelBoard])
def test_a_summary_set_to_none_follows_the_pages(board_type):
    """Test the derived Summary: extent and grid from the lights, totals from the pages, counters kept apart."""
    board = board_type()
    board.summary = None
    device = fake_device(board)
    summary = device.read_record(hl.PAGE_SUMMARY).records[0].summary
    cells = [light[4] for light in board.lights]
    assert (summary.led_extent, summary.grid_width, summary.grid_height) == (
        4, max(cell[0] for cell in cells) + 1, max(cell[1] for cell in cells) + 1)
    assert (summary.controls, summary.lights, summary.profiles) == (len(board.controls), 2, 1)
    board.reconfigure(lights=board.lights + [light_record(4, 6, x=4, y=3)])
    record = device.read_record(hl.PAGE_SUMMARY).records[0]
    assert (record.summary.led_extent, record.summary.grid_width, record.summary.grid_height) == (10, 5, 4)
    assert record.summary.lights == 3 and record.changes.counter(hl.PAGE_LIGHTS) == 1


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


def test_a_profile_switch_brings_its_pages_token_and_one_event():
    """Test SET_PROFILE on the model board: the profile's pages and a new token, with one event carrying both."""
    board = ModelBoard()
    board.profiles.append([bytes([2, 1]) + b'Second'.ljust(16, b'\x00')])
    board.profiles.append([bytes([3, 1]) + b'Third'.ljust(16, b'\x00')])
    lights = [light_record(0, pin=5, action=1, x=1, y=0), light_record(1, 3, kind=1, slot=0, x=0, y=1)]
    board.profile_pages[2] = (board.controls, lights)
    device = fake_device(board)
    device.subscribe([hl.EVENT_STATE_CHANGED])
    before = device.read_record(hl.PAGE_STATE)
    device.send(hl.CMD_SET_PROFILE, bytes([2]))
    assert board.state[0][2] == 2 and board.token == before.token and not board.pending
    events = [hl.decode_state_changed(event) for event in device.take_events()]
    after = device.read_table(hl.PAGE_LIGHTS)
    assert after.token != before.token and after.records[0].owner.pin == 5
    assert [(event.record.state.profile, event.token) for event in events] == [(2, after.token)]
    device.set_profile(2)
    assert device.take_events() == []
    device.set_profile(3)                   # a profile without pages of its own keeps them
    events = [hl.decode_state_changed(event) for event in device.take_events()]
    assert [(event.record.state.profile, event.token) for event in events] == [(3, after.token)]


def test_set_animation_wakes_the_animations_at_the_next_render_tick():
    """Test that an animation picked during the idle timeout clears the idle flag one render period later."""
    board = ModelBoard()
    board.animations.append([bytes([1, 3, 1, 3]), bytes([5, 5, 5])])
    device = fake_device(board)
    board.idle()
    device.set_animation(1)
    state = device.read_record(hl.PAGE_STATE).records[0].state
    assert state.animation == 1 and state.animations_idle
    board.sleep(1 / 40)
    assert not device.read_record(hl.PAGE_STATE).records[0].state.animations_idle
    board.idle()
    device.set_animation(None)
    board.sleep(0.1)
    assert device.read_record(hl.PAGE_STATE).records[0].state.animations_idle
    device.set_animation(1)
    device.set_animation(None)              # before the render tick: the board applies off
    board.sleep(0.1)
    state = device.read_record(hl.PAGE_STATE).records[0].state
    assert state.animation is None and state.animations_idle


def test_a_bus_reset_drops_the_replies_waiting_and_ends_a_claim():
    """Test bus_reset(): the waiting replies go, a claim ends with the next generation, the subscription stays."""
    board = ModelBoard()
    device = fake_device(board)
    other = fake_device(board)
    device.subscribe([hl.EVENT_STATE_CHANGED])
    device.configure(takeover=hl.TAKEOVER_OVERLAY)
    device.fill((1, 2, 3), commit=True)
    device.set_brightness(2)
    board.bus_reset()
    record = other.read_record(hl.PAGE_STATE).records[0]
    assert record.settings.takeover == hl.TAKEOVER_OVERLAY
    assert record.state.takeover_live and record.state.brightness_unsaved
    claimed = device.claim(hl.CLAIM_TAKE, 0x51, b'app')
    sequence = device.send(hl.CMD_HELLO)
    board.bus_reset()
    with pytest.raises(hl.HostLightingTimeoutError):
        device.wait_reply(hl.CMD_HELLO, sequence, 0.01)
    record = other.read_record(hl.PAGE_STATE).records[0]
    assert record.controller.holder_token == 0 and record.settings == hl.Settings()
    assert record.controller.claim_generation == claimed.claim_generation % hl.GENERATION_MAX + 1
    assert other.hello().events_enabled == {hl.EVENT_STATE_CHANGED}


def test_a_suspend_ends_the_takeover_and_unsaved_step_and_sends_nothing():
    """Test suspend() and resume(): what ends, what runs on, and that the board neither sends nor takes a report."""
    board = ModelBoard()
    device = fake_device(board)
    other = fake_device(board)
    device.claim(hl.CLAIM_TAKE, 0x51, b'app')
    device.subscribe([hl.EVENT_STATE_CHANGED])
    device.fill((1, 2, 3), commit=True)
    device.set_brightness(2)
    device.take_events()
    board.suspend()
    assert board.write(bytes([0, hl.CMD_HELLO, 1, 0]) + bytes(61)) == -1
    assert board.read(64) == [] and board.outbox == []
    board.resume()
    assert device.take_events()
    record = device.read_record(hl.PAGE_STATE).records[0]
    assert not record.state.takeover_live and not record.state.brightness_unsaved and not board.staged
    assert record.controller.holder_token == 0x51
    board.suspend()
    board.sleep(2.5)
    board.resume()
    assert other.read_record(hl.PAGE_STATE).records[0].controller.holder_token == 0


def test_deconfigure_ends_the_usb_session():
    """Test deconfigure(): unclaimed and unsubscribed, the CONFIGURE defaults, nothing waiting, no event."""
    board = ModelBoard()
    device = fake_device(board)
    other = fake_device(board)
    device.subscribe([hl.EVENT_STATE_CHANGED])
    claimed = device.claim(hl.CLAIM_TAKE, 0x51, b'app')
    device.configure(takeover=hl.TAKEOVER_OVERLAY, keepalive_ms=500)
    device.fill((1, 2, 3), commit=True)
    device.take_events()
    device.send(hl.CMD_HELLO)
    board.deconfigure()
    assert board.queue == [] and not board.pending
    record = other.read_record(hl.PAGE_STATE).records[0]
    assert record.controller.holder_token == 0 and record.settings == hl.Settings()
    assert not record.state.takeover_live and not board.staged
    assert record.controller.claim_generation == claimed.claim_generation % hl.GENERATION_MAX + 1
    assert not other.hello().events_enabled and device.take_events() == []
    board.deconfigure()
    generation = other.read_record(hl.PAGE_STATE).records[0].controller.claim_generation
    assert generation == record.controller.claim_generation
    board.suspend()
    board.deconfigure()
    assert not board.suspended and other.hello().board_id


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


@pytest.mark.parametrize('board_type', [FakeBoard, ModelBoard])
def test_set_animation_changing_to_an_animation_wakes_the_animations(board_type):
    """Test that SET_ANIMATION changing to an animation clears the idle flag, and that off or no change leaves it."""
    board = board_type()
    device = fake_device(board)
    board.idle()
    device.set_animation(None)
    assert device.read_record(hl.PAGE_STATE).records[0].state.animations_idle
    device.set_animation(0)
    if board_type is ModelBoard:
        board.sleep(1 / 40)             # the next render tick
    assert not device.read_record(hl.PAGE_STATE).records[0].state.animations_idle
    board.idle()
    device.set_animation(0)                 # the animation already shown
    assert device.read_record(hl.PAGE_STATE).records[0].state.animations_idle


def test_fake_board_publishes_a_claims_commit_after_only_for_the_holder():
    """Test that another token's CLAIM with COMMIT_AFTER records no frame on FakeBoard, as on the board."""
    board = FakeBoard()
    device = fake_device(board)
    device.claim(hl.CLAIM_TAKE, 0x51, b'app')
    device.fill((1, 2, 3))
    device.request(hl.CMD_CLAIM, bytes([hl.CLAIM_TAKE]) + struct.pack('<I', 0x52) + b'other'.ljust(16, b'\x00'),
                   hl.FLAG_COMMIT_AFTER)
    assert not board.frames


def test_a_derived_summary_caps_the_extent_at_the_limit():
    """Test that a light past the limit adds nothing to the derived extent, and one across it reaches the limit."""
    board = FakeBoard()
    board.summary = None
    board.lights = [light_record(98, 5), light_record(120, 2)]
    summary = fake_device(board).read_record(hl.PAGE_SUMMARY).records[0].summary
    assert summary.led_extent == 100
