<!--
gp2040ce-hostlighting (GP2040-CE Host Lighting library and tools)
Filename: README.md
Folder: .github/workflows
https://github.com/djGLiTCH/gp2040ce-hostlighting
SPDX-FileCopyrightText: (C) 2026 Jacob Simpson (DJ GLiTCH)
SPDX-License-Identifier: GPL-3.0-or-later
See LICENSES/GPL-3.0-or-later.txt at the repository root.
-->

# .github/workflows

The repository's GitHub Actions workflows. Each action is pinned to a commit.

| File | What it does |
|---|---|
| `release.yml` | Builds the hlp app on Windows (x64), macOS (x64 and ARM64) and Linux (x64 and ARM64) with `scripts/build-app.*`, checks that each runs, and builds the wheel and sdist. A `v*` tag also makes a draft GitHub release with them, a pre-release for an rc, which the owner writes up and publishes. Run by hand, it keeps the files as the run's artifacts |

A release carries `hlp-windows-x64.exe`, `hlp-macos-x64.zip`,
`hlp-macos-arm64.zip`, `hlp-linux-x64.tar.gz` and `hlp-linux-arm64.tar.gz`, each
platform's `THIRD-PARTY-NOTICES` (inside each archive, and beside the Windows
app), `hlp-linux-x64-sources.tar.gz` and `hlp-linux-arm64-sources.tar.gz` (the
source of the GPL-family libraries the Linux apps bundle), the wheel, the sdist
and `SHA256SUMS.txt`.
