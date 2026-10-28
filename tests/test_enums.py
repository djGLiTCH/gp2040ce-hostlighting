# gp2040ce-hostlighting (GP2040-CE Host Lighting library and tools)
# Filename: test_enums.py
# Folder: tests
# https://github.com/djGLiTCH/gp2040ce-hostlighting
# SPDX-FileCopyrightText: © 2026 Jacob Simpson (DJ GLiTCH)
# SPDX-License-Identifier: GPL-3.0-or-later
# See LICENSES/GPL-3.0-or-later.txt at the repository root.

"""Test reading GP2040-CE's enum names from its .proto files."""
from gp2040ce_hostlighting.enums import parse_enum_names, read_enum_names


def test_read_enum_names_takes_files_and_directories(tmp_path):
    """Test a path given as text or as a directory, a later file overriding an earlier one's enum, and a gap."""
    first, later = tmp_path / 'first', tmp_path / 'later'
    first.mkdir()
    later.mkdir()
    (first / 'enums.proto').write_text('enum LightType { LIGHT_TYPE_ACTION = 0; LIGHT_TYPE_CASE = 1; }\n'
                                       'enum InputMode { INPUT_MODE_XINPUT = 0; }\n', encoding='utf-8')
    (later / 'enums.proto').write_text('enum LightType { LIGHT_TYPE_ACTION = 0; }\n', encoding='utf-8')
    names = read_enum_names([str(first), later, tmp_path / 'absent'])
    assert names == {'LightType': {0: 'LIGHT_TYPE_ACTION'}, 'InputMode': {0: 'INPUT_MODE_XINPUT'}}
    assert read_enum_names([]) == {} and parse_enum_names('message Gamepad { int32 a = 1; }') == {}
