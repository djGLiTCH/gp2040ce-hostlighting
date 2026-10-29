# gp2040ce-hostlighting (GP2040-CE Host Lighting library and tools)
# Filename: test_hlp.py
# Folder: tests
# https://github.com/djGLiTCH/gp2040ce-hostlighting
# SPDX-FileCopyrightText: © 2026 Jacob Simpson (DJ GLiTCH)
# SPDX-License-Identifier: GPL-3.0-or-later
# See LICENSES/GPL-3.0-or-later.txt at the repository root.

"""Test the hlp command."""
import importlib
import importlib.machinery
import io
import logging
import pathlib
import sys
import tomllib
from importlib import metadata

import pytest

from gp2040ce_hostlighting import records
from gp2040ce_hostlighting.tools import commands, hlp


def test_hlp_runs_every_hlp_command():
    """Test that each hlp-* script is an hlp subcommand of the same name, calling the same function."""
    scripts = tomllib.loads((pathlib.Path(__file__).parents[1] / 'pyproject.toml').read_text(encoding='utf-8'))
    scripts = scripts['project']['scripts']
    assert scripts.pop('hlp') == 'gp2040ce_hostlighting.tools.hlp:main'
    assert sorted(f'hlp-{name}' for name in hlp.COMMANDS) == sorted(scripts)
    for name, function in hlp.COMMANDS.items():
        module, attribute = scripts[f'hlp-{name}'].split(':')
        assert getattr(importlib.import_module(module), attribute) is function


def test_hlp_passes_the_arguments_on_unchanged(monkeypatch):
    """Test that the arguments after the command reach it as given, with 'hlp NAME' in place of the first two."""
    seen = []
    monkeypatch.setitem(hlp.COMMANDS, 'set-light', lambda: seen.append(list(sys.argv)))
    monkeypatch.setattr(sys, 'argv', ['hlp', 'set-light', '--by', 'pin', '--', '-10:00FF00', '-d'])
    hlp.main()
    monkeypatch.setattr(sys, 'argv', ['hlp', '--', 'set-light', '-d'])
    hlp.main()
    monkeypatch.setattr(sys, 'argv', ['hlp', '--', 'set-light', '--', '-10:00FF00'])
    hlp.main()
    assert seen == [['hlp set-light', '--by', 'pin', '--', '-10:00FF00', '-d'], ['hlp set-light', '-d'],
                    ['hlp set-light', '--', '-10:00FF00']]


def test_hlp_ping_runs_on_a_board(hlp_run_on_fake_board, capsys):
    """Test hlp ping against the fake board, as hlp-ping."""
    hlp_run_on_fake_board(hlp.main, 'ping')
    assert '100 round trips in' in capsys.readouterr().out


def test_hlp_names_the_subcommand_in_its_help_and_errors(monkeypatch, capsys):
    """Test that a subcommand's help and usage errors name it as hlp NAME, run as pip's Windows launchers run it."""
    monkeypatch.setattr(sys.modules['__main__'], '__spec__', importlib.machinery.ModuleSpec('__main__', None))
    monkeypatch.setattr(sys, 'argv', ['hlp', 'ping', '--help'])
    with pytest.raises(SystemExit, match='^0$'):
        hlp.main()
    assert capsys.readouterr().out.startswith('usage: hlp ping [-h]')
    monkeypatch.setattr(sys, 'argv', ['hlp', 'ping', '-x'])
    with pytest.raises(SystemExit, match='^2$'):
        hlp.main()
    assert 'hlp ping: error: unrecognized arguments: -x' in capsys.readouterr().err


def test_hlp_help_lists_each_command(monkeypatch, capsys):
    """Test that hlp --help lists each command with the first line of its description."""
    monkeypatch.setenv('COLUMNS', '200')
    monkeypatch.setattr(sys, 'argv', ['hlp', '--help', 'ping'])
    with pytest.raises(SystemExit, match='^0$'):
        hlp.main()
    out = capsys.readouterr().out
    assert out.startswith('usage: hlp [-h] [-v] [--licences] COMMAND [ARGUMENT ...]')
    for name, function in hlp.COMMANDS.items():
        assert f'  {name} ' in out and function.__doc__.splitlines()[0] in out


def test_hlp_help_wraps_to_the_terminal(monkeypatch, capsys):
    """Test that the command list wraps to an 80-column terminal, as argparse wraps the rest."""
    monkeypatch.setenv('COLUMNS', '80')
    monkeypatch.setattr(sys, 'argv', ['hlp', '--help'])
    with pytest.raises(SystemExit, match='^0$'):
        hlp.main()
    out = capsys.readouterr().out
    assert max(len(line) for line in out.splitlines()) <= 78
    assert '  buttons      Colour every lit control' in out


def test_hlp_version(monkeypatch, capsys):
    """Test that hlp --version prints what each hlp-* command prints, with the copyright, before a command too."""
    monkeypatch.setenv('COLUMNS', '400')
    monkeypatch.setattr(sys, 'argv', ['hlp', '-v', 'ping'])
    with pytest.raises(SystemExit, match='^0$'):
        hlp.main()
    out = capsys.readouterr().out
    assert out == commands.VERSION_TEXT + '\n' and out.endswith('Jacob Simpson (DJ GLiTCH), GPL-3.0-or-later.\n')


class _Console(io.StringIO):
    """Typed lines, read by input() as from a console. A line ^C is Ctrl-C."""

    def isatty(self):
        """Answer as a console does."""
        return True

    def readline(self, *arguments):
        """Read a line, raising KeyboardInterrupt for ^C."""
        line = super().readline(*arguments)
        if line == '^C\n':
            raise KeyboardInterrupt
        return line


def _number(name):
    """Give a command's number in the menu."""
    return str(list(hlp.COMMANDS).index(name) + 1)


def _board(board_id, label='Fake board'):
    """List one board as list_boards() does."""
    return records.BoardListing(board_id=board_id, board_label=label, firmware_version='v0.7.12', major=2, minor=0)


def _typed(*lines):
    """Lines for input() to read, as typed at a console."""
    return _Console(''.join(f'{line}\n' for line in lines))


def test_hlp_menu_picks_the_board_and_runs_commands(hlp_run_on_fake_board, capsys, monkeypatch):
    """Test the menu on one board: picked alone, then a command by number, run at once, and a whole command."""
    monkeypatch.setenv('COLUMNS', '200')
    monkeypatch.setattr(hlp, 'list_boards', lambda: [_board('433030343237362E')])
    monkeypatch.setattr(sys, 'stdin', _typed(_number('ping'), 'hlp-ping --board-id 4330', 'q'))
    hlp_run_on_fake_board(hlp.main)
    out = capsys.readouterr().out
    assert out.startswith(f'hlp, gp2040ce-hostlighting {commands.version()}\nCopyright 2026 Jacob Simpson')
    assert out.splitlines()[2] == 'https://github.com/djGLiTCH/gp2040ce-hostlighting'
    assert '  1  Fake board  v0.7.12  433030343237362E\nUsing board 1, the only one found.' in out
    listed = f"{_number('ping'):>3}  ping"
    assert f'{listed}  ' in out and 'Check a board' in out and 'board: Fake board (433030343237362E)' in out
    assert f"  examples:  {_number('ping')}    ping    show --seconds 30" in out and 'usage:' not in out
    assert out.count('100 round trips in') == 2 and out.count(listed) == 1     # the list shows once


def test_hlp_menu_reads_each_form_and_reports_each_ending(monkeypatch, capsys):
    """Test the menu's command forms, help, errors, a command's exits, the board choice and the restart check."""
    seen = []

    def record():
        seen.append(list(sys.argv))
        if '--help' in sys.argv:
            raise SystemExit(0)
        raise SystemExit({'fail': 2, 'stop': 130, 'message': 'error: no board'}.get(sys.argv[-1], 0))
    monkeypatch.setitem(hlp.COMMANDS, 'fill', record)
    monkeypatch.setitem(hlp.COMMANDS, 'reboot', record)
    monkeypatch.setitem(hlp.COMMANDS, 'caps', record)
    monkeypatch.setattr(hlp, 'list_boards', lambda: [_board('B1'), _board('B2')])
    monkeypatch.setattr(sys, 'stdin', _typed('2', 'pong', '', 'fill', '?', r'-P C:\proto "two words"', 'fill fail',
                                             'hlp fill stop', f"{_number('fill')} message", 'fill "open', 'b', '1',
                                             'reboot gamepad',
                                             'n', 'reboot --help', 'reboot gamepad', 'y', 'caps', 'm'))
    monkeypatch.setattr(sys, 'argv', ['hlp'])
    hlp.main()
    captured = capsys.readouterr()
    assert seen == [['hlp fill', '--help'], ['hlp fill', '--help'],
                    ['hlp fill', '--board-id', 'B2', '-P', r'C:\proto', 'two words'],
                    ['hlp fill', '--board-id', 'B2', 'fail'], ['hlp fill', '--board-id', 'B2', 'stop'],
                    ['hlp fill', '--board-id', 'B2', 'message'], ['hlp reboot', '--board-id', 'B1', '--help'],
                    ['hlp reboot', '--board-id', 'B1', 'gamepad'], ['hlp caps', '--board-id', 'B1']]
    assert "no command 'pong'" in captured.out and 'hlp fill exited with 2' in captured.out
    assert 'hlp fill stopped' in captured.out and 'error: No closing quotation' in captured.out
    assert 'example: FF0000, or --scope case' in captured.out and 'hlp reboot not run' in captured.out
    assert captured.out.count('hlp reboot restarts the board. y to go on: ') == 2
    assert captured.out.count('  m  this list') == 4     # at the start, for '', after b and for m
    assert captured.err == 'error: no board\n'


def test_hlp_menu_waits_for_a_board(monkeypatch, capsys):
    """Test the menu with no board, then a failed look, then a board found, and a board not chosen."""
    seen = []
    looks = iter([[], hlp.HostLightingError('no HID access'), [_board('B1'), _board('B2')], [_board('B3')]])

    def look():
        found = next(looks)
        if isinstance(found, Exception):
            raise found
        return found
    monkeypatch.setitem(hlp.COMMANDS, 'ping', lambda: seen.append(list(sys.argv)))
    monkeypatch.setattr(hlp, 'list_boards', look)
    monkeypatch.setattr(sys, 'stdin', _typed('ping', '', 'r', 'r', 'x', 'r', 'ping', ''))
    monkeypatch.setattr(sys, 'argv', ['hlp'])
    hlp.main()
    out = capsys.readouterr().out
    assert 'No board found. Connect a board' in out and 'error: no HID access' in out
    assert 'No board chosen. b to choose one.' in out and out.count('board: none chosen') == 4
    assert seen == [['hlp ping'], ['hlp ping', '--board-id', 'B3']]


def test_hlp_menu_cancels_and_carries_on(monkeypatch, capsys):
    """Test the menu's cancels, a command that fails and odd input, then the end of input.

    Ctrl-C and Enter cancel, at the command prompt and in a look for boards too. The odd input is a superscript
    and a 5000-digit number.
    """
    seen = []
    looks = iter([[_board('B1'), _board('B2')], KeyboardInterrupt])

    def record():
        seen.append(list(sys.argv))
        if sys.argv[-1] == 'boom':
            raise RuntimeError('boom')

    def look():
        found = next(looks)
        if found is KeyboardInterrupt:
            raise KeyboardInterrupt
        return found
    for name in ('fill', 'reboot', 'caps'):
        monkeypatch.setitem(hlp.COMMANDS, name, record)
    monkeypatch.setattr(hlp, 'list_boards', look)
    monkeypatch.setattr(sys, 'stdin', _typed('^C', 'fill', '', 'fill', '^C', 'reboot gamepad', '^C', '\u00b2',
                                             'caps boom', '^C', 'caps', 'r', '9' * 5000))
    monkeypatch.setattr(sys, 'argv', ['hlp'])
    hlp.main()
    captured = capsys.readouterr()
    assert captured.out.count('No board chosen.') == 2 and captured.out.count('hlp fill not run') == 2
    assert "no command '9999" in captured.out
    assert 'hlp reboot not run' in captured.out and "no command '\u00b2'" in captured.out
    assert 'RuntimeError: boom' in captured.err and 'hlp caps failed' in captured.out
    assert seen == [['hlp fill', '--help'], ['hlp fill', '--help'], ['hlp caps', 'boom'], ['hlp caps']]


def _bundle(path):
    """Lay out an app bundle's licence and notices under path, and give their texts."""
    (path / 'LICENSES').mkdir()
    (path / 'LICENSES' / 'GPL-3.0-or-later.txt').write_text('GNU GENERAL PUBLIC LICENSE\n', encoding='utf-8')
    (path / 'THIRD-PARTY-NOTICES.txt').write_text('Third-party notices for hlp\n', encoding='utf-8')


def test_hlp_licences_in_the_app_and_from_pip(monkeypatch, capsys, tmp_path):
    """Test hlp --licences from an app bundle, then --licenses from an install without the licence in its metadata."""
    _bundle(tmp_path)
    monkeypatch.setattr(sys, '_MEIPASS', str(tmp_path), raising=False)
    monkeypatch.setattr(sys, 'argv', ['hlp', '--licences'])
    with pytest.raises(SystemExit, match='^0$'):
        hlp.main()
    out = capsys.readouterr().out
    assert out.startswith(commands.COPYRIGHT + '\n\nThis program is free software') and 'WITHOUT ANY WARRANTY' in out
    assert out.endswith('\n\nGNU GENERAL PUBLIC LICENSE\n\nThird-party notices for hlp\n')
    monkeypatch.delattr(sys, '_MEIPASS')

    def not_installed(name):
        raise metadata.PackageNotFoundError(name)
    monkeypatch.setattr(hlp.metadata, 'distribution', not_installed)
    monkeypatch.setattr(sys, 'argv', ['hlp', '--licenses'])
    with pytest.raises(SystemExit, match='^0$'):
        hlp.main()
    out = capsys.readouterr().out
    assert 'https://www.gnu.org/licenses/gpl-3.0.txt' in out and out.endswith('each under its own licence.\n')


def test_hlp_menu_shows_the_licences(monkeypatch, capsys, tmp_path):
    """Test that l, licences or licenses at the menu prints the licence and the notices, then the prompt follows."""
    _bundle(tmp_path)
    monkeypatch.setattr(sys, '_MEIPASS', str(tmp_path), raising=False)
    monkeypatch.setattr(hlp, 'list_boards', lambda: [_board('B1')])
    monkeypatch.setattr(sys, 'stdin', _typed('l', 'licences', 'Licenses', 'q'))
    monkeypatch.setattr(sys, 'argv', ['hlp'])
    hlp.main()
    out = capsys.readouterr().out
    assert '  l  licences' in out and out.count('Third-party notices for hlp\n') == 3
    assert out.count('  m  this list') == 1


def test_hlp_menu_fits_80_columns_and_shows_real_usage(monkeypatch, capsys):
    """Test that the menu wraps to an 80-column terminal, and the usage of a real command that needs arguments."""
    monkeypatch.setenv('COLUMNS', '80')
    monkeypatch.setattr(hlp, 'list_boards', lambda: [_board('B1')])
    monkeypatch.setattr(sys, 'stdin', _typed('fill', '', 'q'))
    monkeypatch.setattr(sys, 'argv', ['hlp'])
    hlp.main()
    out = capsys.readouterr().out
    typed = out.replace('): ', '):\n')     # at a console the typed Enter ends each prompt's line
    assert max(len(line) for line in typed.splitlines()) <= 80
    assert 'usage: hlp fill [-h]' in out and 'example: FF0000' in out and 'hlp fill not run' in out


def test_a_later_command_sets_the_log_level_again(monkeypatch):
    """Test that a command run from the menu with -d logs at DEBUG after one run without it."""
    monkeypatch.setattr(logging.root, 'handlers', [])
    monkeypatch.setattr(logging.root, 'level', logging.root.level)
    commands.device_parser("test").parse_args([])
    handler, = logging.root.handlers
    assert handler.level == logging.root.level == logging.WARNING
    commands.device_parser("test").parse_args(['-d'])
    assert logging.root.handlers == [handler] and handler.level == logging.root.level == logging.DEBUG


@pytest.mark.parametrize('arguments, message', [([], 'the following arguments are required: COMMAND'),
                                                (['pong'], "invalid choice: 'pong'"),
                                                (['-d', 'ping'], 'unrecognized arguments: -d')])
def test_hlp_refuses_a_missing_or_unknown_command(monkeypatch, capsys, arguments, message):
    """Test that hlp exits 2 with a usage error when the first argument is not a command."""
    monkeypatch.setattr(sys, 'stdin', io.StringIO(''))      # not a console, so no menu
    monkeypatch.setattr(sys, 'argv', ['hlp', *arguments])
    with pytest.raises(SystemExit, match='^2$'):
        hlp.main()
    assert message in capsys.readouterr().err
