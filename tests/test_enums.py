"""Test reading GP2040-CE's enum names from its .proto files.

SPDX-FileCopyrightText: © 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
from gp2040ce_hostlighting.enums import proto_enums, read_enum_names


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
    assert read_enum_names([]) == {} and proto_enums('message Gamepad { int32 a = 1; }') == {}
