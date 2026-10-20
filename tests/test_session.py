"""Test Session, the specification's full host flow, on the model board's clock.

SPDX-FileCopyrightText: © 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import logging
from types import SimpleNamespace

import pytest

from gp2040ce_hostlighting import claim as claim_module
from gp2040ce_hostlighting import protocol as hl
from gp2040ce_hostlighting import testing
from gp2040ce_hostlighting.claim import ClaimChangeKind, ClaimState
from gp2040ce_hostlighting.frames import Frame, FrameResult
from gp2040ce_hostlighting.records import Requirements, Settings
from gp2040ce_hostlighting.session import Session
from gp2040ce_hostlighting.testing import attach, fake_device, light_record


@pytest.fixture
def board(hlp_model_board, monkeypatch):
    """Put the protocol and the claim on the model board's clock."""
    timing = SimpleNamespace(sleep=hlp_model_board.sleep, monotonic=hlp_model_board.time,
                             perf_counter=hlp_model_board.time)
    for module in (hl, claim_module):
        monkeypatch.setattr(module, 'time', timing)
    return hlp_model_board


def session_on(board, name='host', **options) -> Session:
    """Make a session on the board, on its clock."""
    return Session(fake_device(board), name, clock=board.time, **options)


def test_show_refuses_an_led_run_past_the_limit_before_sending(board):
    """Test that show() raises ValueError for an LED run past the Summary's limit, and sends no STAGE."""
    session = session_on(board)
    session.start()
    frame = Frame()
    frame.run(hl.ADDRESSING_LED_RUN, 0, [(255, 0, 0)] * (board.led_limit() + 1))
    with pytest.raises(ValueError, match='addressable LED limit'):
        session.show(frame)
    assert not board.sent(hl.CMD_STAGE)
    session.close()


def frame_of(*lights) -> Frame:
    """Build a frame that colours lights by ordinal."""
    frame = Frame()
    frame.entries(hl.ADDRESSING_LIGHT, [(light, (255, 0, 0)) for light in lights])
    return frame


def commands(board, start=0) -> list:
    """List the commands sent from a point."""
    return [request[0] for request in board.requests[start:]]


def test_start_runs_the_host_flow_and_close_gives_up(board):
    """Test HELLO, the pages, CLAIM, CONFIGURE, SUBSCRIBE and page 1, then the give up at the end of a with block."""
    with session_on(board, settings=Settings(keepalive_ms=900)) as session:
        assert commands(board) == [hl.CMD_HELLO] + [hl.CMD_GET_PAGE] * 6 + [
            hl.CMD_CLAIM, hl.CMD_CONFIGURE, hl.CMD_SUBSCRIBE, hl.CMD_GET_PAGE]
        assert board.holder_token == session.claim.token and board.keepalive_ms == 900
        assert session.board_id == testing.BOARD_ID.hex().upper() and len(session.cache.lights) == 2
        assert session.board_state.controller.holder_token == session.claim.token
        assert session.claim.state is ClaimState.HELD
    assert board.holder_token == 0 and board.requests[-1][3] == hl.CLAIM_GIVE_UP


def test_a_with_block_with_a_failed_start_closes_the_device(board):
    """Test that a with block closes the device when the start fails, here at a claim another application holds."""
    board.take_over()
    session = session_on(board)
    with pytest.raises(hl.HostLightingClaimedError):
        with session:
            pass
    assert session.device.closed


def test_start_checks_what_the_session_and_the_host_need(board):
    """Test that a capability missing from HELLO refuses the start before anything is claimed."""
    session = session_on(board, require=Requirements(addressing_modes=frozenset({9})))
    with pytest.raises(hl.HostLightingIncompatibleError, match=r'addressing mode 9'):
        session.start()
    sent = len(board.requests)
    session.close()
    assert not board.sent(hl.CMD_CLAIM) and commands(board, sent) == []
    assert hl.CMD_RELEASE in session_on(board, None).requirements().commands
    assert 3 in session_on(board, exclusive=True).requirements().request_flags


def test_a_failure_after_the_claim_gives_it_up(board):
    """Test that a refused SUBSCRIBE after the claim gives the claim up before the error is raised."""
    answer = board.answer

    def refusing(command, sequence, request):
        if command == hl.CMD_SUBSCRIBE:
            return bytes([command | hl.REPLY_FLAG, sequence, hl.STATUS_INVALID, 3]) + bytes(60)
        return answer(command, sequence, request)
    board.answer = refusing
    with pytest.raises(hl.HostLightingRejectedError):
        session_on(board).start()
    assert board.holder_token == 0 and board.requests[-1][3] == hl.CLAIM_GIVE_UP


def test_poll_renews_the_claim_and_show_clears_when_addresses_go(board):
    """Test the renewals with KEEPALIVE, the frame shown, and CLEAR first only when the frame names fewer."""
    session = session_on(board).start()
    assert session.show(frame_of(0, 1)) == FrameResult(acknowledged=True, resent=False)
    board.sleep(0.7)
    update = session.poll()
    assert update.claim.kind is ClaimChangeKind.UNCHANGED and not update.restage
    assert board.sent(hl.CMD_CLAIM)[-1][2] & hl.FLAG_KEEPALIVE and board.live
    sent = len(board.requests)
    session.show(frame_of(0, 1))
    session.show(frame_of(1))
    assert commands(board, sent) == [hl.CMD_STAGE, hl.CMD_CLEAR, hl.CMD_STAGE]
    assert board.frames[-1] == {1: (255, 0, 0), 2: (255, 0, 0), 3: (255, 0, 0)}


def test_a_takeover_that_ended_asks_for_a_restage(board):
    """Test that page 1 showing the takeover ended while the claim is held asks for the whole frame again."""
    session = session_on(board).start()
    session.show(frame_of(0))
    assert not session.poll().restage               # page 1 seen since the frame shows it live
    board._end_takeover()
    board._note()
    update = session.poll()
    assert update.restage and not update.board_state.state.takeover_live
    sent = len(board.requests)
    session.show(frame_of(0))
    assert commands(board, sent) == [hl.CMD_CLEAR, hl.CMD_STAGE]


def test_events_older_than_a_frame_ask_for_no_restage(board):
    """Test that an event that arrives before a frame's acknowledgement is applied before the frame counts as shown."""
    session = session_on(board).start()
    board.before[hl.CMD_STAGE] = [testing.state_event(board.token, holder_token=session.claim.token,
                                                      claim_generation=board.generation)]
    assert session.show(frame_of(0))
    update = session.poll()
    assert not update.restage and update.board_state.state.takeover_live
    sent = len(board.requests)
    session.show(frame_of(0))
    assert commands(board, sent) == [hl.CMD_STAGE]


def test_configure_changes_the_settings_for_the_session(board):
    """Test configure() before the start, through the claim, and for a host that does not claim."""
    session = session_on(board)
    assert session.configure(Settings(keepalive_ms=600)) is None
    session.start()
    assert board.keepalive_ms == 600
    overlay = Settings(takeover=hl.TAKEOVER_OVERLAY, keepalive_ms=900)
    assert session.configure(overlay) == overlay and board.overlay == 1 and session.settings == overlay
    session.close()
    plain = session_on(board, None).start()
    assert plain.configure(Settings(keepalive_ms=700)) == Settings(keepalive_ms=700) and board.keepalive_ms == 700
    # the defaults go in place of its own settings, once
    assert plain.configure(Settings()) == Settings(keepalive_ms=2000) and board.keepalive_ms == 2000
    assert plain.configure(Settings()) is None and plain.settings == Settings()


def test_a_claim_taken_over_and_given_back_is_taken_again(board):
    """Test RETAKEN after events show another holder, with the settings sent again and a restage."""
    session = session_on(board, settings=Settings(keepalive_ms=600)).start()
    session.show(frame_of(0))
    board.take_over()
    board.let_go()
    update = session.poll()
    assert update.claim.kind is ClaimChangeKind.RETAKEN and update.restage
    assert board.holder_token == session.claim.token and board.keepalive_ms == 600


def test_a_lost_claim_stops_the_frames(board, caplog):
    """Test LOST without wait_when_taken_over, no frame sent while the claim is lost, and a give up sending nothing."""
    session = session_on(board, wait_when_taken_over=False).start()
    board.take_over()
    with caplog.at_level(logging.INFO, logger='gp2040ce_hostlighting.claim'):
        update = session.poll()
    assert update.claim.kind is ClaimChangeKind.LOST and update.claim.holder.application_name == 'Other'
    assert session.claim.state is ClaimState.LOST
    sent = len(board.requests)
    assert session.show(frame_of(0)) is None
    session.close()
    assert board.holder_token == 0xB2 and hl.CMD_CLAIM not in commands(board, sent)


def test_a_refused_frame_goes_to_the_claim(board, monkeypatch):
    """Test a status 4 frame refusal: no events applied first, the claim waiting, and the next poll saying so."""
    session = session_on(board).start()
    monkeypatch.setattr(session.device, 'take_events', lambda: [])     # the change not yet seen
    board.take_over()
    holder = hl.Controller(holder_token=0xB2, application_name='Other', exclusive=False, claim_generation=3)
    assert session.show(frame_of(0)) == FrameResult(acknowledged=False, resent=False, refused=holder)
    assert session.claim.state is ClaimState.WAITING
    assert session.poll().claim.kind is ClaimChangeKind.LOST
    board.disconnect_on = {hl.CMD_STAGE}
    session.claim.state = ClaimState.HELD                   # held again, as the device sees it too
    session.device.generation = session.claim.generation
    with pytest.raises(hl.HostLightingDisconnectedError):
        session.show(frame_of(0))


def test_a_changed_configuration_is_read_again(board):
    """Test that a token change reads the changed pages again and asks for a restage when Lights changed."""
    session = session_on(board).start()
    board.reconfigure(lights=[light_record(0, pin=2, action=9, duplicated=1, instance=1, instances=2, x=1, y=0),
                              board.lights[1]])
    update = session.poll()
    assert hl.PAGE_LIGHTS in update.pages_read and update.restage
    assert session.cache.lights[0].owner.action == 9
    assert session.poll().pages_read == frozenset()


def test_a_board_without_events_is_polled(board, monkeypatch):
    """Test that page 1 is read each second on a board without events, and applies the holder it shows."""
    def without_events(*arguments, **options):
        reply = bytearray(hello(*arguments, **options))
        reply[43] = 0                                   # no events listed
        return bytes(reply)
    hello = testing.hello_reply
    monkeypatch.setattr(testing, 'hello_reply', without_events)
    session = session_on(board).start()
    assert not board.sent(hl.CMD_SUBSCRIBE)
    sent = len(board.requests)
    session.poll()
    assert hl.CMD_GET_PAGE not in commands(board, sent)
    board.take_over()
    board.sleep(1.0)
    update = session.poll()
    assert update.claim.kind is ClaimChangeKind.LOST and hl.CMD_GET_PAGE in commands(board, sent)
    board.let_go()
    slow = session_on(board, wait=True, state_poll_seconds=5.0).start()
    sent = len(board.requests)
    board.sleep(1.0)
    slow.poll()
    assert hl.CMD_GET_PAGE not in commands(board, sent)


def test_unanswered_renewals_and_frames_lose_the_board(board):
    """Test three requests unanswered in a row, and a frame then HELLO unanswered, each raising Unanswered."""
    session = session_on(board).start()
    answer = board.answer
    silent = {hl.CMD_CLAIM}
    board.answer = lambda command, sequence, request: (None if command in silent
                                                       else answer(command, sequence, request))
    for _ in range(2):
        board.sleep(0.7)
        session.poll()
    board.sleep(0.7)
    with pytest.raises(hl.HostLightingUnansweredError, match="3 of the session's requests"):
        session.poll()
    silent.clear()
    session = session_on(board).start()
    silent.update({hl.CMD_STAGE, hl.CMD_HELLO})
    with pytest.raises(hl.HostLightingUnansweredError, match='neither a frame nor HELLO'):
        session.show(frame_of(0))


def test_a_missed_frame_is_not_shown_and_the_next_clears(board):
    """Test a frame unanswered twice, its HELLO answered: not shown, and the next frame clears first."""
    session = session_on(board).start()
    answer, dropped = board.answer, []

    def dropping(command, sequence, request):
        if command == hl.CMD_STAGE and len(dropped) < 2:
            dropped.append(sequence)
            return None
        return answer(command, sequence, request)
    board.answer = dropping
    assert session.show(frame_of(0)) == FrameResult(acknowledged=False, resent=True)
    sent = len(board.requests)
    assert session.show(frame_of(0)).acknowledged
    assert commands(board, sent) == [hl.CMD_CLEAR, hl.CMD_STAGE]


def test_reconnect_finds_the_board_again_and_waits_for_the_claim(board, monkeypatch):
    """Test that a board back on the bus is found by its ID, read again and claimed with the same token."""
    attach(monkeypatch, board)
    session = Session(hl.open_device(), 'host', clock=board.time).start()
    token = session.claim.token
    board.unplug()
    with pytest.raises(hl.HostLightingDisconnectedError):
        session.show(frame_of(0))
    assert not session.reconnect()
    board.replug()
    board.take_over(0xA1, b'MESH')
    assert session.reconnect() and session.claim.token == token
    assert session.claim.state is ClaimState.WAITING and session.cache.token == board.token
    board.let_go()
    board.sleep(0.7)
    assert session.poll().claim.kind is ClaimChangeKind.TAKEN and board.holder_token == token
    session.board_id = None
    assert not session.reconnect()


def test_a_host_that_does_not_claim(board):
    """Test the flow without a claim: no CLAIM or CONFIGURE, HELLO with KEEPALIVE, RELEASE and the saved step."""
    session = session_on(board, None).start()
    assert hl.CMD_CLAIM not in commands(board) and hl.CMD_CONFIGURE not in commands(board)
    assert session.show(frame_of(0)).acknowledged and session.claim is None
    board.sleep(0.7)
    session.poll()
    keepalive = board.sent(hl.CMD_HELLO)[-1]
    assert keepalive[2] == hl.FLAG_KEEPALIVE | hl.FLAG_NO_REPLY and board.live
    sent = len(board.requests)
    session.close()
    assert commands(board, sent) == [hl.CMD_GET_PAGE, hl.CMD_RELEASE, hl.CMD_SET_BRIGHTNESS]
    assert board.requests[-1][3] == hl.BRIGHTNESS_SAVED and not board.live


def test_a_host_that_does_not_claim_sends_its_settings_while_unclaimed(board):
    """Test CONFIGURE at the start, none while another application holds the claim, then again once it has gone."""
    overlay = Settings(takeover=hl.TAKEOVER_OVERLAY, keepalive_ms=900)
    session = session_on(board, None, settings=overlay).start()
    assert hl.CMD_CONFIGURE in session.requirements().commands
    assert board.overlay == 1 and board.keepalive_ms == 900
    sent = len(board.requests)
    session.poll()
    assert hl.CMD_CONFIGURE not in commands(board, sent)
    board.take_over()
    session.poll()
    assert hl.CMD_CONFIGURE not in commands(board, sent) and board.overlay == 0
    board.let_go()
    session.poll()
    assert commands(board, sent).count(hl.CMD_CONFIGURE) == 1 and board.overlay == 1
    plain = session_on(board, None)
    assert hl.CMD_CONFIGURE not in plain.requirements().commands


def test_a_host_that_does_not_claim_leaves_a_holder_alone(board, caplog):
    """Test that no frame is sent while another application holds the claim, and close sends nothing then."""
    session = session_on(board, None).start()
    board.take_over()
    session.poll()
    assert session.show(frame_of(0)) is None
    sent = len(board.requests)
    session.close()
    assert commands(board, sent) == [hl.CMD_GET_PAGE]
    other = session_on(board, None)
    board.unplug()
    other.hello = board.answer(hl.CMD_HELLO, 1, b'')
    other.close()
    assert 'could not end the session on the board' in caplog.text


def test_a_refresh_that_fails_is_tried_again(board, monkeypatch):
    """Test a refresh unanswered, changing under the reads and left a while, refused with status 4, then landing."""
    session = session_on(board).start()
    board.reconfigure(lights=[light_record(0, pin=2, action=9, duplicated=1, instance=1, instances=2, x=1, y=0),
                              board.lights[1]])
    refresh = session.cache.refresh
    holder = hl.Controller(holder_token=0xB2, application_name='Other', exclusive=False, claim_generation=3)
    failures = [hl.HostLightingTimeoutError('late'), hl.HostLightingError('kept changing'),
                hl.HostLightingRejectedError(hl.CMD_GET_PAGE, hl.STATUS_CLAIMED, 0, holder)]

    def failing(device, attempts=3):
        if failures:
            raise failures.pop(0)
        return refresh(device, attempts)
    monkeypatch.setattr(session.cache, 'refresh', failing)
    assert session.poll().pages_read == frozenset() and session.poll().pages_read == frozenset()
    sent = len(board.requests)
    assert session.poll().pages_read == frozenset() and commands(board, sent) == []
    board.sleep(1.0)
    update = session.poll()
    assert update.claim.kind is ClaimChangeKind.LOST and update.pages_read == frozenset()
    session.claim.state = ClaimState.HELD
    assert hl.PAGE_LIGHTS in session.poll().pages_read
    board.reconfigure(lights=board.lights[:1])
    failures.append(hl.HostLightingRejectedError(hl.CMD_GET_PAGE, hl.STATUS_UNSUPPORTED, 0))
    assert session.poll().pages_read == frozenset()
    board.sleep(1.0)
    failures.append(hl.HostLightingDisconnectedError('gone'))
    with pytest.raises(hl.HostLightingDisconnectedError):
        session.poll()


def test_a_renewal_unanswered_in_show_is_counted(board, monkeypatch):
    """Test that the renewal a refusal showing the claim ended leads to, unanswered, counts toward the limit."""
    session = session_on(board).start()
    monkeypatch.setattr(session.device, 'take_events', lambda: [])     # the change not yet seen
    board.let_go()
    answer = board.answer
    board.answer = lambda command, sequence, request: (None if command == hl.CMD_CLAIM
                                                       else answer(command, sequence, request))
    assert session.show(frame_of(0)).refused.holder_token == 0
    board.sleep(0.7)
    session.poll()
    board.sleep(0.7)
    with pytest.raises(hl.HostLightingUnansweredError):
        session.poll()


def test_a_poll_ends_at_the_first_request_unanswered(board):
    """Test that a poll with an unanswered renewal sends nothing more, so that one poll counts one request."""
    session = session_on(board).start()
    board.reconfigure(lights=board.lights[:1])
    answer = board.answer
    board.answer = lambda command, sequence, request: (None if command == hl.CMD_CLAIM
                                                       else answer(command, sequence, request))
    board.sleep(0.7)
    sent = len(board.requests)
    assert session.poll().pages_read == frozenset() and commands(board, sent) == [hl.CMD_CLAIM]
    board.answer = answer
    board.sleep(0.7)
    assert hl.PAGE_LIGHTS in session.poll().pages_read


def test_reconnect_to_a_board_not_answering_yet_tries_again(board):
    """Test that a board found but silent, or a bus that cannot be listed, leaves reconnect() to try again."""
    session = session_on(board).start()
    answer = board.answer
    board.answer = lambda command, sequence, request: None
    found = []

    def reopen(board_id):
        found.append(fake_device(board))
        return found[-1]
    session.reopen = reopen
    assert not session.reconnect() and found[-1].closed

    def unclosable(board_id):
        device = fake_device(board)
        device.close = lambda: (_ for _ in ()).throw(OSError('gone'))
        return device
    session.reopen = unclosable
    assert not session.reconnect()
    session.reopen = lambda board_id: (_ for _ in ()).throw(OSError('enumeration failed'))
    assert not session.reconnect()
    board.answer, session.reopen = answer, reopen
    session.require = Requirements(addressing_modes=frozenset({9}))
    with pytest.raises(hl.HostLightingIncompatibleError):
        session.reconnect()
    session.require = Requirements()
    assert session.reconnect() and session.claim.state is ClaimState.HELD


def test_the_cache_keeps_the_pages_and_groups_asked_for(board):
    """Test pages and masks, which leave the other pages and groups unread."""
    session = session_on(board, pages=[hl.PAGE_LIGHTS], masks={hl.PAGE_LIGHTS: 0b111}).start()
    pages = [request[3] for request in board.sent(hl.CMD_GET_PAGE)]
    assert pages == [hl.PAGE_SUMMARY, hl.PAGE_LIGHTS, hl.PAGE_STATE]
    assert session.cache.controls is None and not session.cache.lights[0].has('grid')


def test_a_page_1_read_refused_goes_to_the_claim(board, monkeypatch):
    """Test that page 1, read under a generation no longer current on a board without events, loses the claim."""
    def without_events(*arguments, **options):
        reply = bytearray(hello(*arguments, **options))
        reply[43] = 0
        return bytes(reply)
    hello = testing.hello_reply
    monkeypatch.setattr(testing, 'hello_reply', without_events)
    session = session_on(board, settings=Settings(keepalive_ms=9000)).start()
    board.take_over()
    board.sleep(1.0)
    assert session.poll().claim.kind is ClaimChangeKind.LOST
    board.let_go()
    session.claim.take()
    board.reject[hl.CMD_STAGE] = hl.STATUS_INVALID
    with pytest.raises(hl.HostLightingRejectedError, match='INVALID_ARG'):
        session.show(frame_of(0))


def test_reconnect_edges_and_a_session_never_started(board, monkeypatch):
    """Test a device that fails to close, a board gone again during the start, and closing before a start."""
    session = session_on(board).start()
    monkeypatch.setattr(session.device, 'close', lambda: (_ for _ in ()).throw(OSError('gone')))
    board.unplug()
    session.reopen = lambda board_id: fake_device(board)
    assert not session.reconnect()
    never = session_on(board)
    never.close()
    assert never.device.closed
    unclaiming = session_on(board, None)
    unclaiming.close()
    assert unclaiming.device.closed


def test_changes_found_in_turn_combine():
    """Test that the later change wins unless it is unchanged, and a restage either asks for is kept."""
    from gp2040ce_hostlighting.claim import ClaimChange
    from gp2040ce_hostlighting.session import _later
    retaken = ClaimChange(kind=ClaimChangeKind.RETAKEN, restage=True)
    lost = ClaimChange(kind=ClaimChangeKind.LOST)
    unchanged = ClaimChange(kind=ClaimChangeKind.UNCHANGED)
    assert _later(retaken, unchanged) is retaken and _later(unchanged, lost) is lost
    assert _later(retaken, lost) == ClaimChange(kind=ClaimChangeKind.LOST, restage=True)


def test_the_readme_session_example_runs(board, monkeypatch):
    """Test that the README's Session example shows the case light on the model board, then gives the claim up."""
    import pathlib
    import re
    import time
    readme = (pathlib.Path(__file__).parent.parent / 'README.md').read_text(encoding='utf-8')
    example = re.findall(r'```python\n(.*?)```', readme, re.S)[1]
    monkeypatch.setattr(hl, 'open_device', lambda prefix='': fake_device(board))
    monkeypatch.setattr(time, 'sleep', board.sleep)
    exec(compile(example, 'README.md', 'exec'), {})     # the repository's own README
    assert board.frames[-1] == {1: (0, 0, 255), 2: (0, 0, 255), 3: (0, 0, 255)} and board.holder_token == 0
    assert board.now >= 3.0


def test_a_reconnect_that_finds_no_board_leaves_nothing_to_send(board):
    """Test that after a reconnect that found no board, poll() reports the board gone and close() sends nothing."""
    session = session_on(board, reopen=lambda board_id: None).start()
    board.unplug()
    assert session.reconnect() is False and session.claim.state is ClaimState.ENDED
    with pytest.raises(hl.HostLightingDisconnectedError):
        session.poll()
    sent = len(board.requests)
    session.close()
    assert len(board.requests) == sent


def test_another_application_taking_over_between_a_retake_and_its_configure(board):
    """Test that the refused CONFIGURE goes to the claim, poll() says so, and no frame goes to the new holder."""
    session = session_on(board).start()
    board.let_go()
    write = board.write

    def take_over_after_the_claim(data):
        written = write(data)
        if data[1] == hl.CMD_CLAIM:
            board.write = write
            board.take_over()
        return written
    board.write = take_over_after_the_claim
    assert session.poll().claim.kind is ClaimChangeKind.LOST and session.claim.state is ClaimState.WAITING
    stages = len(board.sent(hl.CMD_STAGE))
    assert session.show(frame_of(0)) is None and len(board.sent(hl.CMD_STAGE)) == stages


def test_a_status_4_the_claim_did_not_see_is_learned_before_the_next_frame(board, monkeypatch):
    """Test that a refusal outside the claim's calls has the next show() renew first, and send no frame."""
    session = session_on(board).start()
    monkeypatch.setattr(session.device, 'take_events', lambda: [])     # the change not yet seen
    board.take_over()
    assert session.device.request(hl.CMD_HELLO)[2] == hl.STATUS_CLAIMED
    assert session.device.generation == 0 and session.claim.state is ClaimState.HELD
    stages = len(board.sent(hl.CMD_STAGE))
    assert session.show(frame_of(0)) is None and len(board.sent(hl.CMD_STAGE)) == stages
    assert session.claim.state is ClaimState.WAITING


def test_a_hello_refused_after_a_missed_frame_goes_to_the_claim(board, monkeypatch):
    """Test that the HELLO after a missed frame resets the count, and a status 4 reply to it reaches the claim."""
    from gp2040ce_hostlighting import session as session_module
    session = session_on(board).start()
    monkeypatch.setattr(session_module, 'publish_frame', lambda *arguments, **options: FrameResult(
        acknowledged=False, resent=True))
    monkeypatch.setattr(session.device, 'take_events', lambda: [])
    session._unanswered = 2
    board.take_over()
    assert session.show(frame_of(0)) == FrameResult(acknowledged=False, resent=True)
    assert session.claim.state is ClaimState.WAITING and session._unanswered == 0


def test_an_acknowledged_frame_resets_the_unanswered_count(board):
    """Test that the board acknowledging a frame is an answer, which resets the count of requests unanswered."""
    session = session_on(board).start()
    session._unanswered = 2
    assert session.show(frame_of(0)).acknowledged and session._unanswered == 0


def test_a_host_that_does_not_claim_restages_once_the_board_is_unclaimed(board):
    """Test no KEEPALIVE while another application holds the claim, then a restage once it gives the claim up."""
    session = session_on(board, None).start()
    assert session.show(frame_of(0)).acknowledged
    board.take_over()
    start = len(board.requests)
    for _ in range(3):                      # inside the other application's 2 s lease
        board.sleep(0.5)
        assert not session.poll().restage
    assert not [request for request in board.requests[start:]
                if request[0] == hl.CMD_HELLO and request[2] & hl.FLAG_KEEPALIVE]
    board.let_go()
    assert session.poll().restage


def test_a_cache_without_controls_or_lights_still_restages_when_they_change(board):
    """Test that a moved Lights counter asks for a restage although the cache keeps no Lights page."""
    session = session_on(board, pages=()).start()
    board.reconfigure(lights=[light_record(0, pin=2, action=9, duplicated=1, instance=1, instances=2, x=1, y=0),
                              board.lights[1]])
    update = session.poll()
    assert update.pages_read == frozenset() and update.restage


def test_events_older_than_a_renewals_reply_retake_once(board):
    """Test that a lapse event read while a renewal's reply is awaited does not have the claim retake again."""
    session = session_on(board).start()
    board.let_go()
    board.pending = False                   # the lapse's event arrives with the renewal's reply instead
    board.before[hl.CMD_CLAIM] = [testing.state_event(board.token, holder_token=0,
                                                      claim_generation=board.generation)]
    board.sleep(1.0)                        # a renewal is due
    assert session.poll().claim.kind is ClaimChangeKind.RETAKEN
    claims = len(board.sent(hl.CMD_CLAIM))
    assert session.poll().claim.kind is ClaimChangeKind.UNCHANGED and len(board.sent(hl.CMD_CLAIM)) == claims


def test_two_hosts_that_do_not_claim_leave_each_other_s_settings_alone(board):
    """Test that a host that does not claim sends its settings again only after a change of holder."""
    first = session_on(board, None, settings=Settings(keepalive_ms=3000)).start()
    second = session_on(board, None, settings=Settings(keepalive_ms=5000)).start()
    sent = len(board.sent(hl.CMD_CONFIGURE))
    for _ in range(10):
        board.sleep(0.1)
        first.poll()
        second.poll()
    assert len(board.sent(hl.CMD_CONFIGURE)) == sent
    board.take_over()
    board.let_go()                          # a change of holder returns the defaults
    for _ in range(3):
        board.sleep(0.1)
        first.poll()
        second.poll()
    assert len(board.sent(hl.CMD_CONFIGURE)) == sent + 2


def test_a_start_under_the_same_token_clears_the_frame_it_kept(board):
    """Test that the first frame of a start clears first: a start under the token holding the claim keeps the frame."""
    session = session_on(board).start()
    session.show(frame_of(0, 1))
    session.start()
    session.show(frame_of(0))
    assert board.frames[-1] == {0: (255, 0, 0)}


def test_a_waiting_host_sends_no_configure_before_its_claim(board, monkeypatch):
    """Test that a claiming host that waits sends no CONFIGURE when the holder gives up before page 1 is read."""
    board.take_over()
    write, gone = board.write, []

    def let_go_after_the_claim(data):
        written = write(data)
        if data[1] == hl.CMD_CLAIM and not gone:
            gone.append(True)
            board.let_go()
        return written
    monkeypatch.setattr(board, 'write', let_go_after_the_claim)
    session = session_on(board, settings=Settings(keepalive_ms=600), wait=True).start()
    assert session.claim.state is ClaimState.WAITING and hl.CMD_CONFIGURE not in commands(board)


def test_a_host_that_does_not_claim_puts_its_settings_back_at_close(board):
    """Test close() without a frame: its own settings back to the defaults, and no RELEASE or SET_BRIGHTNESS."""
    session = session_on(board, None, settings=Settings(takeover=hl.TAKEOVER_OVERLAY)).start()
    assert board.overlay == 1
    mark = len(board.requests)
    session.close()
    sent = commands(board, mark)
    assert board.overlay == 0 and session.settings == Settings(takeover=hl.TAKEOVER_OVERLAY)
    assert hl.CMD_CONFIGURE in sent and hl.CMD_RELEASE not in sent and hl.CMD_SET_BRIGHTNESS not in sent


def test_a_host_that_does_not_claim_leaves_others_settings_at_close(board):
    """Test that close() leaves settings another host sent after its own, and releases after a frame."""
    session = session_on(board, None, settings=Settings(takeover=hl.TAKEOVER_OVERLAY)).start()
    session.show(frame_of(0))
    board.take_over()
    board.let_go()                  # a change of holder returns the defaults, and the session sends its own again
    session.poll()
    fake_device(board).configure(hl.TAKEOVER_OVERLAY, 900)     # another host that does not claim, after it
    mark = len(board.requests)
    session.close()
    sent = commands(board, mark)
    assert board.keepalive_ms == 900 and hl.CMD_RELEASE in sent and hl.CMD_SET_BRIGHTNESS in sent


def test_a_session_refuses_to_run_before_start(board):
    """Test that poll() and show() before start() raise and send nothing."""
    session = session_on(board)
    for call in (session.poll, lambda: session.show(frame_of(0))):
        with pytest.raises(hl.HostLightingError, match='has not started'):
            call()
    assert not board.requests


def test_page_1_is_read_within_a_third_of_a_short_timeout(board, monkeypatch):
    """Test that a host without events reads page 1 within a third of the timeout it applied, below 1 s."""
    answer = board.answer

    def no_events(command, sequence, request):
        reply = answer(command, sequence, request)
        return reply[:43] + bytes(2) + reply[45:] if command == hl.CMD_HELLO and reply else reply
    monkeypatch.setattr(board, 'answer', no_events)
    session = session_on(board, None, settings=Settings(keepalive_ms=300)).start()
    mark = len(board.requests)
    board.sleep(0.15)
    session.poll()
    assert hl.CMD_GET_PAGE in commands(board, mark)


@pytest.mark.parametrize('events', [True, False])
def test_configure_of_a_host_that_does_not_claim_sees_a_holder_first(board, monkeypatch, events):
    """Test that configure() takes the events, or reads page 1, and sends nothing under another holder's claim."""
    if not events:
        answer = board.answer

        def no_events(command, sequence, request):
            reply = answer(command, sequence, request)
            return reply[:43] + bytes(2) + reply[45:] if command == hl.CMD_HELLO and reply else reply
        monkeypatch.setattr(board, 'answer', no_events)
    session = session_on(board, None).start()
    board.take_over()
    assert session.configure(Settings(keepalive_ms=900)) is None
    assert hl.CMD_CONFIGURE not in commands(board) and board.keepalive_ms == 2000


def test_configure_after_an_unseen_change_of_holder_is_put_back_at_close(board):
    """Test that configure() acts under the generation page 1 shows now, so close() puts the defaults back."""
    session = session_on(board, None).start()
    board.take_over()
    board.let_go()                  # two changes of holder the session has not seen
    assert session.configure(Settings(takeover=hl.TAKEOVER_OVERLAY)) is not None and board.overlay == 1
    session.close()
    assert board.overlay == 0


@pytest.mark.parametrize('events', [True, False])
@pytest.mark.parametrize('waits', [True, False])
def test_a_take_over_during_start_is_waited_for_or_raised(board, monkeypatch, waits, events):
    """Test a take over between CONFIGURE and the end of start(): WAITING, or HostLightingClaimedError.

    With events SUBSCRIBE is refused, and without them the page 1 read.
    """
    if not events:
        without_events(board, monkeypatch)
    write = board.write

    def take_over_after_configure(data):
        written = write(data)
        if data[1] == hl.CMD_CONFIGURE and not board.holder_token == 0xB2:
            board.take_over()
        return written
    monkeypatch.setattr(board, 'write', take_over_after_configure)
    session = session_on(board, wait=True, wait_when_taken_over=waits)
    if waits:
        assert session.start().claim.state is ClaimState.WAITING
    else:
        with pytest.raises(hl.HostLightingClaimedError, match="'Other' holds the lights"):
            session.start()
    assert [request[3] for request in board.sent(hl.CMD_CLAIM)] == [hl.CLAIM_TAKE]     # no give up
    assert board.holder_token == 0xB2


def test_a_frame_that_fails_has_the_next_clear_first(board):
    """Test that a frame refused with another status leaves the next to clear what it staged."""
    session = session_on(board).start()
    session.show(frame_of(0))
    board.reject[hl.CMD_STAGE] = hl.STATUS_INVALID
    with pytest.raises(hl.HostLightingRejectedError, match='INVALID_ARG'):
        session.show(frame_of(1))
    board.reject.clear()
    session.show(frame_of(0))
    assert board.frames[-1] == {0: (255, 0, 0)}


def test_a_frame_that_sends_nothing_does_not_count_as_driven(board, monkeypatch):
    """Test that a host that does not claim, with its only show() failed before sending, releases nothing at close."""
    session = session_on(board, None).start()
    frame = frame_of(0)

    def unbuildable():
        raise ValueError("no payloads")
    monkeypatch.setattr(frame, 'payloads', unbuildable)
    with pytest.raises(ValueError, match='no payloads'):
        session.show(frame)
    mark = len(board.requests)
    session.close()
    assert hl.CMD_RELEASE not in commands(board, mark)


def test_settings_sent_after_a_page_1_reply_older_than_the_board_are_put_back(board, monkeypatch):
    """Test that the generation is read after CONFIGURE, so close() restores the defaults after such a race."""
    answer, write, raced = board.answer, board.write, []

    def no_events(command, sequence, request):
        reply = answer(command, sequence, request)
        return reply[:43] + bytes(2) + reply[45:] if command == hl.CMD_HELLO and reply else reply

    def race_the_page_1_reply(data):
        written = write(data)
        if data[1] == hl.CMD_GET_PAGE and data[4] == hl.PAGE_STATE and armed and not raced:
            raced.append(True)          # the reply on its way shows the board before these two changes
            board.take_over()
            board.let_go()
        return written
    monkeypatch.setattr(board, 'answer', no_events)
    monkeypatch.setattr(board, 'write', race_the_page_1_reply)
    armed = False
    session = session_on(board, None).start()
    armed = True
    assert session.configure(Settings(takeover=hl.TAKEOVER_OVERLAY)) is not None and raced and board.overlay == 1
    session.close()
    assert board.overlay == 0


def hook_after(board, monkeypatch, when, then):
    """Run then() once, just after the board takes the first request when() picks."""
    write, done = board.write, []

    def hooked(data):
        written = write(data)
        if not done and when(data):
            done.append(True)
            then()
        return written
    monkeypatch.setattr(board, 'write', hooked)
    return done


def test_settings_with_the_page_1_read_after_lost_are_still_put_back(board, monkeypatch):
    """Test that the settings count as sent once CONFIGURE is answered, though the read after it goes unanswered."""
    session = session_on(board, None).start()
    write, lost = board.write, []

    def lose_the_read_after_configure(data):
        if data[1] == hl.CMD_GET_PAGE and data[4] == hl.PAGE_STATE and armed and not lost:
            lost.append(True)
            board.requests.append(bytes(data[1:]))
            return len(data)
        written = write(data)
        return written
    armed = False
    monkeypatch.setattr(board, 'write', lose_the_read_after_configure)
    session.poll()
    original = session.device.configure

    def configure_then_arm(*arguments):
        nonlocal armed
        applied = original(*arguments)
        armed = True
        return applied
    monkeypatch.setattr(session.device, 'configure', configure_then_arm)
    assert session.configure(Settings(takeover=hl.TAKEOVER_OVERLAY)) is not None and lost and board.overlay == 1
    session.close()
    assert board.overlay == 0


@pytest.mark.parametrize('events', [True, False])
def test_a_take_over_before_the_start_configure_is_waited_for(board, monkeypatch, events):
    """Test that a host that does not claim, refused CONFIGURE in start() by a new exclusive claim, starts and waits.

    Without events, page 1 is read at the next poll, since the refusal showed a holder.
    """
    if not events:
        answer = board.answer

        def no_events(command, sequence, request):
            reply = answer(command, sequence, request)
            return reply[:43] + bytes(2) + reply[45:] if command == hl.CMD_HELLO and reply else reply
        monkeypatch.setattr(board, 'answer', no_events)
    done = hook_after(board, monkeypatch, lambda data: data[1] == hl.CMD_GET_PAGE and data[4] == hl.PAGE_STATE,
                      lambda: board.take_over(exclusive=True))
    session = session_on(board, None, settings=Settings(takeover=hl.TAKEOVER_OVERLAY)).start()
    assert done and board.holder_token == 0xB2 and board.overlay == 0
    session.poll()
    assert session.board_state.controller.holder_token == 0xB2


def test_close_sends_no_release_once_another_application_holds_the_claim(board, monkeypatch):
    """Test that close() reads page 1 after its last CONFIGURE and sends no RELEASE under a claim taken since."""
    session = session_on(board, None, settings=Settings(takeover=hl.TAKEOVER_OVERLAY)).start()
    session.show(frame_of(0))
    hook_after(board, monkeypatch, lambda data: data[1] == hl.CMD_CONFIGURE, board.take_over)
    mark = len(board.requests)
    session.close()
    assert hl.CMD_CONFIGURE in commands(board, mark) and hl.CMD_RELEASE not in commands(board, mark)


def without_events(board, monkeypatch):
    """Have the board's HELLO list no events."""
    answer = board.answer

    def no_events(command, sequence, request):
        reply = answer(command, sequence, request)
        return reply[:43] + bytes(2) + reply[45:] if command == hl.CMD_HELLO and reply else reply
    monkeypatch.setattr(board, 'answer', no_events)


def lose_replies(board, monkeypatch, picks, count=1):
    """Have the board carry out the next requests picks() chooses but lose their replies."""
    write, lost = board.write, []

    def losing(data):
        written = write(data)
        if len(lost) < count and picks(data):
            lost.append(True)
            board.queue[:] = [report for report in board.queue
                              if not (report[0] == data[1] | hl.REPLY_FLAG and report[1] == data[2])]
        return written
    monkeypatch.setattr(board, 'write', losing)
    return lost


def test_a_configure_with_its_reply_lost_is_put_back_at_close(board, monkeypatch):
    """Test that a host that does not claim takes a CONFIGURE without a reply as maybe applied, as page 1 shows."""
    session = session_on(board, None).start()
    lost = lose_replies(board, monkeypatch, lambda data: data[1] == hl.CMD_CONFIGURE)
    with pytest.raises(hl.HostLightingTimeoutError):
        session.configure(Settings(takeover=hl.TAKEOVER_OVERLAY))
    assert lost and board.overlay == 1
    session.close()
    assert board.overlay == 0


def test_a_with_block_start_losing_the_configure_reply_puts_the_defaults_back(board, monkeypatch):
    """Test that a start that fails ends as close() does, putting back the settings it may have applied."""
    lose_replies(board, monkeypatch, lambda data: data[1] == hl.CMD_CONFIGURE)
    with pytest.raises(hl.HostLightingTimeoutError):
        with session_on(board, None, settings=Settings(takeover=hl.TAKEOVER_OVERLAY)):
            pass
    assert board.overlay == 0


@pytest.mark.parametrize('lost', ['CONFIGURE', 'page 1'])
def test_a_poll_reads_page_1_before_sending_configure_again(board, monkeypatch, lost):
    """Test that after an unanswered request, a poll sends no CONFIGURE under a claim it has not seen."""
    without_events(board, monkeypatch)
    session = session_on(board, None).start()
    if lost == 'CONFIGURE':
        picks = lambda data: data[1] == hl.CMD_CONFIGURE                            # noqa: E731
    else:
        picks = lambda data: data[1] == hl.CMD_GET_PAGE and data[4] == hl.PAGE_STATE  # noqa: E731
    lose_replies(board, monkeypatch, picks)
    with pytest.raises(hl.HostLightingTimeoutError):
        session.configure(Settings(takeover=hl.TAKEOVER_OVERLAY))
    board.take_over()
    mark = len(board.requests)
    session.poll()
    assert hl.CMD_CONFIGURE not in commands(board, mark) and board.holder_token == 0xB2


def test_start_again_after_an_unseen_lapse_sends_no_old_generation(board):
    """Test that start() again on the same device sends nothing under a generation that lapsed unseen."""
    session = session_on(board).start()
    board.sleep(2.5)
    assert session.start().claim.state is ClaimState.HELD


def test_configure_of_a_claiming_host_drops_the_events_older_than_its_renewal(board):
    """Test that the events read while configure()'s renewal was awaited do not have the next poll claim again."""
    session = session_on(board, settings=Settings(keepalive_ms=900)).start()
    session.show(frame_of(0))
    board.sleep(1.2)                # the lease lapses, and its event waits unread
    session.configure(Settings(keepalive_ms=1500))
    mark = len(board.requests)
    session.poll()
    assert hl.CMD_CLAIM not in commands(board, mark) and hl.CMD_CONFIGURE not in commands(board, mark)


def test_a_host_that_does_not_claim_reads_page_1_even_with_events(board):
    """Test that a host that does not claim learns of a holder with an event it never received, and shows nothing."""
    session = session_on(board, None).start()
    board.subscribed = 0            # the event of the take over is lost
    board.take_over()
    board.sleep(1.1)
    session.poll()
    assert session.board_state.controller.holder_token == 0xB2 and session.show(frame_of(0)) is None


def test_a_reconnect_with_a_failed_start_puts_back_the_settings_it_sent(board, monkeypatch):
    """Test that a reconnect() with a start that loses the CONFIGURE reply ends the session as close() does."""
    session = session_on(board, None, settings=Settings(takeover=hl.TAKEOVER_OVERLAY),
                         reopen=lambda board_id: fake_device(board)).start()
    board.overlay = 0                       # as after a power cycle
    lose_replies(board, monkeypatch, lambda data: data[1] == hl.CMD_CONFIGURE)
    assert session.reconnect() is False
    assert board.overlay == 0 and session.device.closed


def test_a_zero_timeout_counts_as_the_defaults(board):
    """Test that a host that does not claim with keepalive_ms 0, the board's default, leaves another's settings."""
    fake_device(board).configure(hl.TAKEOVER_OVERLAY, 2000)
    session = session_on(board, None, settings=Settings(keepalive_ms=0)).start()
    assert hl.CMD_CONFIGURE not in commands(board)[1:] and board.overlay == 1
    session.close()
    assert board.overlay == 1


@pytest.mark.parametrize('started', [Settings(), Settings(keepalive_ms=5000)])
def test_a_configure_with_its_request_lost_is_sent_again(board, monkeypatch, started):
    """Test that a host that does not claim sends again a CONFIGURE that page 1 shows did not land."""
    session = session_on(board, None, settings=started).start()
    write, lost = board.write, []

    def lose_one_request(data):
        if data[1] == hl.CMD_CONFIGURE and not lost:
            lost.append(True)
            board.requests.append(bytes(data[1:]))
            return len(data)
        return write(data)
    monkeypatch.setattr(board, 'write', lose_one_request)
    with pytest.raises(hl.HostLightingTimeoutError):
        session.configure(Settings(keepalive_ms=900))
    assert board.keepalive_ms == started.keepalive_ms
    session.poll()
    assert board.keepalive_ms == 900


def test_a_claiming_host_with_events_reads_page_1_too(board):
    """Test that a change of the state token with a lost event is learned from page 1 within a second."""
    session = session_on(board).start()
    board.subscribed = 0                    # the event of the change is lost
    board.reconfigure(lights=board.lights[:1])
    board.sleep(1.1)
    update = session.poll()
    assert hl.PAGE_LIGHTS in update.pages_read and update.restage


def test_a_start_with_its_claim_reply_lost_gives_the_claim_up(board, monkeypatch):
    """Test that a with block with its CLAIM reply lost leaves the board unclaimed for another application."""
    lose_replies(board, monkeypatch, lambda data: data[1] == hl.CMD_CLAIM)
    with pytest.raises(hl.HostLightingTimeoutError):
        with session_on(board):
            pass
    assert board.holder_token == 0 and board.requests[-1][3] == hl.CLAIM_GIVE_UP


@pytest.mark.parametrize('started', [Settings(), Settings(keepalive_ms=5000)])
def test_a_configure_with_its_reply_lost_leaves_a_later_host_s_settings(board, monkeypatch, started):
    """Test that settings another host sends after a CONFIGURE without a reply stay, as after a reply."""
    first = session_on(board, None, settings=started).start()
    lose_replies(board, monkeypatch, lambda data: data[1] == hl.CMD_CONFIGURE)
    with pytest.raises(hl.HostLightingTimeoutError):
        first.configure(Settings(keepalive_ms=1200))
    second = session_on(board, None, settings=Settings(takeover=hl.TAKEOVER_OVERLAY, keepalive_ms=3000)).start()
    for _ in range(15):
        board.sleep(0.1)
        first.poll()
        second.poll()
    assert (board.overlay, board.keepalive_ms) == (1, 3000)
    first.close()
    assert (board.overlay, board.keepalive_ms) == (1, 3000)


def test_page_1_is_read_at_the_next_poll_after_a_refusal(board, monkeypatch):
    """Test that a page 1 read refused with status 4 has the next poll() read it again, without waiting."""
    session = session_on(board, settings=Settings(keepalive_ms=9000)).start()   # no renewal due before page 1
    monkeypatch.setattr(session.device, 'take_events', lambda: [])     # the change not yet seen
    board.take_over()
    board.sleep(1.05)
    assert session.poll().claim.kind is ClaimChangeKind.LOST            # page 1 refused, then the renewal
    mark = len(board.requests)
    session.poll()
    assert [request[3] for request in board.requests[mark:] if request[0] == hl.CMD_GET_PAGE] == [hl.PAGE_STATE]


@pytest.mark.parametrize('events', [True, False])
def test_a_configure_with_its_page_1_read_lost_leaves_a_later_host_s_settings(board, monkeypatch, events):
    """Test that a configure() with an unanswered page 1 read sends nothing over settings another host sent since."""
    if not events:
        without_events(board, monkeypatch)
    first = session_on(board, None).start()
    lose_replies(board, monkeypatch, lambda data: data[1] == hl.CMD_GET_PAGE)
    with pytest.raises(hl.HostLightingTimeoutError):
        first.configure(Settings(keepalive_ms=1200))
    assert board.keepalive_ms == 2000
    second = session_on(board, None, settings=Settings(takeover=hl.TAKEOVER_OVERLAY, keepalive_ms=3000)).start()
    for _ in range(15):
        board.sleep(0.1)
        first.poll()
        second.poll()
    assert (board.overlay, board.keepalive_ms) == (1, 3000)
    first.close()
    assert (board.overlay, board.keepalive_ms) == (1, 3000)


def test_settings_seen_replaced_are_not_taken_back_for_the_same_values(board):
    """Test that close() leaves another host's settings of the same values once page 1 showed its own replaced."""
    first = session_on(board, None, settings=Settings(keepalive_ms=900)).start()
    session_on(board, None, settings=Settings(takeover=hl.TAKEOVER_OVERLAY, keepalive_ms=3000)).start()
    board.sleep(1.1)
    first.poll()                            # page 1 shows the other host's settings
    fake_device(board).configure(hl.TAKEOVER_WHOLE_FRAME, 900)     # a third host sends the same values
    first.close()
    assert (board.overlay, board.keepalive_ms) == (0, 900)


def test_page_1_is_read_at_the_next_poll_after_a_lost_read(board, monkeypatch):
    """Test that a page 1 read without a reply has the next poll() read page 1 again, without waiting."""
    session = session_on(board, settings=Settings(keepalive_ms=9000)).start()
    board.sleep(1.05)
    lose_replies(board, monkeypatch, lambda data: data[1] == hl.CMD_GET_PAGE)
    session.poll()
    mark = len(board.requests)
    session.poll()
    assert [request[3] for request in board.requests[mark:] if request[0] == hl.CMD_GET_PAGE] == [hl.PAGE_STATE]


def test_a_change_of_holder_learned_from_events_reads_page_1_before_configure(board):
    """Test that a poll sends CONFIGURE only after reading page 1, though events showed the board unclaimed."""
    session = session_on(board, None, settings=Settings(keepalive_ms=900)).start()
    board.take_over()
    board.let_go()                          # the events show a change of holder, the settings back to the defaults
    session.device.drain()                  # received now, so they carry that state
    board.subscribed = 0
    board.take_over()                       # a claim with its event lost
    mark = len(board.requests)
    session.poll()
    assert hl.CMD_CONFIGURE not in commands(board, mark) and board.keepalive_ms == 2000


@pytest.mark.parametrize('events', [True, False])
def test_a_configure_with_its_page_1_read_lost_is_sent_after_a_change_of_holder(board, monkeypatch, events):
    """Test that settings kept after a lost page 1 read go out once a claim held at the call has ended."""
    if not events:
        without_events(board, monkeypatch)
    session = session_on(board, None).start()
    board.take_over()
    board.sleep(1.1)
    session.poll()                          # page 1 shows the holder
    lose_replies(board, monkeypatch, lambda data: data[1] == hl.CMD_GET_PAGE)
    with pytest.raises(hl.HostLightingTimeoutError):
        session.configure(Settings(keepalive_ms=1200))
    board.let_go()
    for _ in range(15):
        board.sleep(0.1)
        session.poll()
    assert board.keepalive_ms == 1200


def test_the_same_values_after_a_change_of_holder_are_not_taken_for_its_own(board):
    """Test that a host leaves another's later change when both sent the same values after a change of holder."""
    same = Settings(takeover=hl.TAKEOVER_OVERLAY, keepalive_ms=3000)
    first = session_on(board, None, settings=same).start()
    second = session_on(board, None, settings=same).start()
    board.take_over()
    board.let_go()                          # the settings back to the defaults
    board.sleep(1.1)
    first.poll()                            # sends the same values first
    board.sleep(0.1)
    second.poll()                           # sees them under the new generation, and sends nothing
    first.configure(Settings(keepalive_ms=900))
    for _ in range(15):
        board.sleep(0.1)
        first.poll()
        second.poll()
    assert (board.overlay, board.keepalive_ms) == (0, 900)


def test_settings_sent_again_after_being_replaced_are_put_back_at_close(board):
    """Test that settings replaced, then sent again after a change of holder, are its own again for close()."""
    first = session_on(board, None, settings=Settings(keepalive_ms=900)).start()
    session_on(board, None, settings=Settings(takeover=hl.TAKEOVER_OVERLAY, keepalive_ms=3000)).start()
    board.sleep(1.1)
    first.poll()                            # page 1 shows the other host's settings
    board.take_over()
    board.let_go()
    for _ in range(15):
        board.sleep(0.1)
        first.poll()                        # sends its own again after the change of holder
    assert (board.overlay, board.keepalive_ms) == (0, 900)
    first.close()
    assert (board.overlay, board.keepalive_ms) == (0, 2000)


@pytest.mark.parametrize('events', [True, False])
def test_a_claim_that_ends_under_the_generation_of_its_settings_has_them_sent(board, monkeypatch, events):
    """Test that settings go out again when a claim ends under the generation they were sent under, seen held."""
    if not events:
        without_events(board, monkeypatch)
    session = session_on(board, None, settings=Settings(takeover=hl.TAKEOVER_OVERLAY, keepalive_ms=1200)).start()
    sent_under = board.generation
    for token in range(0xB2, 0xB8):
        board.take_over(token, b'Other')    # six changes of holder, each seen held
        board.sleep(1.1)
        session.poll()
    board.let_go()
    assert board.generation == sent_under
    for _ in range(15):
        board.sleep(0.1)
        session.poll()
    assert (board.overlay, board.keepalive_ms) == (1, 1200)


@pytest.mark.parametrize('events', [True, False])
def test_settings_configured_after_the_defaults_are_sent_when_the_claim_ends(board, monkeypatch, events):
    """Test that settings configured while a claim is held go out after it, though the defaults were in force."""
    if not events:
        without_events(board, monkeypatch)
    session = session_on(board, None, settings=Settings(takeover=hl.TAKEOVER_OVERLAY, keepalive_ms=1200)).start()
    sent_under = board.generation
    board.take_over()
    board.sleep(1.1)
    session.poll()
    session.configure(Settings())
    for _ in range(2):
        board.let_go()
        board.sleep(1.1)
        session.poll()
        board.take_over()
        board.sleep(1.1)
        session.poll()
    board.take_over(0xB3, b'Third')
    board.sleep(1.1)
    session.poll()
    session.configure(Settings(keepalive_ms=900))
    board.let_go()
    assert board.generation == sent_under
    for _ in range(15):
        board.sleep(0.1)
        session.poll()
    assert (board.overlay, board.keepalive_ms) == (0, 900)


@pytest.mark.parametrize('events', [True, False])
def test_defaults_seen_under_another_generation_leave_a_later_host_s_same_values(board, monkeypatch, events):
    """Test that a host with defaults page 1 showed under a new generation leaves its old values another host sent."""
    if not events:
        without_events(board, monkeypatch)
    same = Settings(takeover=hl.TAKEOVER_OVERLAY, keepalive_ms=3000)
    first = session_on(board, None, settings=same).start()
    board.take_over()
    lose_replies(board, monkeypatch, lambda data: data[1] == hl.CMD_GET_PAGE)
    with pytest.raises(hl.HostLightingTimeoutError):
        first.configure(Settings())
    board.let_go()
    board.sleep(1.1)
    first.poll()                            # the defaults, under a new generation
    for token in range(0xB2, 0xB6):
        board.take_over(token, b'Other')    # the claim changes hands unseen, back to the generation of its values
    board.let_go()
    session_on(board, None, settings=same).start()
    for _ in range(15):
        board.sleep(0.1)
        first.poll()
    assert (board.overlay, board.keepalive_ms) == (1, 3000)


def test_the_same_values_learned_from_an_event_are_not_taken_for_its_own(board):
    """Test that a host leaves another's later change when an event showed its values under a new generation."""
    same = Settings(takeover=hl.TAKEOVER_OVERLAY, keepalive_ms=3000)
    first = session_on(board, None, settings=same).start()
    second = session_on(board, None, settings=same).start()
    board.sleep(0.1)
    board.take_over()
    board.sleep(0.1)
    board.let_go()                          # the settings back to the defaults
    board.sleep(0.05)
    first.poll()                            # sends the same values first
    board.sleep(0.05)
    second.poll()                           # learns of them from the event, before its page 1 read is due
    first.configure(Settings(keepalive_ms=900))
    for _ in range(20):
        board.sleep(0.1)
        first.poll()
        second.poll()
    assert (board.overlay, board.keepalive_ms) == (0, 900)


def test_the_same_values_after_a_change_of_holder_stay_at_close(board):
    """Test that close() leaves another host's settings of its values, sent after a change of holder."""
    same = Settings(takeover=hl.TAKEOVER_OVERLAY, keepalive_ms=3000)
    first = session_on(board, None, settings=same).start()
    second = session_on(board, None, settings=same).start()
    board.take_over()
    board.let_go()
    board.sleep(1.1)
    first.poll()                            # sends the same values first
    board.sleep(0.1)
    second.poll()                           # sees them under the new generation
    second.close()
    assert (board.overlay, board.keepalive_ms) == (1, 3000)


@pytest.mark.parametrize('events', [True, False])
def test_settings_landed_after_an_unseen_change_of_holder_are_put_back_at_close(board, monkeypatch, events):
    """Test that close() puts back settings that landed after two changes of holder, the read after them lost."""
    if not events:
        without_events(board, monkeypatch)
    session = session_on(board, None).start()
    write, sent = board.write, []

    def claimed_between(data):
        if data[1] == hl.CMD_CONFIGURE and not sent:
            sent.append(True)
            board.take_over()
            board.let_go()                  # the CONFIGURE lands two generations on
        return write(data)
    monkeypatch.setattr(board, 'write', claimed_between)
    lose_replies(board, monkeypatch, lambda data: bool(sent) and data[1] == hl.CMD_GET_PAGE)
    session.configure(Settings(takeover=hl.TAKEOVER_OVERLAY, keepalive_ms=1200))
    for _ in range(15):
        board.sleep(0.1)
        session.poll()
    assert (board.overlay, board.keepalive_ms) == (1, 1200)
    session.close()
    assert (board.overlay, board.keepalive_ms) == (0, 2000)


def test_an_event_older_than_its_configure_leaves_its_settings_its_own(board):
    """Test that a page 1 event from before a host's CONFIGURE, taken after it, does not make the settings another's."""
    session = session_on(board, None).start()
    board.take_over()
    held = board._event()                   # page 1 while the claim is held
    board.let_go()
    board.sleep(1.1)
    session.poll()
    session.configure(Settings(takeover=hl.TAKEOVER_OVERLAY, keepalive_ms=1200))
    session.device.events.append(held)      # taken late, as if the CONFIGURE's own event was lost
    session.poll()
    board.sleep(1.1)
    session.poll()
    session.close()
    assert (board.overlay, board.keepalive_ms) == (0, 2000)


def test_its_own_settings_shown_are_not_read_again_at_every_poll(board):
    """Test that a host that does not claim, its settings shown, reads page 1 no more often than due."""
    session = session_on(board, None, settings=Settings(takeover=hl.TAKEOVER_OVERLAY, keepalive_ms=1200)).start()
    mark = len(board.requests)
    for _ in range(20):
        board.sleep(0.1)
        session.poll()
    assert commands(board, mark).count(hl.CMD_GET_PAGE) <= 5     # due every third of the timeout


def test_a_configure_without_a_reply_is_sent_again_after_a_change_of_holder(board, monkeypatch):
    """Test that a CONFIGURE with its reply lost is sent again once a change of holder has put the defaults back."""
    session = session_on(board, None).start()
    lose_replies(board, monkeypatch, lambda data: data[1] == hl.CMD_CONFIGURE)
    with pytest.raises(hl.HostLightingTimeoutError):
        session.configure(Settings(takeover=hl.TAKEOVER_OVERLAY, keepalive_ms=1200))
    board.take_over()
    board.let_go()
    for _ in range(15):
        board.sleep(0.1)
        session.poll()
    assert (board.overlay, board.keepalive_ms) == (1, 1200)


def test_settings_landed_unseen_are_its_own_until_the_next_read_only(board, monkeypatch):
    """Test that a host takes its settings landed unseen as its own at the next read only, not at later ones."""
    same = Settings(takeover=hl.TAKEOVER_OVERLAY, keepalive_ms=1200)
    first = session_on(board, None).start()
    sent = []
    lose_replies(board, monkeypatch, lambda data: bool(sent) and data[1] == hl.CMD_GET_PAGE)
    write = board.write

    def configure_seen(data):
        if data[1] == hl.CMD_CONFIGURE:
            sent.append(True)
        return write(data)
    monkeypatch.setattr(board, 'write', configure_seen)
    first.configure(same)                   # answered, the read after it lost
    board.sleep(1.1)
    first.poll()                            # the next read shows them under the same generation
    board.take_over()
    board.let_go()
    session_on(board, None, settings=same).start()      # another host sends the same values first
    board.sleep(1.1)
    first.poll()
    first.close()
    assert (board.overlay, board.keepalive_ms) == (1, 1200)


@pytest.mark.parametrize('second', [Settings(takeover=hl.TAKEOVER_OVERLAY, keepalive_ms=1200),
                                    Settings(keepalive_ms=900)])
def test_settings_landed_unseen_are_put_back_after_a_configure_with_its_read_lost(board, monkeypatch, second):
    """Test that close() puts back settings that landed unseen, though the next configure() lost its page 1 read."""
    session = session_on(board, None).start()
    write, reads, sent = board.write, [], []

    def counted(data):
        if data[1] == hl.CMD_CONFIGURE and not sent:
            sent.append(True)
            board.take_over()
            board.let_go()                  # the CONFIGURE lands two generations on
        if data[1] == hl.CMD_GET_PAGE and data[4] == hl.PAGE_STATE:
            reads.append(True)
        return write(data)
    monkeypatch.setattr(board, 'write', counted)
    # the read after the CONFIGURE, then the read of the next configure()
    lose_replies(board, monkeypatch, lambda data: data[1] == hl.CMD_GET_PAGE and len(reads) in (2, 3), count=2)
    session.configure(Settings(takeover=hl.TAKEOVER_OVERLAY, keepalive_ms=1200))
    with pytest.raises(hl.HostLightingTimeoutError):
        session.configure(second)
    for _ in range(15):
        board.sleep(0.1)
        session.poll()
    session.close()
    assert (board.overlay, board.keepalive_ms) == (0, 2000)


def test_settings_landed_unseen_are_sent_again_after_a_change_of_holder(board, monkeypatch):
    """Test that settings with the read after CONFIGURE lost go out again once a change of holder replaced them."""
    session = session_on(board, None).start()
    sent = []
    lose_replies(board, monkeypatch, lambda data: bool(sent) and data[1] == hl.CMD_GET_PAGE)
    write = board.write

    def configure_seen(data):
        if data[1] == hl.CMD_CONFIGURE:
            sent.append(True)
        return write(data)
    monkeypatch.setattr(board, 'write', configure_seen)
    session.configure(Settings(takeover=hl.TAKEOVER_OVERLAY, keepalive_ms=1200))   # the read after it lost
    board.take_over()
    board.let_go()                          # the defaults back, under a new generation
    for _ in range(15):
        board.sleep(0.1)
        session.poll()
    assert (board.overlay, board.keepalive_ms) == (1, 1200)
