<!--
gp2040ce-hostlighting (GP2040-CE Host Lighting library and tools)
Filename: README.md
Folder: packaging
https://github.com/djGLiTCH/gp2040ce-hostlighting
SPDX-FileCopyrightText: (C) 2026 Jacob Simpson (DJ GLiTCH)
SPDX-License-Identifier: GPL-3.0-or-later
See LICENSES/GPL-3.0-or-later.txt at the repository root.
-->

# packaging

What the hlp app is built from. `scripts/build-app.ps1` and
`scripts/build-app.sh` build it; the repository's README, under "The hlp app",
says what each build produces.

| File | What it does |
|---|---|
| `hlp.spec` | The PyInstaller spec: `hlp` as one executable, with the licences, and on Windows the icon and the version resource that shows the author, copyright and version in the file's Properties |
| `requirements.txt` | The exact versions the build scripts install: hidapi, PyInstaller and setuptools-scm with what they need, all but macholib on macOS |
| `build-constraints.txt` | The versions of Cython and setuptools that pip builds hidapi with from source on Linux |
| `notices.py` | Writes the app's third-party notices from the libraries a build bundles, into the app and beside it as `dist/THIRD-PARTY-NOTICES.txt`; on Linux it takes each library's Debian package, with its version and copyright file, and lists in `dist/THIRD-PARTY-SOURCES.txt` the source packages with a GPL-family licence named in their copyright file. `hlp.spec` runs it, and a library it cannot place stops the build |
| `third-party.toml` | What `notices.py` needs: hidapi's, OpenSSL's, Zstandard's, the Microsoft runtimes' and Python's entries, and the SHA-256 of each text it fetches (the Apache-2.0 and BSD-3-Clause licences, and Python's `Doc/license.rst` by Python version) |
| [`icons/`](icons/) | The app's icon at every size, for each platform |
| [`macos/`](macos/) | The files `build-app.sh` makes `hlp.app` from on macOS |
| [`linux/`](linux/) | The desktop entry `build-app.sh` puts beside the app on Linux |
