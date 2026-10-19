"""Keep a board's configuration pages, reading again only the pages that changed.

The state token changes whenever anything a configuration page reports changes, and the Summary's change
counters say which pages changed. ConfigurationCache reads the Summary, then each kept page with a moved counter,
under the Summary's token, so that the pages it keeps never mix two states. Lights are read whole once, then
groups 1 to 3 alone, the groups a profile switch changes.

Like the device, a cache belongs to one thread.

SPDX-FileCopyrightText: © 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
from typing import Optional

from gp2040ce_hostlighting.protocol import (PAGE_ANIMATIONS, PAGE_CONTROLS, PAGE_GROUPS, PAGE_LIGHTS, PAGE_PROFILES,
                                            PAGE_SUMMARY, HostLightingDevice, HostLightingError,
                                            HostLightingIncompatibleError, HostLightingStaleTokenError)
from gp2040ce_hostlighting.records import Hello, Summary

__all__ = ['ConfigurationCache']

# the configuration pages 2.0 defines, the Summary first
_CONFIGURATION_PAGES = (PAGE_SUMMARY, PAGE_PROFILES, PAGE_CONTROLS, PAGE_LIGHTS, PAGE_ANIMATIONS)
# the Lights groups a profile switch changes: kind and flags, owner, owner instances
_LIGHTS_CHANGING = 0b1110
_SUMMARY_CHANGES = 0b10


class ConfigurationCache:
    """A board's configuration pages, as last read."""

    def __init__(self, hello: Hello, masks: Optional[dict] = None):
        """Hold nothing read yet.

        :param hello: the board's HELLO reply, its page bitmap saying which pages it has
        :param masks: the field groups read per page, by default every group the library decodes. The Summary's
            change counters are read whatever its mask says
        :raises HostLightingIncompatibleError: for a board that does not list the Summary
        """
        if PAGE_SUMMARY not in hello.pages:
            raise HostLightingIncompatibleError("the board does not list page 8 (Summary), which the cache reads "
                                                "first")
        self.hello = hello
        self.masks = {page: (1 << len(PAGE_GROUPS[page])) - 1 for page in _CONFIGURATION_PAGES}
        self.masks.update(masks or {})
        # the configuration pages to keep. The Summary is read whatever this holds
        self.pages = frozenset(page for page in _CONFIGURATION_PAGES if page in hello.pages)
        self.summary: Optional[Summary] = None
        self.token: Optional[int] = None
        self.counters: Optional[tuple] = None          # the Summary's change counters, pages 8 to 15
        self._tables: dict = {}

    @property
    def profiles(self) -> Optional[tuple]:
        """The page 9 records, or None until read."""
        return self._tables.get(PAGE_PROFILES)

    @property
    def controls(self) -> Optional[tuple]:
        """The page 10 records, or None until read."""
        return self._tables.get(PAGE_CONTROLS)

    @property
    def lights(self) -> Optional[tuple]:
        """The page 11 records, or None until read."""
        return self._tables.get(PAGE_LIGHTS)

    @property
    def animations(self) -> Optional[tuple]:
        """The page 12 records, or None until read."""
        return self._tables.get(PAGE_ANIMATIONS)

    def clear(self) -> None:
        """Drop everything read, as a host does after a replug, reboot or input-mode change."""
        self.summary = self.token = self.counters = None
        self._tables = {}

    def refresh(self, device: HostLightingDevice, attempts: int = 3) -> frozenset:
        """Read the Summary, then each kept page with a moved change counter, or that was never read.

        Each page is read under the Summary's state token. A read the board answers as stale starts again from
        the Summary. Nothing is kept until every read has landed.

        :param device: the opened device
        :param attempts: how many times to start from the Summary before giving up
        :return: the pages read again, with page 8 when the Summary itself changed
        :raises HostLightingError: when the configuration keeps changing under the reads
        """
        for _ in range(attempts):
            read = device.read_record(PAGE_SUMMARY, self.masks[PAGE_SUMMARY] | _SUMMARY_CHANGES)
            record = read.records[0]
            counters = record.changes.counters
            due = [page for page in sorted(self.pages - {PAGE_SUMMARY})
                   if page not in self._tables or self.counters is None
                   or counters[page - PAGE_SUMMARY] != self.counters[page - PAGE_SUMMARY]]
            try:
                tables = {page: self._read(device, page, read.token) for page in due}
            except HostLightingStaleTokenError:
                continue
            summary = record.summary if record.has('summary') else self.summary
            changed = set(due) | ({PAGE_SUMMARY} if summary != self.summary else set())
            self.summary, self.token, self.counters = summary, read.token, counters
            self._tables.update(tables)
            return frozenset(changed)
        raise HostLightingError("the board's configuration kept changing while it was read")

    def _read(self, device: HostLightingDevice, page: int, token: int) -> tuple:
        """Read one page under a token. Lights already held are read again by the groups that can change."""
        mask, held = self.masks[page], self._tables.get(page)
        if page == PAGE_LIGHTS and held is not None and mask & _LIGHTS_CHANGING:
            records = device.read_table(page, mask & _LIGHTS_CHANGING, token).records
            if len(records) == len(held):
                return tuple(old.merged(new) for old, new in zip(held, records))
        return device.read_table(page, mask, token).records
