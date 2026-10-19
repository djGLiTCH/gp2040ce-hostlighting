"""Test Frame and publish_frame().

SPDX-FileCopyrightText: © 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import pytest

from gp2040ce_hostlighting import protocol as hl
from gp2040ce_hostlighting.frames import Frame, FrameResult, publish_frame
from gp2040ce_hostlighting.testing import fake_device


def test_a_frame_stages_in_the_order_added_joining_neighbours():
    """Test the order kept, entries joined with their neighbours, a run joined onto the run it continues."""
    frame = Frame()
    frame.entries(hl.ADDRESSING_KIND, [(hl.KIND_CASE, (0, 0, 64))])
    frame.entries(hl.ADDRESSING_ACTION, [(5, (255, 0, 0))])
    frame.entries(hl.ADDRESSING_ACTION, [(6, (0, 255, 0, 9))])       # a white byte: RGBW, a part of its own
    frame.entries(hl.ADDRESSING_ACTION, iter([(7, (0, 0, 255, 0))]))
    frame.entries(hl.ADDRESSING_KIND, [])
    frame.run(hl.ADDRESSING_LED_RUN, 0, [(1, 2, 3)] * 2)
    frame.run(hl.ADDRESSING_LED_RUN, 2, [(4, 5, 6)])
    frame.run(hl.ADDRESSING_LED_RUN, 9, [(7, 8, 9)], pixel_format=hl.PIXEL_RGB565)
    frame.run(hl.ADDRESSING_LIGHT_RUN, 1, [(1, 1, 1)])
    assert frame.payloads() == (
        hl.stage_entry_payloads(hl.ADDRESSING_KIND, [(hl.KIND_CASE, (0, 0, 64))])
        + hl.stage_entry_payloads(hl.ADDRESSING_ACTION, [(5, (255, 0, 0))])
        + hl.stage_entry_payloads(hl.ADDRESSING_ACTION, [(6, (0, 255, 0, 9)), (7, (0, 0, 255, 0))], hl.PIXEL_RGBW)
        + hl.stage_run_payloads(hl.ADDRESSING_LED_RUN, 0, [(1, 2, 3), (1, 2, 3), (4, 5, 6)])
        + hl.stage_run_payloads(hl.ADDRESSING_LED_RUN, 9, [(7, 8, 9)], hl.PIXEL_RGB565)
        + hl.stage_run_payloads(hl.ADDRESSING_LIGHT_RUN, 1, [(1, 1, 1)]))
    assert frame.addresses == {(hl.ADDRESSING_KIND, hl.KIND_CASE), (hl.ADDRESSING_ACTION, 5),
                               (hl.ADDRESSING_ACTION, 6), (hl.ADDRESSING_ACTION, 7), (hl.ADDRESSING_LED_RUN, 0),
                               (hl.ADDRESSING_LED_RUN, 1), (hl.ADDRESSING_LED_RUN, 2), (hl.ADDRESSING_LED_RUN, 9),
                               (hl.ADDRESSING_LIGHT_RUN, 1)}


def test_frames_compare_by_what_they_stage_and_refuse_wrong_calls():
    """Test equality, the representation, and the refusal of a wrong addressing, no colours or a wrong format."""
    first, second = Frame(), Frame()
    assert first == second and first != 'frame' and Frame().payloads() == []
    first.entries(hl.ADDRESSING_PIN, [(2, (1, 2, 3))])
    assert first != second and 'Frame([(5, 0, None, ((2, (1, 2, 3)),))])' == repr(first)
    second.entries(hl.ADDRESSING_PIN, [(2, (1, 2, 3))])
    assert first == second
    with pytest.raises(TypeError):
        hash(first)
    for call in (lambda: first.entries(hl.ADDRESSING_LED_RUN, [(0, (1, 2, 3))]),
                 lambda: first.run(hl.ADDRESSING_LIGHT, 0, [(1, 2, 3)]),
                 lambda: first.run(hl.ADDRESSING_LED_RUN, 0, []),
                 lambda: first.entries(hl.ADDRESSING_PIN, [(2, (1, 2, 3))], pixel_format=7)):
        with pytest.raises(ValueError):
            call()


def test_publish_frame_sends_one_answered_report(hlp_model_board):
    """Test that every report but the last goes with NO_REPLY, the last with COMMIT_AFTER, and the frame shows."""
    device = fake_device(hlp_model_board)
    frame = Frame()
    frame.run(hl.ADDRESSING_LED_RUN, 0, [(10, 20, 30)] * 40)
    frame.entries(hl.ADDRESSING_LIGHT, [(0, (255, 0, 0))])
    assert publish_frame(device, frame) == FrameResult(acknowledged=True, resent=False)
    stages = hlp_model_board.sent(hl.CMD_STAGE)
    assert [request[2] for request in stages] == [hl.FLAG_NO_REPLY] * 3 + [hl.FLAG_COMMIT_AFTER]
    assert hlp_model_board.frames[-1][0] == (255, 0, 0) and hlp_model_board.frames[-1][39] == (10, 20, 30)
    assert not hlp_model_board.sent(hl.CMD_CLEAR)


def test_publish_frame_clears_first_or_for_an_empty_frame(hlp_model_board):
    """Test CLEAR before the frame with clear_first, and CLEAR alone carrying the commit for an empty frame."""
    device = fake_device(hlp_model_board)
    frame = Frame()
    frame.entries(hl.ADDRESSING_LIGHT, [(1, (0, 0, 255))])
    assert publish_frame(device, frame, clear_first=True).acknowledged
    assert [(request[0], request[2]) for request in hlp_model_board.requests[-2:]] == [
        (hl.CMD_CLEAR, hl.FLAG_NO_REPLY), (hl.CMD_STAGE, hl.FLAG_COMMIT_AFTER)]
    assert publish_frame(device, Frame()).acknowledged
    assert (hlp_model_board.requests[-1][0], hlp_model_board.requests[-1][2]) == (hl.CMD_CLEAR, hl.FLAG_COMMIT_AFTER)
    assert hlp_model_board.frames[-1] == {}


def test_publish_frame_sends_the_last_report_once_more(hlp_model_board):
    """Test the resend after a missing acknowledgement, and a frame not shown when the resend goes unanswered too."""
    device = fake_device(hlp_model_board)
    answer, dropped = hlp_model_board.answer, []

    def dropping(command, sequence, request):
        if command == hl.CMD_STAGE and request[2] & hl.FLAG_COMMIT_AFTER and len(dropped) < limit[0]:
            dropped.append(sequence)
            return None
        return answer(command, sequence, request)
    hlp_model_board.answer = dropping
    frame = Frame()
    frame.entries(hl.ADDRESSING_LIGHT, [(0, (1, 2, 3))])
    limit = [1]
    assert publish_frame(device, frame) == FrameResult(acknowledged=True, resent=True)
    limit = [3]
    result = publish_frame(device, frame)
    assert result == FrameResult(acknowledged=False, resent=True) and not result and result.refused is None
    assert len(hlp_model_board.sent(hl.CMD_STAGE)) == 4


def test_publish_frame_raises_a_refusal_and_refuses_a_short_timeout(hlp_model_board):
    """Test that a refused last report raises, and a timeout under 250 ms is refused before anything is sent."""
    device = fake_device(hlp_model_board)
    frame = Frame()
    frame.run(hl.ADDRESSING_LED_RUN, 200, [(1, 2, 3)])               # past the addressable LED limit
    with pytest.raises(hl.HostLightingRejectedError, match='INVALID_ARG at request byte 4'):
        publish_frame(device, frame)
    sent = len(hlp_model_board.requests)
    with pytest.raises(ValueError, match="under the specification's 250 ms"):
        publish_frame(device, frame, timeout=0.2)
    assert len(hlp_model_board.requests) == sent
