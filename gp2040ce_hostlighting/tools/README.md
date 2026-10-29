<!--
gp2040ce-hostlighting (GP2040-CE Host Lighting library and tools)
Filename: README.md
Folder: gp2040ce_hostlighting/tools
https://github.com/djGLiTCH/gp2040ce-hostlighting
SPDX-FileCopyrightText: (C) 2026 Jacob Simpson (DJ GLiTCH)
SPDX-License-Identifier: GPL-3.0-or-later
See LICENSES/GPL-3.0-or-later.txt at the repository root.
-->

# gp2040ce_hostlighting.tools

The commands pip installs, built on the library. Each `hlp-*` command is also an
`hlp` subcommand (`hlp ping` runs `hlp-ping`), and the hlp app is `hlp` built as
one executable. The repository's README describes each command.

| File | What it does |
|---|---|
| `__init__.py` | Marks the package |
| `commands.py` | `hlp-ping`, `hlp-caps`, `hlp-settings`, `hlp-watch`, `hlp-fill`, `hlp-set-light`, `hlp-stream`, `hlp-show`, `hlp-buttons`, `hlp-input-mode` and `hlp-reboot`, and what every command shares: the common options, the argument parser and the error handling |
| `conformance.py` | `hlp-conformance` and `hlp-bench`, built on the library's conformance checks |
| `hlp.py` | `hlp`, which runs each command as a subcommand, and with no arguments at a console its menu |
