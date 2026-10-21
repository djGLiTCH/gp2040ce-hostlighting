"""Run sessions on threads of their own: one board from a GUI or an asyncio program, or every board on the bus.

A device belongs to one thread, and every call on it blocks. A SessionRunner gives a Session a thread that owns
its device: the host posts the latest frame and new settings from any thread, and receives each SessionUpdate
through a callback and a queue. AsyncSession puts a runner behind awaitables. BoardWatcher follows the boards on
the bus by board ID, and SessionGroup keeps a runner for each.

SPDX-FileCopyrightText: © 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import asyncio
import logging
import queue
import threading
import time
from typing import Callable, Optional

from gp2040ce_hostlighting.claim import ClaimChangeKind
from gp2040ce_hostlighting.frames import Frame, FrameResult
from gp2040ce_hostlighting.protocol import (HostLightingDevice, HostLightingDisconnectedError, HostLightingError,
                                            HostLightingTimeoutError, _probe, find_board, find_devices)
from gp2040ce_hostlighting.records import Settings
from gp2040ce_hostlighting.session import Session, SessionUpdate

__all__ = ['AsyncSession', 'BoardWatcher', 'SessionGroup', 'SessionRunner', 'POLL_SECONDS', 'PROBE_TRIES',
           'RECONNECT_SECONDS', 'RETRY_MAX_SECONDS', 'UPDATES_KEPT']

logger = logging.getLogger(__name__)

POLL_SECONDS = 0.01             # between a runner's polls
RECONNECT_SECONDS = 1.0         # between a runner's looks for a board that left the bus
RETRY_MAX_SECONDS = 30.0        # the longest a group waits to try again a board that failed to start a session
UPDATES_KEPT = 64               # a runner's queue of updates, the oldest dropped first
PROBE_TRIES = 3                 # a watcher's HELLOs to an interface that does not answer as a board


def _worth_telling(update: SessionUpdate) -> bool:
    """Say whether an update carries anything: a claim change, changed page 1 fields, pages read or a restage."""
    return (update.claim.kind is not ClaimChangeKind.UNCHANGED or bool(update.changed) or bool(update.pages_read)
            or update.restage)


class SessionRunner:
    """A Session on a thread of its own, which alone uses the session's device.

    The thread starts the session, then polls it, sends the settings and frame posted last, and shows the latest
    frame again whenever an update asks for a restage. A frame with a show neither acknowledged nor refused is
    sent again at the next poll. A board that leaves the bus is looked for again by its board ID every
    RECONNECT_SECONDS, unless reconnect is off, when the runner ends, and the latest frame is shown on it. An
    exception from on_update is logged and the runner carries on, and a frame past the board's LED limit is dropped
    with a warning. Any other error ends the runner, and `error` keeps it. Whenever the runner ends, a start that
    fails included, it closes the session and its device. close() ends the session from any thread and waits for
    the thread. The thread is a daemon thread, so a host closes each runner before it exits, or the claim lapses at
    its timeout rather than being given up.
    """

    def __init__(self, session: Session, on_update: Optional[Callable[[SessionUpdate], None]] = None,
                 on_end: Optional[Callable[[Optional[BaseException]], None]] = None,
                 poll_seconds: float = POLL_SECONDS, reconnect: bool = True,
                 reconnect_seconds: float = RECONNECT_SECONDS) -> None:
        """Describe the runner. Nothing runs until start().

        :param session: a session not yet started, with a device no other thread uses from now on
        :param on_update: called on the runner's thread with each update that carries anything
        :param on_end: called on the runner's thread when it ends, with the error that ended it or None
        :param poll_seconds: the wait between polls
        :param reconnect: look for a board that left the bus again, rather than end
        :param reconnect_seconds: the wait between looks
        """
        self.session = session
        self.on_update = on_update
        self.on_end = on_end
        self.poll_seconds = poll_seconds
        self.reconnect = reconnect
        self.reconnect_seconds = reconnect_seconds
        # the updates that carry anything, for a host that reads them rather than take the callback
        self.updates: queue.Queue[SessionUpdate] = queue.Queue(UPDATES_KEPT)
        self.error: Optional[BaseException] = None      # what ended the runner, if anything did
        self.result: Optional[FrameResult] = None       # what became of the last frame, None if not sent
        self._lock = threading.Lock()
        self._frame: Optional[Frame] = None             # posted, not yet taken by the thread
        self._latest: Optional[Frame] = None            # the last frame taken, shown again when needed
        self._pending = False                           # the latest frame still needs a show
        self._start_error: Optional[BaseException] = None
        self._settings: Optional[Settings] = None       # posted, not yet sent
        self._stop = threading.Event()
        self._started = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> 'SessionRunner':
        """Start the thread, and wait for the session's start.

        :return: the runner
        :raises HostLightingError: when the session does not start, as Session.start() raises it. An error after
            the start is kept in `error` and not raised here
        """
        if self._thread is not None:
            raise HostLightingError("the runner has already started")
        self._thread = threading.Thread(target=self._run, name=f"hlp-{self.session.name or 'session'}", daemon=True)
        self._thread.start()
        self._started.wait()
        if self._start_error is not None:
            raise self._start_error
        return self

    @property
    def running(self) -> bool:
        """Say whether the runner's thread is running."""
        return self._thread is not None and self._thread.is_alive()

    def show(self, frame: Frame) -> None:
        """Post a whole frame, to be shown at the next poll in place of any frame posted and not yet shown.

        The runner keeps the frame and shows it again at each restage, so the host posts a new Frame for a
        change rather than changing one it posted.

        :raises HostLightingError: before start() has returned
        :raises ValueError: for an LED run past the board's addressable LED limit
        """
        if not self._started.is_set() or self._start_error is not None:
            raise HostLightingError("start the runner before posting a frame")
        cache = self.session.cache
        summary = cache.summary if cache is not None else None
        if summary is not None:
            frame.check_led_limit(summary.led_limit)
        with self._lock:
            self._frame = frame

    def configure(self, settings: Settings) -> None:
        """Post new CONFIGURE settings, sent at the next poll."""
        with self._lock:
            self._settings = settings

    def close(self, timeout: float = 5.0) -> bool:
        """End the session and wait up to timeout seconds for the thread.

        :return: whether the thread has ended, which it has not when called from the runner's own thread
        """
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout)
        return not self.running

    def __enter__(self) -> 'SessionRunner':
        """Start the runner for a with block, which closes it."""
        return self.start()

    def __exit__(self, *exception: object) -> None:
        """Close the runner."""
        self.close()

    def _tell(self, update: SessionUpdate) -> None:
        if not _worth_telling(update):
            return
        if self.updates.full():
            try:
                self.updates.get_nowait()       # the oldest goes first
            except queue.Empty:
                pass
        self.updates.put_nowait(update)
        if self.on_update is not None:
            try:
                self.on_update(update)
            except Exception:  # noqa: B902 a host's callback must not end the session
                logger.warning("the on_update callback raised, and the runner carries on", exc_info=True)

    def _taken(self) -> tuple[Optional[Frame], Optional[Settings]]:
        with self._lock:
            frame, settings, self._frame, self._settings = self._frame, self._settings, None, None
        return frame, settings

    def _run(self) -> None:
        try:
            self.session.start()
        except BaseException as error:  # noqa: B902 handed to start()'s caller
            self.error = self._start_error = error
            self._close_session()
            self._started.set()
            self._ended()
            return
        self._started.set()
        try:
            while not self._stop.is_set():
                try:
                    self._cycle()
                except HostLightingDisconnectedError:
                    if not self.reconnect:
                        raise
                    self._rejoin()
                self._stop.wait(self.poll_seconds)
        except BaseException as error:  # noqa: B902 kept for the host
            self.error = error
            logger.debug("the runner ended", exc_info=True)
        finally:
            self._close_session()
            self._ended()

    def _close_session(self) -> None:
        try:
            self.session.close()
        except (HostLightingError, OSError):
            logger.debug("closing the session failed", exc_info=True)

    def _cycle(self) -> None:
        """Poll, send the settings posted, show the latest frame when it is new, unanswered or restaged, then tell."""
        update = self.session.poll()
        frame, settings = self._taken()
        if settings is not None:
            try:
                self.session.configure(settings)
            except HostLightingTimeoutError:
                with self._lock:
                    self._settings = self._settings or settings      # tried again at the next poll
        if frame is not None:
            self._latest, self._pending = frame, True
        if update.restage:
            self._pending = True
        if self._latest is not None and self._pending:
            self._show(self._latest)
        self._tell(update)

    def _show(self, frame: Frame) -> None:
        """Show the latest frame.

        It stays pending until a show is acknowledged or refused. A frame not sent, as while another application
        holds the claim, waits for the restage that follows the claim's return. A frame past the board's LED
        limit, as one posted while a reconnect reads the Summary or held for a board back with a lower limit, is
        dropped, and result is None, as for a frame not sent.
        """
        try:
            result = self.session.show(frame)
        except ValueError as error:
            self.result, self._latest, self._pending = None, None, False
            reason = str(error)
            logger.warning("the frame posted is dropped. %s", reason)
            return
        self.result = result
        self._pending = result is not None and not (result.acknowledged or result.refused is not None)

    def _rejoin(self) -> None:
        """Look for the board that left until it is back or the runner closes, then show the latest frame on it.

        A board that leaves again before the frame lands is looked for again.
        """
        while not self._stop.is_set():
            if self.session.reconnect():
                try:
                    if self._latest is not None:
                        self._show(self._latest)
                    return
                except HostLightingDisconnectedError:
                    logger.debug("the board left again", exc_info=True)
            self._stop.wait(self.reconnect_seconds)

    def _ended(self) -> None:
        if self.on_end is not None:
            self.on_end(self.error)


class AsyncSession:
    """A SessionRunner behind awaitables, for an asyncio program.

    start() and close() run in the loop's executor, and a start cancelled closes the runner. show() and
    configure() post at once. update() waits for the next update that carries anything, UPDATES_KEPT of them kept.
    `async for` takes them until the runner is closed, and raises the error that ended it when one did.
    """

    def __init__(self, session: Session, poll_seconds: float = POLL_SECONDS, reconnect: bool = True,
                 reconnect_seconds: float = RECONNECT_SECONDS) -> None:
        """Describe the session's runner. Nothing runs until start()."""
        self.runner = SessionRunner(session, self._deliver, self._end, poll_seconds, reconnect, reconnect_seconds)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._queue: Optional[asyncio.Queue[Optional[SessionUpdate]]] = None

    async def start(self) -> 'AsyncSession':
        """Start the runner and wait for the session's start.

        :raises HostLightingError: when the session does not start
        """
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(UPDATES_KEPT)
        try:
            await self._loop.run_in_executor(None, self.runner.start)
        except asyncio.CancelledError:
            threading.Thread(target=self.runner.close, daemon=True).start()
            raise
        return self

    def show(self, frame: Frame) -> None:
        """Post a whole frame to the runner, checked against the LED limit at once."""
        self.runner.show(frame)

    def configure(self, settings: Settings) -> None:
        """Post new CONFIGURE settings."""
        self.runner.configure(settings)

    async def update(self) -> SessionUpdate:
        """Wait for the next update that carries anything.

        :raises HostLightingError: once the runner has ended, with the error that ended it when there was one
        """
        if self._queue is None:
            raise HostLightingError("the session has not started")
        update = await self._queue.get()
        if update is None:
            self._queue.put_nowait(None)        # every later call ends too
            error = self.runner.error
            if isinstance(error, HostLightingError):
                raise error
            raise HostLightingError("the session has ended") from error
        return update

    async def close(self) -> None:
        """End the session and wait for the runner's thread."""
        if self._loop is not None:
            await self._loop.run_in_executor(None, self.runner.close)

    async def __aenter__(self) -> 'AsyncSession':
        """Start for an async with block, which closes."""
        return await self.start()

    async def __aexit__(self, *exception: object) -> None:
        """Close."""
        await self.close()

    def __aiter__(self) -> 'AsyncSession':
        """Take updates with async for, until the runner is closed."""
        return self

    async def __anext__(self) -> SessionUpdate:
        """Wait for the next update, ending the iteration once the runner is closed.

        :raises HostLightingError: the error that ended the runner, when one did
        """
        try:
            return await self.update()
        except HostLightingError:
            if self.runner.error is not None:
                raise
            raise StopAsyncIteration from None

    def _deliver(self, update: SessionUpdate) -> None:
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._put, update)

    def _end(self, error: Optional[BaseException]) -> None:
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._put, None)

    def _put(self, update: Optional[SessionUpdate]) -> None:
        """On the loop's thread: queue an update, or the end, the oldest update dropped when full."""
        if self._queue is None:
            return
        if self._queue.full():
            self._queue.get_nowait()
        self._queue.put_nowait(update)


class BoardWatcher:
    """Follow the protocol 2 boards on the bus by board ID.

    Each poll enumerates the interfaces, which sends nothing, and opens only an interface not seen before, for
    its HELLO's board ID. One that does not answer as a board is tried again at the next poll, PROBE_TRIES times
    in all while it stays on the bus.
    """

    def __init__(self) -> None:
        """Start knowing no board."""
        self.boards: dict[str, bytes] = {}          # board ID -> interface path
        self._paths: dict[bytes, str] = {}           # interface path -> board ID
        self._tries: dict[bytes, int] = {}           # interface path -> HELLOs that found no board

    def poll(self) -> tuple[frozenset[str], frozenset[str]]:
        """Look at the bus once.

        :return: (the board IDs that arrived, the board IDs that left) since the last poll
        """
        infos = {bytes(info['path']): info for info in find_devices()}
        left = {self._paths.pop(path) for path in list(self._paths) if path not in infos}
        self._tries = {path: tries for path, tries in self._tries.items() if path in infos}
        for board_id in left:
            self.boards.pop(board_id, None)
        arrived = set()
        for path, info in infos.items():
            if path in self._paths or self._tries.get(path, 0) >= PROBE_TRIES:
                continue
            found = _probe(info)
            if not found:
                self._tries[path] = self._tries.get(path, 0) + 1
                continue
            hello, device = found
            device.close()
            self._paths[path], self.boards[hello.board_id] = hello.board_id, path
            arrived.add(hello.board_id)
        return frozenset(arrived - left), frozenset(left - arrived)

    def forget(self, board_id: str) -> None:
        """Forget a board, so that the next poll opens its interface again and reports it arrived."""
        path = self.boards.pop(board_id, None)
        if path is not None:
            self._paths.pop(path, None)
            self._tries.pop(path, None)


class SessionGroup:
    """A SessionRunner for each board on the bus, started when the board arrives and closed when it leaves.

    A runner that ends is dropped, and its board is reported again at the next poll that finds it, when a runner
    starts for it again. A board that fails to start a session, as when another application holds it and
    make_session's Session does not wait for the claim, is tried again after retry_seconds, the wait doubling at
    each failure up to retry_max_seconds. The first failure is logged as a warning and the rest at debug, until
    a start succeeds or the board leaves. A session built with wait=True instead starts once the other
    application gives the board up.
    """

    def __init__(self, make_session: Callable[[HostLightingDevice], Session],
                 on_update: Optional[Callable[[str, SessionUpdate], None]] = None,
                 watcher: Optional[BoardWatcher] = None, poll_seconds: float = POLL_SECONDS,
                 retry_seconds: float = RECONNECT_SECONDS, retry_max_seconds: float = RETRY_MAX_SECONDS) -> None:
        """Describe the group. Nothing runs until poll().

        :param make_session: builds a session, not yet started, on a board's opened device
        :param on_update: called on a runner's thread with the board ID and each update that carries anything
        :param watcher: the board watcher, by default a new one
        :param poll_seconds: each runner's wait between polls
        :param retry_seconds: the wait before a board that failed to start a session is tried again
        :param retry_max_seconds: the longest that wait grows to, doubling at each failure
        """
        self.make_session = make_session
        self.on_update = on_update
        self.watcher = watcher or BoardWatcher()
        self.poll_seconds = poll_seconds
        self.runners: dict[str, SessionRunner] = {}
        self.retry_seconds = retry_seconds
        self.retry_max_seconds = retry_max_seconds
        self._retry: dict[str, float] = {}      # board ID -> when a board that failed to start is tried
        self._wait: dict[str, float] = {}       # board ID -> the wait after its next failure, while failing

    def poll(self) -> tuple[frozenset[str], frozenset[str]]:
        """Look at the bus once: start a runner for each board that arrived, close each for a board that left.

        :return: (the board IDs a runner started for, the board IDs with a runner that closed)
        """
        for board_id in [board_id for board_id, runner in self.runners.items() if not runner.running]:
            self.runners.pop(board_id)
            self.watcher.forget(board_id)
        now = time.monotonic()
        for board_id in [board_id for board_id, due in self._retry.items() if due <= now]:
            self._retry.pop(board_id)
            self.watcher.forget(board_id)
        arrived, left = self.watcher.poll()
        for board_id in left:
            self._retry.pop(board_id, None)
            self._wait.pop(board_id, None)
        closed = set()
        for board_id in left:
            runner = self.runners.pop(board_id, None)
            if runner is not None:
                runner.close()
                closed.add(board_id)
        started = set()
        for board_id in arrived:
            device = self._open(board_id)
            if device is None:
                self.watcher.forget(board_id)
                continue
            tell = None if self.on_update is None else self._teller(board_id, self.on_update)
            runner = SessionRunner(self.make_session(device), tell, poll_seconds=self.poll_seconds, reconnect=False)
            try:
                runner.start()
            except HostLightingError as error:
                reason = str(error)
                wait = self._wait.get(board_id, self.retry_seconds)
                level = logging.DEBUG if board_id in self._wait else logging.WARNING
                logger.log(level, "the session on board %s did not start. It is tried again in %s s. %s", board_id,
                           wait, reason)
                self._retry[board_id] = time.monotonic() + wait
                self._wait[board_id] = min(wait * 2, self.retry_max_seconds)
                continue
            self._wait.pop(board_id, None)
            self.runners[board_id] = runner
            started.add(board_id)
        return frozenset(started), frozenset(closed)

    def show(self, board_id: str, frame: Frame) -> None:
        """Post a frame to one board's runner.

        :raises KeyError: for a board without a runner
        """
        self.runners[board_id].show(frame)

    def close(self) -> None:
        """Close every runner."""
        for runner in self.runners.values():
            runner.close()
        self.runners.clear()

    def _open(self, board_id: str) -> Optional[HostLightingDevice]:
        """Open a board by the interface the watcher found it on, else look for it by board ID."""
        path = self.watcher.boards.get(board_id)
        if path is None:
            return find_board(board_id)
        try:
            device = HostLightingDevice(path)
        except HostLightingError:
            return None
        try:
            if device.hello().board_id == board_id:
                return device
        except (HostLightingError, OSError):
            logger.debug("board %s did not answer on %r", board_id, path, exc_info=True)
        device.close()
        return None

    @staticmethod
    def _teller(board_id: str, on_update: Callable[[str, SessionUpdate], None]) -> Callable[[SessionUpdate], None]:
        return lambda update: on_update(board_id, update)
