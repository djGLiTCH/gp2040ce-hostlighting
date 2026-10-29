<!--
gp2040ce-hostlighting (GP2040-CE Host Lighting library and tools)
Filename: README.md
Folder: packaging/linux
https://github.com/djGLiTCH/gp2040ce-hostlighting
SPDX-FileCopyrightText: (C) 2026 Jacob Simpson (DJ GLiTCH)
SPDX-License-Identifier: GPL-3.0-or-later
See LICENSES/GPL-3.0-or-later.txt at the repository root.
-->

# packaging/linux

A Linux executable cannot carry an icon, so `scripts/build-app.sh` puts this
desktop entry and `hlp.png` beside `dist/hlp`. The repository's README, under
"The hlp app", gives the commands that install the three for the logged-in user.

| File | What it does |
|---|---|
| `hlp.desktop` | The desktop entry: it opens `hlp` in a terminal, so that the menu shows, with the icon named `hlp` |
