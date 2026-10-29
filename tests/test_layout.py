# gp2040ce-hostlighting (GP2040-CE Host Lighting library and tools)
# Filename: test_layout.py
# Folder: tests
# https://github.com/djGLiTCH/gp2040ce-hostlighting
# SPDX-FileCopyrightText: (C) 2026 Jacob Simpson (DJ GLiTCH)
# SPDX-License-Identifier: GPL-3.0-or-later
# See LICENSES/GPL-3.0-or-later.txt at the repository root.

"""Test lights_by_cell() and Canvas."""
import pytest

from gp2040ce_hostlighting import protocol as hl
from gp2040ce_hostlighting.frames import publish_frame
from gp2040ce_hostlighting.layout import Canvas, lights_by_cell
from gp2040ce_hostlighting.testing import ModelBoard, fake_device


def test_lights_by_cell_and_a_canvas_sampled_onto_the_lights():
    """Test the cells of the model board's lights, and a canvas of its grid painted and published."""
    board = ModelBoard()
    device = fake_device(board)
    lights = device.read_table(hl.PAGE_LIGHTS).records
    assert lights_by_cell(lights) == {(1, 0): (0,), (0, 1): (1,)}
    canvas = Canvas.for_summary(device.read_record(hl.PAGE_SUMMARY).records[0].summary)
    assert (canvas.width, canvas.height) == (2, 2) and canvas[0, 0] == (0, 0, 0)
    canvas[1, 0] = (255, 0, 0)
    canvas[0, 1] = (0, 0, 255)
    publish_frame(device, canvas.frame(lights))
    assert board.frames[-1] == {0: (255, 0, 0), 1: (0, 0, 255), 2: (0, 0, 255), 3: (0, 0, 255)}
    canvas.fill((1, 2, 3))
    assert canvas[1, 1] == (1, 2, 3)
    small = Canvas(1, 1)
    assert small.frame(lights).addresses == frozenset()        # both cells are off a 1 x 1 canvas


def test_a_canvas_refuses_cells_off_it_and_bad_colours():
    """Test the size, cell and colour checks."""
    canvas = Canvas(2, 1, (9, 9, 9))
    with pytest.raises(IndexError, match='off a canvas of 2 x 1'):
        canvas[2, 0]
    with pytest.raises(IndexError):
        canvas[0, 1] = (0, 0, 0)
    with pytest.raises(ValueError):
        canvas[0, 0] = (256, 0, 0)
    with pytest.raises(ValueError):
        canvas.fill((1, 2))
    with pytest.raises(ValueError, match='-1 x 1'):
        Canvas(-1, 1)
