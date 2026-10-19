"""Test ConfigurationCache.

SPDX-FileCopyrightText: © 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import dataclasses
import struct

import pytest

from gp2040ce_hostlighting import protocol as hl
from gp2040ce_hostlighting.cache import ConfigurationCache
from gp2040ce_hostlighting.testing import control_record, light_record


def reads(board) -> list:
    """Each page walk sent: (page, expected token, mask), once for the GET_PAGE requests of one walk."""
    sent = [(request[3], struct.unpack_from('<I', request, 6)[0], struct.unpack_from('<H', request, 10)[0])
            for request in board.sent(hl.CMD_GET_PAGE)]
    return [read for index, read in enumerate(sent) if not index or read != sent[index - 1]]


def test_the_first_refresh_reads_every_kept_page(hlp_fake_device, hlp_fake_board):
    """Test the Summary, then pages 9 to 12 under its token with every group, and what the cache then holds."""
    cache = ConfigurationCache(hlp_fake_device.hello())
    assert cache.summary is None and cache.lights is None and cache.token is None
    assert cache.refresh(hlp_fake_device) == {8, 9, 10, 11, 12}
    token = hlp_fake_board.token
    assert reads(hlp_fake_board) == [(8, 0, 0b11), (9, token, 0b1), (10, token, 0b1111), (11, token, 0b111111),
                                     (12, token, 0b11)]
    assert cache.token == token and cache.summary.led_extent == 19 and cache.counters == (0,) * 8
    assert len(cache.profiles) == 1 and len(cache.controls) == 3 and len(cache.lights) == 2
    assert cache.animations[0].speeds.idle == 5
    assert cache.refresh(hlp_fake_device) == frozenset() and reads(hlp_fake_board)[-1] == (8, 0, 0b11)


def test_a_changed_page_is_read_again_and_lights_by_the_groups_that_change(hlp_fake_device, hlp_fake_board):
    """Test that only the pages with a moved counter are read, and Lights by groups 1-3, merged with the rest."""
    hlp_fake_board.reconfigure(lights=hlp_fake_board.lights)       # the Summary's light total as the page has it
    cache = ConfigurationCache(hlp_fake_device.hello())
    cache.refresh(hlp_fake_device)
    first = cache.lights
    # a profile switch: light 0's owner action changes, its LEDs and cell stay
    hlp_fake_board.reconfigure(lights=[light_record(0, pin=2, action=9, instance=1, instances=1),
                                       hlp_fake_board.lights[1]])
    assert cache.refresh(hlp_fake_device) == {11}
    assert reads(hlp_fake_board)[-1] == (11, hlp_fake_board.token, 0b1110)
    assert cache.lights[0].owner.action == 9 and cache.lights[0].leds == first[0].leds
    assert cache.lights[0].grid == first[0].grid and cache.lights[1] == first[1]
    hlp_fake_board.reconfigure(controls=hlp_fake_board.controls[:2])
    assert cache.refresh(hlp_fake_device) == {8, 10}                # the Summary's control totals changed too
    assert reads(hlp_fake_board)[-1][::2] == (10, 0b1111) and len(cache.controls) == 2


def test_lights_of_another_count_are_read_whole(hlp_fake_device, hlp_fake_board):
    """Test that a Lights page that changed length is read with every group."""
    cache = ConfigurationCache(hlp_fake_device.hello())
    cache.refresh(hlp_fake_device)
    hlp_fake_board.reconfigure(lights=[*hlp_fake_board.lights, light_record(4, kind=1, slot=1)])
    assert cache.refresh(hlp_fake_device) == {8, 11}
    assert [mask for page, _, mask in reads(hlp_fake_board)[-2:]] == [0b1110, 0b111111]
    assert len(cache.lights) == 3 and cache.lights[2].leds.first_led == 4


def test_a_stale_read_starts_again_from_the_summary(hlp_fake_device, hlp_fake_board):
    """Test the restart after a change under the reads, nothing kept from the stale walk, and the limit."""
    cache = ConfigurationCache(hlp_fake_device.hello())
    page = hlp_fake_board._page
    changes = [1]

    def changing(sequence, request):
        if request[3] == hl.PAGE_PROFILES and changes[0]:
            changes[0] -= 1
            hlp_fake_board.token += 1
        return page(sequence, request)
    hlp_fake_board._page = changing
    assert cache.refresh(hlp_fake_device) == {8, 9, 10, 11, 12}
    assert [read[0] for read in reads(hlp_fake_board)] == [8, 9, 8, 9, 10, 11, 12]
    assert cache.token == hlp_fake_board.token
    cache.clear()
    assert cache.summary is None and cache.controls is None and cache.counters is None
    changes[0] = 3
    with pytest.raises(hl.HostLightingError, match='kept changing'):
        cache.refresh(hlp_fake_device)
    assert cache.summary is None


def test_masks_and_pages_choose_what_is_read(hlp_fake_device, hlp_fake_board):
    """Test a mask per page, the Summary's counters read whatever its mask, and the pages kept."""
    hello = hlp_fake_device.hello()
    cache = ConfigurationCache(hello, masks={hl.PAGE_SUMMARY: 0b10, hl.PAGE_CONTROLS: 0b0001})
    cache.pages = frozenset({hl.PAGE_SUMMARY, hl.PAGE_CONTROLS})
    assert cache.refresh(hlp_fake_device) == {10}
    assert [(page, mask) for page, _, mask in reads(hlp_fake_board)] == [(8, 0b10), (10, 0b0001)]
    assert cache.summary is None and not cache.controls[0].has('lights') and cache.lights is None
    unlisted = ConfigurationCache(dataclasses.replace(hello, pages=frozenset({0, 1, 8, 11})))
    assert unlisted.pages == {8, 11}
    with pytest.raises(hl.HostLightingIncompatibleError, match='page 8'):
        ConfigurationCache(dataclasses.replace(hello, pages=frozenset({0, 1})))


def test_a_page_added_to_those_kept_is_read_at_the_next_refresh(hlp_fake_device, hlp_fake_board):
    """Test that a page not yet held is read even though its counter stands."""
    cache = ConfigurationCache(hlp_fake_device.hello())
    cache.pages = frozenset({hl.PAGE_SUMMARY})
    assert cache.refresh(hlp_fake_device) == {8}
    cache.pages = frozenset({hl.PAGE_SUMMARY, hl.PAGE_CONTROLS})
    assert cache.refresh(hlp_fake_device) == {10}
    hlp_fake_board.controls = [control_record(2, 1)]
    assert cache.refresh(hlp_fake_device) == frozenset()       # no counter moved: the board said nothing changed
