# gp2040ce-hostlighting (GP2040-CE Host Lighting library and tools)
# Filename: test_licence.py
# Folder: tests
# https://github.com/djGLiTCH/gp2040ce-hostlighting
# SPDX-FileCopyrightText: (C) 2026 Jacob Simpson (DJ GLiTCH)
# SPDX-License-Identifier: GPL-3.0-or-later
# See LICENSES/GPL-3.0-or-later.txt at the repository root.

"""Test the licence files."""
import pathlib

ROOT = pathlib.Path(__file__).parent.parent


def test_licence_copies_match():
    """Test that LICENSE, for GitHub, is LICENSES/GPL-3.0-or-later.txt, for REUSE, line for line."""
    def lines(name):
        """Read a licence file's lines, whatever its line endings."""
        return (ROOT / name).read_text(encoding='ascii').splitlines()
    assert lines('LICENSE') == lines('LICENSES/GPL-3.0-or-later.txt')
    assert lines('LICENSE')[0].strip() == 'GNU GENERAL PUBLIC LICENSE'
