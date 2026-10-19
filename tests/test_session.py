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
        assert board.holder == session.claim.token and board.keepalive_ms == 900
        assert session.board_id == testing.BOARD_ID.hex().upper() and len(session.cache.lights) == 2
        assert session.board_state.controller.holder_token == session.claim.token
        assert session.claim.state is ClaimState.HELD
    assert board.holder == 0 and board.requests[-1][3] == hl.CLAIM_GIVE_UP


def test_start_checks_what_the_session_and_the_host_need(board):
    """Test that a capability missing from HELLO refuses the start before anything is claimed."""
    session = session_on(board, require=Requirements(addressing_modes=frozenset({9})))
    with pytest.raises(hl.HostLightingIncompatibleError, match=r'addressing mode 9'):
        session.start()
    assert not board.sent(hl.CMD_CLAIM)
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
    assert board.holder == 0 and board.requests[-1][3] == hl.CLAIM_GIVE_UP


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


def test_a_claim_taken_over_and_given_back_is_taken_again(board):
    """Test RETAKEN after events show another holder, with the settings sent again and a restage."""
    session = session_on(board, settings=Settings(keepalive_ms=600)).start()
    session.show(frame_of(0))
    board.take_over()
    board.let_go()
    update = session.poll()
    assert update.claim.kind is ClaimChangeKind.RETAKEN and update.restage
    assert board.holder == session.claim.token and board.keepalive_ms == 600


def test_a_lost_claim_stops_the_frames(board, caplog):
    """Test LOST, a frame not sent while the claim is lost, and the give up that sends nothing."""
    session = session_on(board).start()
    board.take_over()
    with caplog.at_level(logging.INFO, logger='gp2040ce_hostlighting.claim'):
        update = session.poll()
    assert update.claim.kind is ClaimChangeKind.LOST and update.claim.holder.application_name == 'Other'
    sent = len(board.requests)
    assert session.show(frame_of(0)) is None
    session.close()
    assert board.holder == 0xB2 and hl.CMD_CLAIM not in commands(board, sent)


def test_a_refused_frame_goes_to_the_claim(board, monkeypatch):
    """Test a status 4 frame refusal: no events applied first, the claim lost, and the next poll saying so."""
    session = session_on(board).start()
    monkeypatch.setattr(session.device, 'take_events', lambda: [])     # the change not yet seen
    board.take_over()
    assert session.show(frame_of(0)) == FrameResult(acknowledged=False, resent=False)
    assert session.claim.state is ClaimState.LOST
    assert session.poll().claim.kind is ClaimChangeKind.LOST
    board.disconnect_on = {hl.CMD_STAGE}
    session.claim.state = ClaimState.HELD
    with pytest.raises(hl.HostLightingDisconnectedError):
        session.show(frame_of(0))


def test_a_changed_configuration_is_read_again(board):
    """Test that a token change reads the changed pages again and asks for a restage when Lights changed."""
    session = session_on(board).start()
    board.reconfigure(lights=[light_record(0, pin=2, action=9, dup=1, instance=1, instances=2, x=1, y=0),
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
    assert session.poll().claim.kind is ClaimChangeKind.TAKEN and board.holder == token
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
    """Test a refresh unanswered, refused with status 4, or changing under the reads, then one that lands."""
    session = session_on(board).start()
    board.reconfigure(lights=[light_record(0, pin=2, action=9, dup=1, instance=1, instances=2, x=1, y=0),
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
    update = session.poll()
    assert update.claim.kind is ClaimChangeKind.LOST and update.pages_read == frozenset()
    session.claim.state = ClaimState.HELD
    assert hl.PAGE_LIGHTS in session.poll().pages_read
    board.reconfigure(lights=board.lights[:1])
    failures.append(hl.HostLightingDisconnectedError('gone'))
    with pytest.raises(hl.HostLightingDisconnectedError):
        session.poll()


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
    frame = Frame()
    frame.run(hl.ADDRESSING_LED_RUN, 200, [(1, 2, 3)])
    board.let_go()
    session.claim.take()
    with pytest.raises(hl.HostLightingRejectedError, match='INVALID_ARG'):
        session.show(frame)


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
    assert board.frames[-1] == {1: (0, 0, 255), 2: (0, 0, 255), 3: (0, 0, 255)} and board.holder == 0
    assert board.now >= 3.0
