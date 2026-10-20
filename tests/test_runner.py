"""Test SessionRunner, AsyncSession, BoardWatcher and SessionGroup on fake boards.

A fake board is changed only on the thread that uses it: through a hook, or the session's reopen.

SPDX-FileCopyrightText: © 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import asyncio
import queue
import threading
import time

import pytest

from gp2040ce_hostlighting import protocol as hl
from gp2040ce_hostlighting.claim import ClaimChange, ClaimChangeKind
from gp2040ce_hostlighting.frames import Frame
from gp2040ce_hostlighting.runner import AsyncSession, BoardWatcher, SessionGroup, SessionRunner
from gp2040ce_hostlighting.session import Session, SessionUpdate
from gp2040ce_hostlighting.testing import FakeBoard, ModelBoard, attach, fake_device

SECOND_ID = bytes.fromhex('0102030405060708')


def until(condition, seconds=5.0) -> bool:
    """Wait for a condition another thread makes true."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.005)
    return False


def frame_of(light, colour=(255, 0, 0)) -> Frame:
    """Build a frame that colours one light."""
    frame = Frame()
    frame.entries(hl.ADDRESSING_LIGHT, [(light, colour)])
    return frame


def once(flag: threading.Event, action):
    """Make a hook that runs an action at the first request after the flag is set."""
    def hook(board, request):
        if flag.is_set():
            flag.clear()
            action(board)
    return hook


def test_a_runner_shows_the_latest_frame_and_tells_of_a_takeover():
    """Test that the frame posted last is shown, and a takeover reaches the callback and the queue."""
    board = ModelBoard()
    seen, steal = [], threading.Event()
    board.hooks.append(once(steal, lambda on: on.take_over()))
    runner = SessionRunner(Session(fake_device(board), 'runner'), on_update=seen.append).start()
    try:
        runner.show(frame_of(0, (1, 0, 0)))
        runner.show(frame_of(0, (2, 0, 0)))
        assert until(lambda: bool(board.frames) and board.frames[-1].get(0) == (2, 0, 0))
        assert (1, 0, 0) not in [frame.get(0) for frame in board.frames]
        steal.set()
        assert until(lambda: any(update.claim.kind is ClaimChangeKind.LOST for update in seen))
        assert not runner.updates.empty()
    finally:
        runner.close()
    assert not runner.running and runner.error is None


def test_a_runner_refuses_a_run_past_the_limit_at_once():
    """Test that show() raises ValueError on the caller's thread for an LED run past the limit."""
    board = ModelBoard()
    with SessionRunner(Session(fake_device(board), 'runner')) as runner:
        frame = Frame()
        frame.run(hl.ADDRESSING_LED_RUN, 0, [(1, 2, 3)] * (board.led_limit() + 1))
        with pytest.raises(ValueError, match='addressable LED limit'):
            runner.show(frame)


def test_a_runner_sends_settings_posted():
    """Test that configure() sends the settings at the next poll."""
    board = ModelBoard()
    with SessionRunner(Session(fake_device(board), 'runner')) as runner:
        runner.configure(hl.Settings(takeover=hl.TAKEOVER_OVERLAY, keepalive_ms=900))
        assert until(lambda: board.keepalive_ms == 900 and board.overlay == hl.TAKEOVER_OVERLAY)


def test_a_runner_ends_when_the_board_leaves_without_reconnect():
    """Test that with reconnect off the runner ends, keeps the error and tells on_end."""
    board, ended, leave = ModelBoard(), [], threading.Event()
    board.hooks.append(once(leave, lambda on: on.unplug()))
    runner = SessionRunner(Session(fake_device(board), 'runner'), on_end=ended.append, reconnect=False).start()
    leave.set()
    assert until(lambda: bool(ended))
    assert isinstance(ended[0], hl.HostLightingDisconnectedError) and runner.error is ended[0]
    assert until(lambda: not runner.running)


def test_a_runner_finds_the_board_again_and_shows_the_frame_again():
    """Test that a board that left is looked for by its ID, and the latest frame is shown on it again."""
    board, leave, back = ModelBoard(), threading.Event(), threading.Event()
    board.hooks.append(once(leave, lambda on: on.unplug()))

    def reopen(board_id):
        if not back.is_set():
            return None
        board.replug()
        return fake_device(board)
    runner = SessionRunner(Session(fake_device(board), 'runner', reopen=reopen), reconnect_seconds=0.01).start()
    try:
        runner.show(frame_of(0))
        assert until(lambda: bool(board.frames))
        shown = len(board.frames)
        leave.set()
        assert until(lambda: board.departed)
        back.set()
        assert until(lambda: len(board.frames) > shown and board.holder_token != 0)
    finally:
        runner.close()
    assert runner.error is None


def test_a_session_that_does_not_start_raises_from_start():
    """Test that start() raises the session's own error."""
    board = ModelBoard()
    board.take_over(exclusive=True)
    runner = SessionRunner(Session(fake_device(board), 'runner'))
    with pytest.raises(hl.HostLightingClaimedError):
        runner.start()
    assert not runner.running
    with pytest.raises(hl.HostLightingError, match='already started'):
        runner.start()


def test_an_async_session_starts_shows_updates_and_ends():
    """Test AsyncSession: start, show, an update awaited, async for, then the end once closed."""
    board = ModelBoard()

    async def main():
        async with AsyncSession(Session(fake_device(board), 'async')) as session:
            session.show(frame_of(0))
            update = await asyncio.wait_for(session.update(), 5)
            assert 'holder_token' in update.changed
            session.configure(hl.Settings(keepalive_ms=800))
            async for update in session:
                if update.board_state.settings.keepalive_ms == 800:
                    break
        with pytest.raises(hl.HostLightingError, match='ended'):
            await session.update()
    asyncio.run(main())
    assert board.holder_token == 0 and board.frames


def test_an_async_session_not_started_and_one_that_fails():
    """Test update() before start(), and a start that fails."""
    board = ModelBoard()
    board.take_over(exclusive=True)

    async def main():
        session = AsyncSession(Session(fake_device(board), 'async'))
        with pytest.raises(hl.HostLightingError, match='not started'):
            await session.update()
        with pytest.raises(hl.HostLightingClaimedError):
            await session.start()
        async for _ in session:
            raise AssertionError('no update after a failed start')
    asyncio.run(main())


def test_the_watcher_reports_boards_arriving_and_leaving(monkeypatch):
    """Test arrivals and departures by board ID, each interface opened once."""
    first, second = FakeBoard(), FakeBoard()
    second.board_id = SECOND_ID
    attach(monkeypatch, first, second)
    ids = {first.board_id.hex().upper(), SECOND_ID.hex().upper()}
    watcher = BoardWatcher()
    assert watcher.poll() == (frozenset(ids), frozenset())
    hellos = len(first.sent(hl.CMD_HELLO))
    assert watcher.poll() == (frozenset(), frozenset()) and len(first.sent(hl.CMD_HELLO)) == hellos
    second.unplug()
    assert watcher.poll() == (frozenset(), frozenset({SECOND_ID.hex().upper()}))
    second.replug()
    assert watcher.poll() == (frozenset({SECOND_ID.hex().upper()}), frozenset())
    watcher.forget(SECOND_ID.hex().upper())
    assert watcher.poll() == (frozenset({SECOND_ID.hex().upper()}), frozenset())


def test_a_group_runs_a_session_per_board(monkeypatch):
    """Test a runner per board, a frame to one, updates by board ID, and a board that leaves dropped."""
    first, second = ModelBoard(), ModelBoard()
    second.board_id = SECOND_ID
    attach(monkeypatch, first, second)
    first_id, second_id = first.board_id.hex().upper(), SECOND_ID.hex().upper()
    seen, leave = set(), threading.Event()
    second.hooks.append(once(leave, lambda on: on.unplug()))
    group = SessionGroup(lambda device: Session(device, 'group'), on_update=lambda board_id, update: seen.add(board_id))
    try:
        assert group.poll() == (frozenset({first_id, second_id}), frozenset())
        group.show(second_id, frame_of(0))
        assert until(lambda: bool(second.frames)) and not first.frames
        assert until(lambda: seen == {first_id, second_id})
        leave.set()
        assert until(lambda: not group.runners[second_id].running)
        group.poll()
        assert set(group.runners) == {first_id}
        with pytest.raises(KeyError):
            group.show(second_id, frame_of(0))
    finally:
        group.close()
    assert until(lambda: first.holder_token == 0) and not group.runners


class ScriptedWatcher(BoardWatcher):
    """A watcher with polls that return what the test scripts, and that records what is forgotten."""

    def __init__(self, *polls):
        """Hold the polls' results, in order."""
        super().__init__()
        self.polls, self.forgotten = list(polls), []

    def poll(self):
        """Return the next scripted result, then nothing."""
        return self.polls.pop(0) if self.polls else (frozenset(), frozenset())

    def forget(self, board_id):
        """Record the board forgotten."""
        self.forgotten.append(board_id)


def test_a_group_closes_a_runner_when_its_board_left_and_skips_what_will_not_start(monkeypatch):
    """Test a departure closing its runner, an ID not on the bus, and a session that does not start."""
    good, held = ModelBoard(), ModelBoard()
    held.board_id = SECOND_ID
    held.take_over(exclusive=True)
    attach(monkeypatch, good, held)
    good_id, held_id, absent = good.board_id.hex().upper(), SECOND_ID.hex().upper(), 'FFFFFFFFFFFFFFFF'
    watcher = ScriptedWatcher((frozenset({good_id, held_id, absent}), frozenset()),
                              (frozenset(), frozenset({good_id})))
    group = SessionGroup(lambda device: Session(device, 'group'), watcher=watcher)
    try:
        assert group.poll() == (frozenset({good_id}), frozenset())
        assert sorted(watcher.forgotten) == sorted([held_id, absent])
        runner = group.runners[good_id]
        assert group.poll() == (frozenset(), frozenset({good_id}))
        assert not runner.running and not group.runners
    finally:
        group.close()


def test_a_runners_queue_keeps_the_latest_updates():
    """Test that a full queue drops its oldest update, and an update that carries nothing is not queued."""
    board = ModelBoard()
    runner = SessionRunner(Session(fake_device(board), 'runner'))
    runner.updates = queue.Queue(2)
    quiet = SessionUpdate(claim=ClaimChange(kind=ClaimChangeKind.UNCHANGED), board_state=None)
    runner._tell(quiet)
    assert runner.updates.empty()
    told = [SessionUpdate(claim=quiet.claim, board_state=None, changed=frozenset({name}))
            for name in ('a', 'b', 'c')]
    for update in told:
        runner._tell(update)
    assert [runner.updates.get_nowait(), runner.updates.get_nowait()] == told[1:]
