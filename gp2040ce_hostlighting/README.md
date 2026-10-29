<!--
gp2040ce-hostlighting (GP2040-CE Host Lighting library and tools)
Filename: README.md
Folder: gp2040ce_hostlighting
https://github.com/djGLiTCH/gp2040ce-hostlighting
SPDX-FileCopyrightText: (C) 2026 Jacob Simpson (DJ GLiTCH)
SPDX-License-Identifier: GPL-3.0-or-later
See LICENSES/GPL-3.0-or-later.txt at the repository root.
-->

# gp2040ce_hostlighting

The library, imported as `gp2040ce_hostlighting`. The repository's README shows
how a host uses it; the commands are in [`tools/`](tools/).

| File | What it does |
|---|---|
| `__init__.py` | Marks the package; it imports nothing, so importing the package changes nothing in the program that imports it |
| `protocol.py` | The protocol: constants, framing, the decoders, the errors, the device (`HostLightingDevice`), board discovery and the capability checks |
| `records.py` | The records and field groups every decoder returns |
| `claim.py` | `LightsClaim`, which takes, renews, follows and gives up the claim on a board's lights |
| `frames.py` | `Frame` and `publish_frame()`, the frames a streaming host sends |
| `cache.py` | `ConfigurationCache`, the configuration pages, read again only when they change |
| `session.py` | `Session`, the specification's whole host flow: handshake, claim, settings, events, cache, frames and shutdown |
| `runner.py` | `SessionRunner` and `AsyncSession`, a session on a thread of its own, and `BoardWatcher` and `SessionGroup`, every board on the bus |
| `layout.py` | `lights_by_cell()` and `Canvas`, the LED layout grid |
| `enums.py` | GP2040-CE's enum names, read from its `.proto` files |
| `conformance.py` | The conformance checks, and the round trip and frame-rate measurements |
| `testing.py` | Fake boards, a model board and report builders, to test host software without a board |
| `py.typed` | Tells type checkers the package carries type hints |
