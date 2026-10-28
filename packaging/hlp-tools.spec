# gp2040ce-hostlighting (GP2040-CE Host Lighting library and tools)
# Filename: hlp-tools.spec
# Folder: packaging
# https://github.com/djGLiTCH/gp2040ce-hostlighting
# SPDX-FileCopyrightText: © 2026 Jacob Simpson (DJ GLiTCH)
# SPDX-License-Identifier: GPL-3.0-or-later
# See LICENSES/GPL-3.0-or-later.txt at the repository root.

"""Build the hlp-* commands as standalone executables with PyInstaller.

One folder holds an executable per command. They share one Python runtime and one copy of the modules. Run from
the repository's root: pyinstaller packaging/hlp-tools.spec
"""
import pathlib
import sys
import tomllib

from PyInstaller.utils.hooks import copy_metadata
from setuptools_scm import get_version

ROOT = pathlib.Path(SPECPATH).parent
WORK = pathlib.Path(workpath)
COMMANDS = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['project']['scripts']
VERSION = get_version(root=str(ROOT))

# A launcher per command, as pip's script wrappers call the entry points
launchers = WORK / 'launchers'
launchers.mkdir(parents=True, exist_ok=True)
for name, target in COMMANDS.items():
    module, function = target.split(':')
    (launchers / f'{name}.py').write_text(f'from {module} import {function}\n\n{function}()\n', encoding='utf-8')

# The package's metadata, so that --version names the version built
metadata = WORK / 'metadata' / f'gp2040ce_hostlighting-{VERSION}.dist-info'
metadata.mkdir(parents=True, exist_ok=True)
(metadata / 'METADATA').write_text(f'Metadata-Version: 2.1\nName: gp2040ce-hostlighting\nVersion: {VERSION}\n',
                                   encoding='utf-8')

# The licences: this package's, Python's with the libraries Python bundles, and hidapi's in its metadata
datas = [(str(metadata), metadata.name), (str(ROOT / 'README.md'), '.'), (str(ROOT / 'LICENSES'), 'LICENSES'),
         (str(pathlib.Path(sys.base_prefix) / 'LICENSE.txt'), 'third-party/python')]
datas += copy_metadata('hidapi')

executables, analyses = [], []
for name in sorted(COMMANDS):
    analysis = Analysis([str(launchers / f'{name}.py')], pathex=[str(ROOT)], datas=datas, noarchive=True)
    analyses.append(analysis)
    executables.append(EXE(PYZ(analysis.pure), analysis.scripts, [], exclude_binaries=True, name=name,
                           console=True, upx=False))

COLLECT(*executables, *[analysis.binaries for analysis in analyses], *[analysis.datas for analysis in analyses],
        name='hlp-tools', upx=False)
