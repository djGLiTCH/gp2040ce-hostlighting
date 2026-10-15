"""Fixtures for the Host Lighting tests.

SPDX-FileCopyrightText: © 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import pytest

# Fake boards (tests/fakes.py) and runners for the hlp-* commands


def _hlp_runner(board, monkeypatch, clock=None):
    """Run hlp-* commands against a fake board: no real waiting, or the board's own clock.

    The Host Lighting modules each get this `time`. The rest of the process keeps the real one.
    """
    import sys
    import time
    from types import SimpleNamespace

    from fakes import fake_device

    from gp2040ce_hostlighting import protocol
    from gp2040ce_hostlighting.tools import commands, conformance
    if clock is None:
        timing = SimpleNamespace(sleep=lambda seconds: None, monotonic=time.monotonic, perf_counter=time.perf_counter)
    else:
        # lambdas, so that a test can replace the board's sleep
        timing = SimpleNamespace(sleep=lambda seconds: clock.sleep(seconds), monotonic=lambda: clock.time(),
                                 perf_counter=lambda: clock.time())
    for module in (protocol, commands, conformance):
        monkeypatch.setattr(module, 'time', timing)
    for module in (commands, conformance):
        monkeypatch.setattr(module, 'open_device', lambda prefix='': fake_device(board))

    def run(function, *arguments):
        monkeypatch.setattr(sys, 'argv', [function.__name__, *arguments])
        function()
    return run


@pytest.fixture
def hlp_fake_board():
    """Make a fake Host Lighting board that answers as the firmware does."""
    from fakes import FakeBoard
    return FakeBoard()


@pytest.fixture
def hlp_fake_device(hlp_fake_board):
    """Make a HostLightingDevice talking to the fake board."""
    from fakes import fake_device
    return fake_device(hlp_fake_board)


@pytest.fixture
def hlp_run_on_fake_board(hlp_fake_board, monkeypatch):
    """Run an hlp-* command against the fake board, with no real waiting."""
    return _hlp_runner(hlp_fake_board, monkeypatch)


@pytest.fixture
def hlp_model_board():
    """Make a fake board that follows the protocol's rules on its own clock."""
    from fakes import ModelBoard
    return ModelBoard()


@pytest.fixture
def hlp_run_on_model_board(hlp_model_board, monkeypatch):
    """Run an hlp-* command against the model board, with time on the board's clock."""
    return _hlp_runner(hlp_model_board, monkeypatch, clock=hlp_model_board)
