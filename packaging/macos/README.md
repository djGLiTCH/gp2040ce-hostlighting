<!--
gp2040ce-hostlighting (GP2040-CE Host Lighting library and tools)
Filename: README.md
Folder: packaging/macos
https://github.com/djGLiTCH/gp2040ce-hostlighting
SPDX-FileCopyrightText: (C) 2026 Jacob Simpson (DJ GLiTCH)
SPDX-License-Identifier: GPL-3.0-or-later
See LICENSES/GPL-3.0-or-later.txt at the repository root.
-->

# packaging/macos

A macOS command-line executable cannot carry an icon, so `scripts/build-app.sh`
also puts the built `hlp` in `hlp.app` with these files, then signs the app ad
hoc:
`Contents/MacOS/hlp-launcher` and `hlp`, `Contents/Resources/hlp.icns` and
`THIRD-PARTY-NOTICES.txt`, and `Contents/Info.plist`.

| File | What it does |
|---|---|
| `Info.plist` | The app's property list: its name, identifier, icon, version and copyright. The build script writes the version's release numbers, such as 1.0.0, in place of `@RELEASE@` |
| `hlp-launcher` | The app's executable: it opens Terminal running `hlp`, so opening the app shows the menu |
