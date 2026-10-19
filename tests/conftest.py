"""Fixtures for the Host Lighting tests.

SPDX-FileCopyrightText: © 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import logging

import pytest

# Fake boards (gp2040ce_hostlighting.testing) and runners for the hlp-* commands


def _hlp_runner(board, monkeypatch, clock=None):
    """Run hlp-* commands against a fake board: no real waiting, or the board's own clock.

    The Host Lighting modules each get this `time`. The rest of the process keeps the real one. Without the
    board's clock, the claim runs on a clock that only its sleeps advance, so that a hold takes no real time.
    """
    import sys
    import time
    from types import SimpleNamespace

    from gp2040ce_hostlighting import claim
    from gp2040ce_hostlighting import conformance as checks_module
    from gp2040ce_hostlighting import protocol
    from gp2040ce_hostlighting.testing import fake_device
    from gp2040ce_hostlighting.tools import commands, conformance
    if clock is None:
        timing = SimpleNamespace(sleep=lambda seconds: None, monotonic=time.monotonic, perf_counter=time.perf_counter)
        now = [0.0]

        def sleep(seconds):
            timing.sleep(seconds)       # a test can replace the commands' sleep
            now[0] += seconds
        monkeypatch.setattr(claim, 'time', SimpleNamespace(sleep=sleep, monotonic=lambda: now[0]))
    else:
        # lambdas, so that a test can replace the board's sleep
        timing = SimpleNamespace(sleep=lambda seconds: clock.sleep(seconds), monotonic=lambda: clock.time(),
                                 perf_counter=lambda: clock.time())
        monkeypatch.setattr(claim, 'time', timing)
    for module in (protocol, checks_module, commands, conformance):
        monkeypatch.setattr(module, 'time', timing)
    for module in (commands, conformance):
        monkeypatch.setattr(module, 'open_device', lambda prefix='': fake_device(board))

    def run(function, *arguments):
        monkeypatch.setattr(sys, 'argv', [function.__name__, *arguments])
        function()
    return run


@pytest.fixture(autouse=True)
def _hlp_claim_log():
    """Leave the claim's logger as it was, after a command set it up to print the claim's INFO lines."""
    log = logging.getLogger('gp2040ce_hostlighting.claim')
    handlers, level = list(log.handlers), log.level
    yield
    log.handlers[:] = handlers
    log.setLevel(level)


@pytest.fixture
def hlp_fake_board():
    """Make a fake Host Lighting board that answers as the firmware does."""
    from gp2040ce_hostlighting.testing import FakeBoard
    return FakeBoard()


@pytest.fixture
def hlp_fake_device(hlp_fake_board):
    """Make a HostLightingDevice talking to the fake board."""
    from gp2040ce_hostlighting.testing import fake_device
    return fake_device(hlp_fake_board)


@pytest.fixture
def hlp_run_on_fake_board(hlp_fake_board, monkeypatch):
    """Run an hlp-* command against the fake board, with no real waiting."""
    return _hlp_runner(hlp_fake_board, monkeypatch)


@pytest.fixture
def hlp_model_board():
    """Make a fake board that follows the protocol's rules on its own clock."""
    from gp2040ce_hostlighting.testing import ModelBoard
    return ModelBoard()


@pytest.fixture
def hlp_run_on_model_board(hlp_model_board, monkeypatch):
    """Run an hlp-* command against the model board, with time on the board's clock."""
    return _hlp_runner(hlp_model_board, monkeypatch, clock=hlp_model_board)
