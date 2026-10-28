# gp2040ce-hostlighting (GP2040-CE Host Lighting library and tools)
# Filename: test_runner.py
# Folder: tests
# https://github.com/djGLiTCH/gp2040ce-hostlighting
# SPDX-FileCopyrightText: © 2026 Jacob Simpson (DJ GLiTCH)
# SPDX-License-Identifier: GPL-3.0-or-later
# See LICENSES/GPL-3.0-or-later.txt at the repository root.

"""Test SessionRunner, AsyncSession, BoardWatcher and SessionGroup on fake boards.

A fake board is changed only on the thread that uses it: through a hook, or the session's reopen.
"""
import asyncio
import logging
import queue
import threading
import time
import types

import pytest

from gp2040ce_hostlighting import protocol as hl
from gp2040ce_hostlighting import runner as runner_module
from gp2040ce_hostlighting.claim import ClaimChange, ClaimChangeKind
from gp2040ce_hostlighting.frames import Frame
from gp2040ce_hostlighting.runner import PROBE_TRIES, AsyncSession, BoardWatcher, SessionGroup, SessionRunner
from gp2040ce_hostlighting.session import Session, SessionUpdate
from gp2040ce_hostlighting.testing import FakeBoard, ModelBoard, attach, fake_device, summary_record

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
        with pytest.raises(hl.HostLightingClaimedError):
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
    """Test a departure closing its runner, an ID not on the bus, and a session that does not start.

    A board that failed to start a session is forgotten, to be tried again, only once retry_seconds have passed.
    """
    good, held = ModelBoard(), ModelBoard()
    held.board_id = SECOND_ID
    held.take_over(exclusive=True)
    attach(monkeypatch, good, held)
    good_id, held_id, absent = good.board_id.hex().upper(), SECOND_ID.hex().upper(), 'FFFFFFFFFFFFFFFF'
    watcher = ScriptedWatcher((frozenset({good_id, held_id, absent}), frozenset()),
                              (frozenset(), frozenset({good_id})))
    group = SessionGroup(lambda device: Session(device, 'group'), watcher=watcher, retry_seconds=0)
    try:
        assert group.poll() == (frozenset({good_id}), frozenset())
        assert watcher.forgotten == [absent]
        runner = group.runners[good_id]
        assert group.poll() == (frozenset(), frozenset({good_id}))
        assert watcher.forgotten == [absent, held_id]
        assert not runner.running and not group.runners
    finally:
        group.close()


def at_stage(flag: threading.Event, action):
    """Make a hook that runs an action at the first STAGE after the flag is set."""
    def hook(board, request):
        if flag.is_set() and request[0] == hl.CMD_STAGE:
            flag.clear()
            action(board)
    return hook


def test_a_frame_with_its_acknowledgement_lost_is_sent_again():
    """Test that a show neither acknowledged nor refused leaves the frame pending, sent again at the next poll."""
    board, lose, answer = FakeBoard(), threading.Event(), threading.Event()
    board.hooks.append(once(lose, lambda on: on.drop.add(hl.CMD_STAGE)))
    board.hooks.append(once(answer, lambda on: on.drop.discard(hl.CMD_STAGE)))
    runner = SessionRunner(Session(fake_device(board), 'runner')).start()
    try:
        runner.show(frame_of(0, (9, 0, 0)))
        assert until(lambda: bool(board.frames) and board.frames[-1].get(0) == (9, 0, 0))
        lose.set()
        runner.show(frame_of(0, (0, 9, 0)))
        assert until(lambda: runner.result is not None and not runner.result.acknowledged)
        answer.set()
        assert until(lambda: board.frames[-1].get(0) == (0, 9, 0))
    finally:
        runner.close()
    assert runner.error is None


def test_a_frame_posted_as_the_board_leaves_is_shown_after_the_reconnect():
    """Test that the frame shown at the departure, not the one before it, is shown on the board's return."""
    board, leave, back = ModelBoard(), threading.Event(), threading.Event()
    board.hooks.append(at_stage(leave, lambda on: (on.unplug(), on.disconnect_on.add(hl.CMD_STAGE))))

    def reopen(board_id):
        if not back.is_set():
            return None
        board.disconnect_on.clear()
        board.replug()
        return fake_device(board)
    runner = SessionRunner(Session(fake_device(board), 'runner', reopen=reopen), reconnect_seconds=0.01).start()
    try:
        runner.show(frame_of(0, (9, 0, 0)))
        assert until(lambda: bool(board.frames) and board.frames[-1].get(0) == (9, 0, 0))
        leave.set()
        runner.show(frame_of(0, (0, 0, 9)))
        assert until(lambda: board.departed)
        back.set()
        assert until(lambda: board.frames[-1].get(0) == (0, 0, 9))
    finally:
        runner.close()
    assert runner.error is None


def test_start_raises_only_the_starts_own_error():
    """Test that an error after the session started stays in `error`, and start() returns."""
    session = Session(fake_device(ModelBoard()), 'runner')

    def poll():
        raise RuntimeError('a fault after the start')
    session.poll = poll
    runner = SessionRunner(session)
    assert runner.start() is runner
    assert until(lambda: not runner.running)
    assert isinstance(runner.error, RuntimeError)


def test_a_callback_that_raises_is_logged_and_the_runner_carries_on(caplog):
    """Test that an exception from on_update is logged, and the runner keeps the session and its claim."""
    board, steal = ModelBoard(), threading.Event()
    board.hooks.append(once(steal, lambda on: on.take_over()))

    def fail(update):
        raise RuntimeError('a fault in the host')
    runner = SessionRunner(Session(fake_device(board), 'runner'), on_update=fail).start()
    try:
        steal.set()
        assert until(lambda: 'the on_update callback raised' in caplog.text)
        runner.show(frame_of(0, (5, 5, 5)))
        assert until(lambda: runner.result is not None)
        assert runner.running and runner.error is None
    finally:
        runner.close()


def test_show_before_start_raises():
    """Test that a frame posted before start() is refused on the caller's thread."""
    runner = SessionRunner(Session(fake_device(ModelBoard()), 'runner'))
    with pytest.raises(hl.HostLightingError, match='start the runner'):
        runner.show(frame_of(0))


def test_a_group_tries_a_board_that_did_not_start_only_after_the_delay(monkeypatch, caplog):
    """Test that a held board is not claimed again at every poll, and the warning names the error."""
    held = ModelBoard()
    held.take_over(exclusive=True)
    attach(monkeypatch, held)
    held_id = held.board_id.hex().upper()
    watcher = ScriptedWatcher((frozenset({held_id}), frozenset()))
    group = SessionGroup(lambda device: Session(device, 'group'), watcher=watcher, retry_seconds=60)
    try:
        assert group.poll() == (frozenset(), frozenset())
        assert group.poll() == (frozenset(), frozenset()) and watcher.forgotten == []
        assert 'did not start. It is tried again in 60 s.' in caplog.text and 'holds the lights' in caplog.text
    finally:
        group.close()


class RefindingWatcher(BoardWatcher):
    """A watcher that finds one board, and finds it again at the poll after it is forgotten."""

    def __init__(self, board_id):
        """Hold the board ID, not yet found."""
        super().__init__()
        self.board_id, self.known = board_id, False

    def poll(self):
        """Report the board as arrived when it is not known."""
        if self.known:
            return frozenset(), frozenset()
        self.known = True
        return frozenset({self.board_id}), frozenset()

    def forget(self, board_id):
        """Forget the board, to be found again."""
        self.known = False


def test_a_group_warns_once_of_a_held_board_and_backs_its_retries_off(monkeypatch, caplog):
    """Test that a held board is warned of once, then logged at debug, and tried after a doubling wait."""
    held = ModelBoard()
    held.take_over(exclusive=True)
    attach(monkeypatch, held)
    now = [100.0]
    monkeypatch.setattr(runner_module, 'time', types.SimpleNamespace(monotonic=lambda: now[0]))
    caplog.set_level(logging.DEBUG, logger='gp2040ce_hostlighting.runner')
    watcher = RefindingWatcher(held.board_id.hex().upper())
    group = SessionGroup(lambda device: Session(device, 'group'), watcher=watcher, retry_seconds=1, retry_max_seconds=3)
    tries = []
    try:
        for _ in range(52):                        # 13 s in steps of 0.25 s
            claims = len(held.sent(hl.CMD_CLAIM))
            group.poll()
            if len(held.sent(hl.CMD_CLAIM)) > claims:
                tries.append(now[0])
            now[0] += 0.25
    finally:
        group.close()
    logged = [record for record in caplog.records if 'did not start' in record.getMessage()]
    assert [record.levelno for record in logged] == [logging.WARNING] + [logging.DEBUG] * (len(tries) - 1)
    assert [later - earlier for earlier, later in zip(tries, tries[1:])] == [1, 2, 3, 3, 3]
    assert 'It is tried again in 1 s.' in logged[0].getMessage()


def test_a_held_frame_past_a_lower_led_limit_is_dropped_after_the_reconnect(caplog):
    """Test that a board back with a lower LED limit drops the held frame with a warning, and the runner goes on."""
    board, back = ModelBoard(), threading.Event()

    def reopen(board_id):
        if not back.is_set():
            return None
        board.replug()
        return fake_device(board)
    runner = SessionRunner(Session(fake_device(board), 'runner', reopen=reopen), reconnect_seconds=0.01).start()
    try:
        frame = Frame()
        frame.run(hl.ADDRESSING_LED_RUN, 10, [(1, 2, 3)] * 5)
        runner.show(frame)
        assert until(lambda: runner.result is not None and runner.result.acknowledged)
        board.unplug()
        board.summary = summary_record(led_limit=12)
        back.set()
        assert until(lambda: 'the frame posted is dropped' in caplog.text)
        assert 'limit of 12' in caplog.text and runner.running and runner.result is None
        with pytest.raises(ValueError, match='limit of 12'):
            runner.show(frame)
        runner.show(frame_of(0, (0, 9, 0)))
        assert until(lambda: board.frames[-1].get(0) == (0, 9, 0))
    finally:
        runner.close()
    assert runner.error is None


def test_a_frame_past_the_limit_posted_during_a_reconnect_is_dropped(caplog):
    """Test that a frame posted while a reconnect reads the Summary, so not checked, is dropped when shown."""
    board, back, post = ModelBoard(), threading.Event(), threading.Event()
    late = Frame()
    late.run(hl.ADDRESSING_LED_RUN, board.led_limit() - 2, [(1, 2, 3)] * 5)
    runner = None

    def at_summary(on, request):
        if post.is_set() and request[0] == hl.CMD_GET_PAGE and request[3] == hl.PAGE_SUMMARY:
            post.clear()
            runner.show(late)
    board.hooks.append(at_summary)

    def reopen(board_id):
        if not back.is_set():
            return None
        board.replug()
        post.set()
        return fake_device(board)
    runner = SessionRunner(Session(fake_device(board), 'runner', reopen=reopen), reconnect_seconds=0.01).start()
    try:
        runner.show(frame_of(0, (9, 0, 0)))
        assert until(lambda: bool(board.frames))
        board.unplug()
        back.set()
        assert until(lambda: 'the frame posted is dropped' in caplog.text)
        assert not post.is_set() and runner.running
    finally:
        runner.close()
    assert runner.error is None


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


def test_a_runner_with_a_failed_start_closes_the_device():
    """Test that a session that does not start leaves no device open."""
    board = ModelBoard()
    board.take_over(exclusive=True)
    runner = SessionRunner(Session(fake_device(board), 'runner'))
    with pytest.raises(hl.HostLightingClaimedError):
        runner.start()
    assert runner.session.device.closed and runner.close()


def test_an_unanswered_configure_is_tried_again():
    """Test that settings with an unanswered renewal are sent at a later poll, the runner still running."""
    board, lose = ModelBoard(), threading.Event()
    board.hooks.append(lambda on, request: (on.drop.add if lose.is_set() else on.drop.discard)(hl.CMD_CLAIM))
    with SessionRunner(Session(fake_device(board), 'runner')) as runner:
        lose.set()
        runner.configure(hl.Settings(keepalive_ms=900))
        assert until(lambda: board.requests and board.requests[-1][0] == hl.CMD_CLAIM)
        lose.clear()
        assert until(lambda: board.keepalive_ms == 900) and runner.running and runner.error is None


def test_a_board_that_leaves_again_while_rejoining_is_looked_for_again():
    """Test that a board gone again as the frame is shown again is looked for again, not the runner's end."""
    board, leave, back, opened = ModelBoard(), threading.Event(), threading.Event(), []
    board.hooks.append(once(leave, lambda on: on.unplug()))

    def reopen(board_id):
        if not back.is_set():
            return None
        board.replug()
        opened.append(True)
        if len(opened) == 1:
            leave.set()                                 # gone again at the next request
        return fake_device(board)
    runner = SessionRunner(Session(fake_device(board), 'runner', reopen=reopen), reconnect_seconds=0.01).start()
    try:
        runner.show(frame_of(0))
        assert until(lambda: bool(board.frames))
        leave.set()
        assert until(lambda: board.departed)
        back.set()
        assert until(lambda: len(opened) >= 2 and board.holder_token != 0)
        assert runner.running and runner.error is None
    finally:
        runner.close()


def test_a_cancelled_async_start_closes_the_runner(monkeypatch):
    """Test that cancelling start() closes the runner once its start returns."""
    board, release = ModelBoard(), threading.Event()
    session = AsyncSession(Session(fake_device(board), 'async'))
    start = session.runner.start

    def slow_start():
        release.wait(5)
        return start()
    monkeypatch.setattr(session.runner, 'start', slow_start)

    async def main():
        task = asyncio.ensure_future(session.start())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(main())
    release.set()
    assert until(lambda: session.runner._stop.is_set() and not session.runner.running)
    assert until(lambda: board.holder_token == 0)


def test_the_watcher_gives_up_on_an_interface_that_is_not_a_board(monkeypatch):
    """Test that an interface with a failing HELLO is tried PROBE_TRIES times while it stays on the bus."""
    board = FakeBoard()
    attach(monkeypatch, board)
    board.magic = b'NOPE'
    watcher = BoardWatcher()
    for _ in range(PROBE_TRIES + 2):
        assert watcher.poll() == (frozenset(), frozenset())
    assert len(board.sent(hl.CMD_HELLO)) == PROBE_TRIES
