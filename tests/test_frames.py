# gp2040ce-hostlighting (GP2040-CE Host Lighting library and tools)
# Filename: test_frames.py
# Folder: tests
# https://github.com/djGLiTCH/gp2040ce-hostlighting
# SPDX-FileCopyrightText: (C) 2026 Jacob Simpson (DJ GLiTCH)
# SPDX-License-Identifier: GPL-3.0-or-later
# See LICENSES/GPL-3.0-or-later.txt at the repository root.

"""Test Frame and publish_frame()."""
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


def test_an_led_run_past_the_limit_is_refused_before_anything_is_sent(hlp_model_board):
    """Test check_led_limit() and publish_frame(led_limit=): the run that reaches past it, nothing sent."""
    frame = Frame()
    frame.run(hl.ADDRESSING_LED_RUN, 98, [(1, 2, 3)] * 2)
    frame.run(hl.ADDRESSING_LIGHT_RUN, 98, [(1, 2, 3)] * 5)             # a light run past the lights is skipped
    frame.check_led_limit(100)
    frame.run(hl.ADDRESSING_LED_RUN, 99, [(1, 2, 3)] * 2)
    with pytest.raises(ValueError, match="an LED run of 2 from 99 reaches past the board's addressable LED limit "
                                         'of 100'):
        frame.check_led_limit(100)
    with pytest.raises(ValueError, match='limit of 100'):
        publish_frame(fake_device(hlp_model_board), frame, led_limit=100)
    assert not hlp_model_board.sent(hl.CMD_STAGE) and not hlp_model_board.sent(hl.CMD_CLEAR)


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


def test_a_frame_refuses_an_address_when_it_is_added():
    """Test that entries() and run() check addresses as they are added, not at publish."""
    frame = Frame()
    with pytest.raises(ValueError, match='light address 70000 is outside'):
        frame.entries(hl.ADDRESSING_LIGHT, [(0, (1, 2, 3)), (70000, (1, 2, 3))])
    with pytest.raises(ValueError, match='passes index'):
        frame.run(hl.ADDRESSING_LED_RUN, 0xFFFF, [(1, 2, 3)] * 2)
    assert frame == Frame()


def test_a_frame_refuses_a_colour_when_it_is_added():
    """Test that entries() and run() take only colours of three or four integer channels of 0-255."""
    frame = Frame()
    for colour in ((300, 0, 0), (1, 2), (1, 2, 3, 4, 5), (-1, 0, 0), (51.0, 255.0, 0.0)):
        with pytest.raises(ValueError, match='not three or four integer channels of 0-255'):
            frame.entries(hl.ADDRESSING_LIGHT, [(0, colour)])
        with pytest.raises(ValueError, match='not three or four integer channels of 0-255'):
            frame.run(hl.ADDRESSING_LED_RUN, 0, [(1, 2, 3), colour])
    assert frame == Frame()


def test_a_frame_takes_integer_channels_of_any_integer_type():
    """Test that channels such as IntEnum values, which operator.index() takes, are accepted, and floats are not."""
    import enum

    class Level(enum.IntEnum):
        FULL = 255
    frame = Frame()
    frame.entries(hl.ADDRESSING_LIGHT, [(0, (Level.FULL, 0, True))])
    with pytest.raises(ValueError, match='integer channels'):
        frame.run(hl.ADDRESSING_LED_RUN, 0, [(1.0, 2, 3)])
    assert frame.payloads()
