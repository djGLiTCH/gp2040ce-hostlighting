# gp2040ce-hostlighting (GP2040-CE Host Lighting library and tools)
# Filename: test_cache.py
# Folder: tests
# https://github.com/djGLiTCH/gp2040ce-hostlighting
# SPDX-FileCopyrightText: (C) 2026 Jacob Simpson (DJ GLiTCH)
# SPDX-License-Identifier: GPL-3.0-or-later
# See LICENSES/GPL-3.0-or-later.txt at the repository root.

"""Test ConfigurationCache."""
import dataclasses
import struct

import pytest

from gp2040ce_hostlighting import protocol as hl
from gp2040ce_hostlighting.cache import ConfigurationCache
from gp2040ce_hostlighting.testing import ModelBoard, control_record, fake_device, light_record, summary_record

# the configuration pages, which a refresh reads first
EVERY_PAGE = {hl.PAGE_SUMMARY, hl.PAGE_PROFILES, hl.PAGE_CONTROLS, hl.PAGE_KEYBOARD_MAPPING, hl.PAGE_LIGHTS,
              hl.PAGE_ANIMATIONS}


def reads(board) -> list:
    """Each page walk sent: (page, expected token, mask), once for the GET_PAGE requests of one walk."""
    sent = [(request[3], struct.unpack_from('<I', request, 6)[0], struct.unpack_from('<H', request, 10)[0])
            for request in board.sent(hl.CMD_GET_PAGE)]
    return [read for index, read in enumerate(sent) if not index or read != sent[index - 1]]


def test_the_first_refresh_reads_every_kept_page(hlp_fake_device, hlp_fake_board):
    """Test the Summary, then pages 9 to 13 under its token with every group, and what the cache then holds."""
    cache = ConfigurationCache(hlp_fake_device.hello())
    assert cache.summary is None and cache.lights is None and cache.token is None
    assert cache.refresh(hlp_fake_device) == EVERY_PAGE
    token = hlp_fake_board.token
    assert reads(hlp_fake_board) == [(hl.PAGE_SUMMARY, 0, 0b11), (hl.PAGE_PROFILES, token, 0b1),
                                     (hl.PAGE_CONTROLS, token, 0b1111), (hl.PAGE_KEYBOARD_MAPPING, token, 0b1),
                                     (hl.PAGE_LIGHTS, token, 0b111111), (hl.PAGE_ANIMATIONS, token, 0b11)]
    assert cache.token == token and cache.summary.led_extent == 19 and cache.counters == (0,) * 8
    assert len(cache.profiles) == 1 and len(cache.controls) == 3 and len(cache.lights) == 2
    assert len(cache.keyboard_mapping) == len(hlp_fake_board.keyboard_actions)
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
    assert cache.refresh(hlp_fake_device) == {hl.PAGE_LIGHTS}
    assert reads(hlp_fake_board)[-1] == (hl.PAGE_LIGHTS, hlp_fake_board.token, 0b1110)
    assert cache.lights[0].owner.action == 9 and cache.lights[0].leds == first[0].leds
    assert cache.lights[0].grid == first[0].grid and cache.lights[1] == first[1]
    hlp_fake_board.reconfigure(controls=hlp_fake_board.controls[:2])
    # the Summary's control totals changed too
    assert cache.refresh(hlp_fake_device) == {hl.PAGE_SUMMARY, hl.PAGE_CONTROLS}
    assert reads(hlp_fake_board)[-1][::2] == (hl.PAGE_CONTROLS, 0b1111) and len(cache.controls) == 2


def test_lights_of_another_count_are_read_whole(hlp_fake_device, hlp_fake_board):
    """Test that a Lights page that changed length is read with every group."""
    cache = ConfigurationCache(hlp_fake_device.hello())
    cache.refresh(hlp_fake_device)
    hlp_fake_board.reconfigure(lights=[*hlp_fake_board.lights, light_record(4, kind=1, slot=1)])
    assert cache.refresh(hlp_fake_device) == {hl.PAGE_SUMMARY, hl.PAGE_LIGHTS}
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
    assert cache.refresh(hlp_fake_device) == EVERY_PAGE
    assert [read[0] for read in reads(hlp_fake_board)] == [hl.PAGE_SUMMARY, hl.PAGE_PROFILES] + sorted(EVERY_PAGE)
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


def test_a_mask_of_0_is_every_group_and_an_undecoded_page_is_refused(hlp_fake_device, hlp_fake_board):
    """Test that masks of 0 read every group, Lights again by groups 1-3, and that page 14 cannot be kept."""
    cache = ConfigurationCache(hlp_fake_device.hello(), {hl.PAGE_SUMMARY: 0, hl.PAGE_LIGHTS: 0})
    cache.refresh(hlp_fake_device)
    assert cache.summary is not None and cache.masks[hl.PAGE_LIGHTS] == 0b111111
    hlp_fake_board.reconfigure(lights=hlp_fake_board.lights)
    cache.refresh(hlp_fake_device)
    assert reads(hlp_fake_board)[-1][2] == 0b1110 and cache.moved == {hl.PAGE_LIGHTS}
    cache.pages = cache.pages | {14}
    with pytest.raises(ValueError, match='not 14'):
        cache.refresh(hlp_fake_device)


def test_the_lights_an_entry_reaches_and_the_controls_by_key_or_action(hlp_fake_device, hlp_fake_board):
    """Test lights_reached(): by ordinal, pin, action and kind, a light at the limit left out; and controls_with()."""
    cache = ConfigurationCache(hlp_fake_device.hello())
    assert cache.lights_reached(hl.ADDRESSING_PIN, 2) is None and cache.controls_with(action=1) is None
    hlp_fake_board.reconfigure(lights=hlp_fake_board.lights + [light_record(100, pin=2, action=1)])
    cache.refresh(hlp_fake_device)
    assert cache.lights_reached(hl.ADDRESSING_PIN, 2) == (0,) and cache.lights_reached(hl.ADDRESSING_ACTION, 1) == (0,)
    assert cache.lights_reached(hl.ADDRESSING_KIND, 1) == (1,) and cache.lights_reached(hl.ADDRESSING_LIGHT, 1) == (1,)
    assert cache.lights_reached(hl.ADDRESSING_LIGHT, 2) == () and cache.lights_reached(hl.ADDRESSING_PIN, 3) == ()
    with pytest.raises(ValueError, match='a run'):
        cache.lights_reached(hl.ADDRESSING_LED_RUN, 0)
    assert [control.input.key for control in cache.controls_with(action=1)] == [2]
    assert cache.controls_with(key=12)[0].input.action == 13 and cache.controls_with(key=12, action=1) == ()


def test_the_keyboard_code_of_an_action_follows_the_mapping(hlp_fake_device):
    """Test keyboard_code(): None until read, the code of the record with the action, None for an action without one.

    A changed code is read again by page 11's counter alone.
    """
    board = ModelBoard()
    device = fake_device(board)
    cache = ConfigurationCache(device.hello())
    b1 = board.keyboard_actions.index(5)            # BUTTON_PRESS_B1's record
    assert cache.keyboard_code(5) is None
    cache.refresh(device)
    assert cache.keyboard_code(5) == board.keyboard_codes[1][b1] and cache.keyboard_code(55) is None
    device.set_keyboard_mapping(1, b1, [0x04])
    assert cache.refresh(device) == {hl.PAGE_KEYBOARD_MAPPING} and cache.moved == {hl.PAGE_KEYBOARD_MAPPING}
    assert cache.keyboard_code(5) == 0x04
    board.later_groups[hl.PAGE_KEYBOARD_MAPPING] = bytes([1])     # a group newer firmware adds, read alone
    unread = ConfigurationCache(device.hello(), masks={hl.PAGE_KEYBOARD_MAPPING: 0b10})
    unread.refresh(device)
    assert unread.keyboard_mapping and unread.keyboard_code(5) is None


def test_the_summary_gives_the_brightness_scale_and_a_stream_rate(hlp_fake_device, hlp_fake_board):
    """Test brightness_scale() as the specification gives it, and stream_rate() from the render rate."""
    hlp_fake_board.summary = summary_record(brightness_maximum=99, brightness_steps=10, render_hz=40)
    summary = hlp_fake_device.read_record(hl.PAGE_SUMMARY).records[0].summary
    assert summary.brightness_scale(10) == 90 / 255 and summary.brightness_scale(0) == 0
    assert summary.stream_rate() == 40.0 and summary.stream_rate(cap=30) == 30.0
    hlp_fake_board.summary = summary_record(brightness_steps=0, render_hz=0)
    summary = hlp_fake_device.read_record(hl.PAGE_SUMMARY).records[0].summary
    assert summary.brightness_scale(5) == 0.0 and summary.stream_rate() is None
