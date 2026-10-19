"""Read GP2040-CE's enum names from its .proto files, to name the values the pages report.

The pages carry GP2040-CE's enum values verbatim: actions, light types, LED formats, input modes and animation
effects among them. Their names come from GP2040-CE's own enums.proto, which a host gives, so the library keeps
no list of its own that could fall behind the firmware.

SPDX-FileCopyrightText: © 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import logging
import pathlib
import re
from typing import Iterable, Union

__all__ = ['parse_enum_names', 'read_enum_names']

logger = logging.getLogger(__name__)

_PROTO_COMMENT = re.compile(r'//[^\n]*|/\*.*?\*/', re.S)
_PROTO_ENUM = re.compile(r'\benum\s+(\w+)\s*\{(.*?)\}', re.S)
_PROTO_VALUE = re.compile(r'^\s*(\w+)\s*=\s*(-?(?:0[xX][0-9a-fA-F]+|\d+))\s*(?:\[[^\]]*\])?\s*;', re.M)


def parse_enum_names(text: str) -> dict:
    """Read the enum blocks of a .proto file.

    :param text: the file's contents
    :return: {enum name: {value: value name}}, with the first name where values share a number
    """
    names: dict = {}
    for enum in _PROTO_ENUM.finditer(_PROTO_COMMENT.sub('', text)):
        values = names.setdefault(enum.group(1), {})
        for value in _PROTO_VALUE.finditer(enum.group(2)):
            values.setdefault(int(value.group(2), 0), value.group(1))
    return names


def read_enum_names(paths: Iterable[Union[str, pathlib.Path]]) -> dict:
    """Read the enum names of GP2040-CE's enums.proto.

    Each path is the file, or the directory that holds it, such as GP2040-CE's proto directory. A directory
    without enums.proto is passed over, and a file that cannot be read is logged as a warning.

    :param paths: the files or directories, later ones overriding earlier ones for an enum they share
    :return: {enum name: {value: value name}}, such as names['GpioAction'][1] == 'BUTTON_PRESS_UP'
    """
    names: dict = {}
    for path in paths:
        file = pathlib.Path(path).expanduser()
        if file.is_dir():
            file = file / 'enums.proto'
            if not file.is_file():
                logger.debug("no enums.proto in %s", path)
                continue
        try:
            names.update(parse_enum_names(file.read_text(encoding='utf-8')))
        except (OSError, UnicodeDecodeError):
            logger.warning("could not read %s for enum names", file)
            logger.debug("reading the .proto file failed", exc_info=True)
    return names
