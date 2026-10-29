# gp2040ce-hostlighting (GP2040-CE Host Lighting library and tools)
# Filename: hlp.py
# Folder: gp2040ce_hostlighting/tools
# https://github.com/djGLiTCH/gp2040ce-hostlighting
# SPDX-FileCopyrightText: (C) 2026 Jacob Simpson (DJ GLiTCH)
# SPDX-License-Identifier: GPL-3.0-or-later
# See LICENSES/GPL-3.0-or-later.txt at the repository root.

"""The hlp command, which runs each hlp-* command as a subcommand: hlp ping runs hlp-ping.

With no arguments at a console, as when the app is opened from a file manager, hlp offers a menu: it picks the
board, then runs each command chosen on it until the user quits.
"""
import argparse
import contextlib
import io
import pathlib
import shlex
import shutil
import sys
import textwrap
import traceback
from importlib import metadata
from typing import Any, Callable, Optional

from gp2040ce_hostlighting.protocol import HostLightingError, list_boards
from gp2040ce_hostlighting.records import BoardListing
from gp2040ce_hostlighting.tools import commands, conformance

COMMANDS: dict[str, Callable[[], None]] = {
    'bench': conformance.bench,
    'buttons': commands.buttons,
    'caps': commands.caps,
    'conformance': conformance.conformance,
    'fill': commands.fill,
    'input-mode': commands.input_mode,
    'ping': commands.ping,
    'reboot': commands.reboot,
    'set-light': commands.set_light,
    'settings': commands.settings,
    'show': commands.show,
    'stream': commands.stream,
    'watch': commands.watch,
}

# The commands that need arguments, with the example the menu shows under the usage line when one is named alone.
# The others run as soon as they are named.
EXAMPLES = {
    'fill': "FF0000, or --scope case 0000FF --seconds 5",
    'input-mode': "0 (caps lists the modes the board accepts)",
    'reboot': "gamepad, webconfig or bootsel",
    'set-light': "0:FF0000 1:00FF00, or --by action 1:FFFFFF",
}

# What the menu shows under the command list, as typed at its prompt
MENU_EXAMPLES = (str(list(COMMANDS).index('ping') + 1), 'ping', 'show --seconds 30', 'fill FF0000',
                 'set-light --by action 1:FFFFFF')

# The commands that restart the board, which the menu asks to confirm
RESTARTS = ('input-mode', 'reboot')

# The GPL's own short notice, as its "How to Apply These Terms" gives it
GPL_NOTICE = ("This program is free software: you can redistribute it and/or modify it under the terms of the GNU "
              "General Public License as published by the Free Software Foundation, either version 3 of the "
              "License, or (at your option) any later version. This program is distributed in the hope that it "
              "will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or "
              "FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License, below, for more details.")


def _licences() -> str:
    """Give hlp's licence and the notices of the libraries it bundles.

    The app holds both, as its build bundled them. A pip install reads the licence text from the package's
    metadata; it bundles no libraries, as pip installs hidapi and Python carries its own, each with its licence.
    """
    bundle = getattr(sys, '_MEIPASS', None)
    if bundle is not None:
        licence: Optional[str] = pathlib.Path(bundle, 'LICENSES', 'GPL-3.0-or-later.txt').read_text(encoding='utf-8')
        notices = pathlib.Path(bundle, 'THIRD-PARTY-NOTICES.txt').read_text(encoding='utf-8')
    else:
        try:
            licence = metadata.distribution('gp2040ce-hostlighting').read_text('licenses/LICENSES/GPL-3.0-or-later.txt')
        except metadata.PackageNotFoundError:
            licence = None
        notices = ("Installed with pip, hlp bundles no libraries: pip installs hidapi, and Python carries its own, "
                   "each under its own licence.")
    licence = licence or "The licence text is at https://www.gnu.org/licenses/gpl-3.0.txt"
    return '\n\n'.join([commands.COPYRIGHT, textwrap.fill(GPL_NOTICE, 78), licence.strip(), notices.strip()]) + '\n'


class _Licences(argparse.Action):
    """Print the licence and the third-party notices, then exit, as --version does."""

    def __call__(self, parser: argparse.ArgumentParser, namespace: argparse.Namespace, values: Any,
                 option_string: Optional[str] = None) -> None:
        """Print them and exit."""
        print(_licences(), end='')
        parser.exit()


def _description(name: str) -> str:
    """Name what a command does: the first line of its docstring."""
    return (COMMANDS[name].__doc__ or '').partition('\n')[0]


def _listed(label: str, text: str, width: int, indent: str = '  ') -> str:
    """Wrap one entry of a list to the terminal, as argparse wraps: the label, then its text in a column."""
    columns = max(shutil.get_terminal_size().columns - 2, width + 24)
    return textwrap.fill(f"{label:<{width}}  {text}", columns, initial_indent=indent,
                         subsequent_indent=' ' * (len(indent) + width + 2), break_on_hyphens=False)


def _command_list() -> str:
    """List each command with its description, for hlp --help."""
    width = max(len(name) for name in COMMANDS)
    lines = [_listed(name, _description(name), width) for name in COMMANDS]
    notes = textwrap.fill("A command's options go after its name. hlp COMMAND --help lists them. With no arguments "
                          "at a console, hlp offers a menu.", max(shutil.get_terminal_size().columns - 2, 40))
    return "commands:\n" + "\n".join(lines) + "\n\n" + notes


def _split(text: str) -> list[str]:
    """Split a line typed at the menu into arguments, quotes grouping words and backslashes kept, as in paths."""
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split = True
    lexer.escape = ''
    return list(lexer)


def _ask(prompt: str) -> Optional[str]:
    """Ask at the menu. Ctrl-C answers None, which cancels the question.

    :raises EOFError: at the end of input, which ends the menu
    """
    try:
        return input(prompt).strip()
    except KeyboardInterrupt:
        print()
        return None


def _run(name: str, arguments: list[str]) -> None:
    """Run one command for the menu, reporting an exit other than 0, and an error with its traceback."""
    sys.argv[:] = [f'hlp {name}', *arguments]
    try:
        COMMANDS[name]()
    except SystemExit as error:
        if error.code == 130:
            print(f"hlp {name} stopped")
        elif isinstance(error.code, str):
            print(error.code, file=sys.stderr)
        elif error.code:
            print(f"hlp {name} exited with {error.code}")
    except Exception:       # noqa: B902 the menu stays open, so the traceback can be read
        traceback.print_exc()
        print(f"hlp {name} failed")


def _usage(name: str) -> str:
    """Give a command's usage lines, from its help."""
    text = io.StringIO()
    with contextlib.redirect_stdout(text):
        _run(name, ['--help'])
    return text.getvalue().partition('\n\n')[0]


def _number(text: str, count: int) -> Optional[int]:
    """Read a choice from 1 to count, as an index from 0, or None."""
    return int(text) - 1 if text.isdecimal() and len(text) <= 4 and 1 <= int(text) <= count else None


def _choose_board() -> Optional[BoardListing]:
    """List the connected boards and pick one: the only one, or the one asked for.

    :return: the board, or None when none is found or none is chosen, or Ctrl-C stops the look
    """
    try:
        boards = list_boards()
    except (HostLightingError, OSError) as error:
        print(f"error: {error}")
        return None
    except KeyboardInterrupt:
        print("\nNo board chosen. b to choose one.")
        return None
    if not boards:
        print("\nNo board found. Connect a board with the Host Lighting add-on enabled, then r to look again.")
        return None
    print("\nboards:")
    for number, board in enumerate(boards, 1):
        print(f"{number:>3}  {board.board_label or '?'}  {board.firmware_version or '?'}  {board.board_id}")
    if len(boards) == 1:
        print("Using board 1, the only one found.")
        return boards[0]
    index = _number(_ask("board (number): ") or '', len(boards))
    if index is None:
        print("No board chosen. b to choose one.")
        return None
    return boards[index]


def _show_commands(board: Optional[BoardListing]) -> None:
    """List the commands by number, with examples, the board they run on and the menu's letters."""
    width = max(len(name) for name in COMMANDS) + 5
    print()
    for number, name in enumerate(COMMANDS, 1):
        print(_listed(f"{number:>3}  {name}", _description(name), width, indent=''))
    examples = '    '.join(example.replace(' ', '\N{NO-BREAK SPACE}') for example in MENU_EXAMPLES)
    print('\n' + _listed('examples:', examples, 9).replace('\N{NO-BREAK SPACE}', ' '))     # each wraps whole
    chosen = f"{board.board_label or '?'} ({board.board_id})" if board else "none chosen"
    print(f"\n  board: {chosen}")
    print("  m  this list    b  another board    r  look again    l  licences    q  quit")


def _command(line: str) -> Optional[tuple[str, list[str]]]:
    """Read a command typed at the menu: a number, a name, hlp NAME or hlp-NAME, with or without arguments.

    :return: the command's name and arguments, or None when no command is named
    """
    try:
        words = _split(line)
    except ValueError as error:
        print(f"error: {error}")
        return None
    if words[:1] == ['hlp']:
        words = words[1:]
    if not words:
        return None
    name = words[0].removeprefix('hlp-')
    if (index := _number(name, len(COMMANDS))) is not None:
        name = list(COMMANDS)[index]
    if name not in COMMANDS:
        print(f"no command {words[0]!r}")
        return None
    return name, words[1:]


def _ask_arguments(name: str) -> Optional[list[str]]:
    """Show the usage and an example of a command that needs arguments, then ask for them, showing its help for '?'.

    :return: the arguments, or None when none are given or they cannot be split
    """
    print(f"{_usage(name)}\nexample: {EXAMPLES[name]}")
    while (text := _ask("arguments (? for its help, Enter to cancel): ")) == '?':
        _run(name, ['--help'])
    if not text:
        print(f"hlp {name} not run")
        return None
    try:
        return _split(text)
    except ValueError as error:
        print(f"error: {error}")
        return None


def _run_on_board(name: str, arguments: list[str], board: Optional[BoardListing]) -> None:
    """Run a command from the menu on the chosen board, once confirmed if it restarts the board."""
    if name in RESTARTS and not {'-h', '--help'} & set(arguments):
        if (_ask(f"hlp {name} restarts the board. y to go on: ") or '').lower() != 'y':
            print(f"hlp {name} not run")
            return
    if board and '--board-id' not in arguments:
        arguments = ['--board-id', board.board_id, *arguments]
    _run(name, arguments)


# The menu's letters other than q, and an empty line
LETTERS = ('', 'm', 'b', 'r', 'l')


def _letter(letter: str, board: Optional[BoardListing]) -> tuple[Optional[BoardListing], bool]:
    """Act on one of LETTERS.

    :return: the board, which b and r choose again, and whether the command list shows next
    """
    if letter == 'l':
        print(f"\n{_licences()}", end='')
        return board, False
    if letter in ('b', 'r'):
        return _choose_board(), True
    return board, True


def _menu() -> None:
    """Pick the board, then run each command chosen on it, until the user quits.

    The command list shows at the start, after a board is chosen and for m or an empty line. After a command the
    next prompt follows its results. Ctrl-C cancels a question, the command prompt's too, so a late Ctrl-C after a
    command stops leaves the menu open; q or the end of input quits.
    """
    print(f"hlp, gp2040ce-hostlighting {commands.version()}\n{commands.COPYRIGHT}\n"
          "https://github.com/djGLiTCH/gp2040ce-hostlighting")
    try:
        board, listing = _choose_board(), True
        while True:
            if listing:
                _show_commands(board)
            listing = False
            line = _ask("\ncommand (number, name or a whole command; m for the list, q to quit): ")
            if line is None:
                continue
            if line.lower() in ('q', 'quit', 'exit'):
                return
            letter = 'l' if line.lower() in ('licences', 'licenses') else line.lower()
            if letter in LETTERS:
                board, listing = _letter(letter, board)
                continue
            chosen = _command(line)
            if chosen is None:
                continue
            name, arguments = chosen
            asked = arguments or (_ask_arguments(name) if name in EXAMPLES else [])
            if asked is not None:
                _run_on_board(name, asked, board)
    except EOFError:
        print()


@commands.hlp_command
def main() -> None:
    """Run the command the first argument names, with the arguments that follow it unchanged."""
    arguments = sys.argv[1:]
    if not arguments and sys.stdin is not None and sys.stdin.isatty():
        _menu()
        return
    if not arguments or arguments[0] not in COMMANDS:
        parser = argparse.ArgumentParser(prog='hlp', description="Talk to a GP2040-CE board's Host Lighting "
                                         "interface.", epilog=_command_list(),
                                         formatter_class=argparse.RawDescriptionHelpFormatter)
        parser.add_argument('-v', '--version', action='version', version=commands.VERSION_TEXT,
                            help="show version information and exit")
        parser.add_argument('--licences', '--licenses', action=_Licences, nargs=0,
                            help="show the licence and the notices of the libraries the app bundles, and exit")
        parser.add_argument('command', metavar='COMMAND', choices=list(COMMANDS), help="one of the commands below")
        parser.add_argument('arguments', metavar='ARGUMENT', nargs='*', help="the command's arguments")
        parsed = parser.parse_args(arguments)       # exits for --help, --version and a usage error
        arguments = [parsed.command, *parsed.arguments]
    sys.argv[:] = [f'hlp {arguments[0]}', *arguments[1:]]   # the command's usage and errors name it as hlp NAME
    COMMANDS[arguments[0]]()
