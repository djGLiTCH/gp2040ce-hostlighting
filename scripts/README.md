<!--
gp2040ce-hostlighting (GP2040-CE Host Lighting library and tools)
Filename: README.md
Folder: scripts
https://github.com/djGLiTCH/gp2040ce-hostlighting
SPDX-FileCopyrightText: (C) 2026 Jacob Simpson (DJ GLiTCH)
SPDX-License-Identifier: GPL-3.0-or-later
See LICENSES/GPL-3.0-or-later.txt at the repository root.
-->

# scripts

Scripts for the repository itself, run from anywhere in a clone. They are not
part of the package pip installs.

| File | What it does | Run |
|---|---|---|
| `build-app.ps1` | Builds the hlp app for Windows as `dist\hlp.exe`, in a virtual environment with the pinned build requirements | `powershell -ExecutionPolicy Bypass -File scripts\build-app.ps1` |
| `build-app.sh` | Builds the hlp app for macOS or Linux as `dist/hlp`, and on macOS `dist/hlp.app` too | `sh scripts/build-app.sh` |
| `fetch-sources.sh` | After a Linux build, downloads the exact source of the bundled libraries with a GPL-family licence named in their copyright file (`dist/THIRD-PARTY-SOURCES.txt`), with `apt-get source` or else from Launchpad, and packs it as `dist/NAME.tar.gz` | `sh scripts/fetch-sources.sh NAME` |
| `make_icons.py` | Draws the hlp icon into `packaging/icons/` at every size; needs Pillow | `python scripts/make_icons.py` |
| `test_vectors.py` | Writes `tests/vectors/vectors.json` from the model board, or replays the vectors on a board | `python scripts/test_vectors.py [--board ID]` |

The repository's README, under "The hlp app", says what each build produces and
how to install it.
