"""Build a whole frame and publish it as the specification has a streaming host do.

A Frame collects entries and runs, and stages them in the order they were added, since a later entry wins where
lights share an LED. publish_frame() sends every report but the last with NO_REPLY and the last with
COMMIT_AFTER, which alone is answered, and sends that report once more when its acknowledgement does not come.

Like the device, a Frame belongs to one thread.

SPDX-FileCopyrightText: © 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from gp2040ce_hostlighting.protocol import (ADDRESS_SIZES, CMD_CLEAR, CMD_STAGE, FLAG_COMMIT_AFTER, FLAG_NO_REPLY,
                                            PIXEL_RGB, PIXEL_RGBW, PIXEL_SIZES, STATUS_OK, Controller,
                                            HostLightingDevice, HostLightingTimeoutError, _check_colour, _check_run,
                                            encode_address, stage_entry_payloads, stage_run_payloads)

__all__ = ['ACKNOWLEDGEMENT_TIMEOUT', 'Frame', 'FrameResult', 'publish_frame']

# the shortest request timeout the specification allows, past the board's settings write of about 200 ms
ACKNOWLEDGEMENT_TIMEOUT = 0.25


@dataclass(frozen=True, slots=True, kw_only=True)
class FrameResult:
    """What became of a published frame.

    `acknowledged` is true when the frame's last report was answered with status 0, and the result is true
    then. `resent` is true when that report was sent a second time. `refused` is the holder a status 4 refusal
    showed, where Session.show() turned the refusal into a result. publish_frame() raises a refusal instead.
    """

    acknowledged: bool
    resent: bool
    refused: Optional[Controller] = None

    def __bool__(self) -> bool:
        """Say whether the frame was acknowledged."""
        return self.acknowledged


class _Part:
    """Neighbouring calls with the same addressing and pixel format, staged together."""

    __slots__ = ('addressing', 'pixel_format', 'first', 'items')

    def __init__(self, addressing: int, pixel_format: int, first: Optional[int], items: list[Any]):
        self.addressing = addressing
        self.pixel_format = pixel_format
        self.first = first              # a run's first index, None for entries
        self.items = items              # a run's colours, or (address, colour) entries

    def key(self) -> tuple[Any, ...]:
        """Return what the part stages, for comparing frames."""
        return self.addressing, self.pixel_format, self.first, tuple(self.items)


def _pixel_format(colours: Iterable[tuple[int, ...]], pixel_format: Optional[int]) -> int:
    """Return the pixel format given, or RGBW when a colour has a white byte and RGB otherwise."""
    if pixel_format is None:
        return PIXEL_RGBW if any(len(colour) > 3 for colour in colours) else PIXEL_RGB
    if pixel_format not in PIXEL_SIZES:
        raise ValueError(f"unknown pixel format {pixel_format}")
    return pixel_format


class Frame:
    """A whole frame: entries addressed by light, pin, action or kind, and runs of LEDs or lights.

    Each call is staged after the ones before it. Neighbouring calls with the same addressing and pixel format
    are joined, entries into fewer reports and a run onto the run it continues.
    """

    def __init__(self) -> None:
        """Start with nothing staged."""
        self._parts: list[_Part] = []

    def __eq__(self, other: object) -> bool:
        """Compare what two frames stage, in order."""
        if not isinstance(other, Frame):
            return NotImplemented
        return [part.key() for part in self._parts] == [part.key() for part in other._parts]

    __hash__ = None             # type: ignore[assignment]  # a frame changes as calls are added

    def __repr__(self) -> str:
        """Show the frame's parts."""
        return f"Frame({[part.key() for part in self._parts]!r})"

    def entries(self, addressing: int, entries: Iterable[tuple[int, tuple[int, ...]]],
                pixel_format: Optional[int] = None) -> None:
        """Add entries.

        :param addressing: ADDRESSING_LIGHT, ADDRESSING_PIN, ADDRESSING_ACTION or ADDRESSING_KIND
        :param entries: (address, colour) pairs, each colour as for encode_pixel
        :param pixel_format: PIXEL_RGB, PIXEL_RGBW or PIXEL_RGB565, by default RGBW when a colour has a white
            byte and RGB otherwise
        :raises ValueError: for a run's addressing, an address outside its range, a colour that is not three or
            four integer channels of 0-255, or an unknown pixel format
        """
        if addressing not in ADDRESS_SIZES:
            raise ValueError(f"addressing {addressing} does not take entries")
        entries = list(entries)
        if not entries:
            return
        for address, colour in entries:
            encode_address(addressing, address)
            _check_colour(colour)
        pixel_format = _pixel_format((colour for _, colour in entries), pixel_format)
        last = self._parts[-1] if self._parts else None
        if last and last.first is None and (last.addressing, last.pixel_format) == (addressing, pixel_format):
            last.items.extend(entries)
        else:
            self._parts.append(_Part(addressing, pixel_format, None, entries))

    def run(self, addressing: int, first: int, colours: Iterable[tuple[int, ...]],
            pixel_format: Optional[int] = None) -> None:
        """Add a run of consecutive LEDs or lights.

        :param addressing: ADDRESSING_LED_RUN or ADDRESSING_LIGHT_RUN
        :param first: the first LED index or light ordinal
        :param colours: one colour per LED or light, as for encode_pixel
        :param pixel_format: as for entries()
        :raises ValueError: for an entry addressing, no colours, a run past index 0xFFFF, a colour that is not three
            or four integer channels of 0-255, or an unknown pixel format
        """
        colours = list(colours)
        _check_run(addressing, first, len(colours))
        for colour in colours:
            _check_colour(colour)
        pixel_format = _pixel_format(colours, pixel_format)
        last = self._parts[-1] if self._parts else None
        if (last and last.first is not None and (last.addressing, last.pixel_format) == (addressing, pixel_format)
                and last.first + len(last.items) == first):
            last.items.extend(colours)
        else:
            self._parts.append(_Part(addressing, pixel_format, first, colours))

    @property
    def addresses(self) -> frozenset[tuple[int, int]]:
        """Every (addressing, address) the frame names, a run naming each of its indexes."""
        named: set[tuple[int, int]] = set()
        for part in self._parts:
            if part.first is None:
                named.update((part.addressing, address) for address, _ in part.items)
            else:
                named.update((part.addressing, part.first + index) for index in range(len(part.items)))
        return frozenset(named)

    def payloads(self) -> list[bytes]:
        """Build the frame's STAGE payloads, in order, and none for an empty frame."""
        payloads = []
        for part in self._parts:
            if part.first is None:
                payloads += stage_entry_payloads(part.addressing, part.items, part.pixel_format)
            else:
                payloads += stage_run_payloads(part.addressing, part.first, part.items, part.pixel_format)
        return payloads


def publish_frame(device: HostLightingDevice, frame: Frame, clear_first: bool = False,
                  timeout: float = ACKNOWLEDGEMENT_TIMEOUT) -> FrameResult:
    """Stage a whole frame and publish it.

    The reports already received are read first. Every report but the last goes with NO_REPLY, and the last
    with COMMIT_AFTER, so it stages, publishes and is the frame's one acknowledgement. When that does not come,
    the report is sent once more. A frame then unacknowledged is not shown, and the next is staged whole. The
    board is lost only when a HELLO then goes unanswered too.

    The staged frame lasts while the takeover does, so an address the frame no longer names keeps its colour
    unless the frame is cleared first. An empty frame is published by CLEAR, which carries the commit.

    :param device: an opened device, holding the claim when one is held
    :param frame: the frame
    :param clear_first: send CLEAR first, which unstages every pixel
    :param timeout: seconds to wait for the acknowledgement, at least the specification's 250 ms
    :return: whether the frame was acknowledged, and whether its last report was sent again
    :raises ValueError: for a timeout under 250 ms
    :raises HostLightingRejectedError: when the board refuses the last report, with the holder for status 4
    """
    if timeout < ACKNOWLEDGEMENT_TIMEOUT:
        raise ValueError(f"the acknowledgement timeout is {timeout} s, under the specification's 250 ms")
    requests = [(CMD_STAGE, payload) for payload in frame.payloads()]
    if clear_first or not requests:
        requests.insert(0, (CMD_CLEAR, b''))
    device.drain()
    for command, payload in requests[:-1]:
        device.send(command, payload, FLAG_NO_REPLY)
    command, payload = requests[-1]
    for attempt in range(2):
        try:
            reply = device.request(command, payload, FLAG_COMMIT_AFTER, timeout=timeout)
        except HostLightingTimeoutError:
            continue
        if reply[2] != STATUS_OK:
            raise device.rejected(command, reply)
        return FrameResult(acknowledged=True, resent=attempt > 0)
    return FrameResult(acknowledged=False, resent=True)
