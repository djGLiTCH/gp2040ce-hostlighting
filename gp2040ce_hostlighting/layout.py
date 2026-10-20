"""The board's LED layout grid: lights by cell, and a canvas painted by cell and sampled onto the lights.

Page 11 gives each light a cell of the grid the Summary sizes (grid_width x grid_height). lights_by_cell() maps
each cell to its lights. A Canvas holds one colour per cell, and frame() builds a Frame that colours each light
with its cell's colour.

SPDX-FileCopyrightText: © 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
from typing import Iterable, Optional

from gp2040ce_hostlighting.frames import Frame
from gp2040ce_hostlighting.protocol import ADDRESSING_LIGHT, _check_colour
from gp2040ce_hostlighting.records import LightRecord, Summary

__all__ = ['Canvas', 'lights_by_cell']

Colour = tuple[int, ...]


def lights_by_cell(lights: Iterable[LightRecord]) -> dict[tuple[int, int], tuple[int, ...]]:
    """Map each grid cell (x, y) to the ordinals of the lights in it, lights without a grid group left out."""
    cells: dict[tuple[int, int], list[int]] = {}
    for light in lights:
        if light.has('grid'):
            cells.setdefault((light.grid.x, light.grid.y), []).append(light.ordinal)
    return {cell: tuple(ordinals) for cell, ordinals in cells.items()}


class Canvas:
    """One colour per cell of the LED layout grid, (0, 0) the top left, every cell black at first."""

    def __init__(self, width: int, height: int, colour: Colour = (0, 0, 0)) -> None:
        """Make a canvas of the grid's size.

        :param width: the grid's width in cells, the Summary's grid_width
        :param height: the grid's height in cells, the Summary's grid_height
        :param colour: the colour of every cell at first, three or four channels of 0-255
        :raises ValueError: for a negative size or a colour encode_pixel would refuse
        """
        if width < 0 or height < 0:
            raise ValueError(f"a canvas of {width} x {height} cells")
        _check_colour(colour)
        self.width, self.height = width, height
        self._cells = [[tuple(colour)] * width for _ in range(height)]

    @classmethod
    def for_summary(cls, summary: Summary, colour: Colour = (0, 0, 0)) -> 'Canvas':
        """Make a canvas of the size of a board's grid."""
        return cls(summary.grid_width, summary.grid_height, colour)

    def __getitem__(self, cell: tuple[int, int]) -> Colour:
        """Give a cell's colour.

        :raises IndexError: for a cell off the canvas
        """
        x, y = self._inside(cell)
        return self._cells[y][x]

    def __setitem__(self, cell: tuple[int, int], colour: Colour) -> None:
        """Paint a cell.

        :raises IndexError: for a cell off the canvas
        :raises ValueError: for a colour encode_pixel would refuse
        """
        x, y = self._inside(cell)
        _check_colour(colour)
        self._cells[y][x] = tuple(colour)

    def fill(self, colour: Colour) -> None:
        """Paint every cell."""
        _check_colour(colour)
        self._cells = [[tuple(colour)] * self.width for _ in range(self.height)]

    def frame(self, lights: Iterable[LightRecord], pixel_format: Optional[int] = None) -> Frame:
        """Build a frame that colours each light with its cell's colour, staged by light ordinal.

        A light without a grid group, or with its cell off the canvas, is left out.
        """
        entries = [(light.ordinal, self._cells[light.grid.y][light.grid.x]) for light in lights
                   if light.has('grid') and light.grid.x < self.width and light.grid.y < self.height]
        frame = Frame()
        frame.entries(ADDRESSING_LIGHT, entries, pixel_format)
        return frame

    def _inside(self, cell: tuple[int, int]) -> tuple[int, int]:
        x, y = cell
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise IndexError(f"cell {cell} is off a canvas of {self.width} x {self.height}")
        return x, y
