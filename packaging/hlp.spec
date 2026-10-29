# gp2040ce-hostlighting (GP2040-CE Host Lighting library and tools)
# Filename: hlp.spec
# Folder: packaging
# https://github.com/djGLiTCH/gp2040ce-hostlighting
# SPDX-FileCopyrightText: © 2026 Jacob Simpson (DJ GLiTCH)
# SPDX-License-Identifier: GPL-3.0-or-later
# See LICENSES/GPL-3.0-or-later.txt at the repository root.

"""Build the hlp command as one executable with PyInstaller, for the platform it runs on.

The executable holds Python, the package, the licences and the third-party notices, and runs without Python
installed; the notices are also written beside it, as dist/THIRD-PARTY-NOTICES.txt. The scripts
scripts/build-app.ps1 (Windows) and scripts/build-app.sh (macOS and Linux) run it in a virtual environment with
the pinned requirements. Docstrings are kept (no -OO): hlp --help lists the commands from them.
"""
import importlib.util
import pathlib
import shutil
import sys

from PyInstaller.utils.hooks import copy_metadata
from setuptools_scm import get_version

ROOT = pathlib.Path(SPECPATH).parent
WORK = pathlib.Path(workpath)
VERSION = get_version(root=str(ROOT))

# A launcher, as pip's script wrapper calls the entry point
launcher = WORK / 'hlp.py'
launcher.parent.mkdir(parents=True, exist_ok=True)
launcher.write_text('from gp2040ce_hostlighting.tools.hlp import main\n\nmain()\n', encoding='utf-8')

# The package's metadata, so that --version names the version built
metadata = WORK / 'metadata' / f'gp2040ce_hostlighting-{VERSION}.dist-info'
metadata.mkdir(parents=True, exist_ok=True)
(metadata / 'METADATA').write_text(f'Metadata-Version: 2.1\nName: gp2040ce-hostlighting\nVersion: {VERSION}\n',
                                   encoding='utf-8')

# The licences: this package's, Python's and hidapi's in its metadata. On Windows, Python's LICENSE.txt also covers
# the libraries Python bundles.
candidates = [pathlib.Path(sys.base_prefix) / 'LICENSE.txt',
              pathlib.Path(sys.base_prefix, 'lib', f'python{sys.version_info[0]}.{sys.version_info[1]}', 'LICENSE.txt'),
              pathlib.Path(sys.base_prefix, 'lib', f'python{sys.version_info[0]}.{sys.version_info[1]}t', 'LICENSE.txt')]
python_licence = next((path for path in candidates if path.exists()), None)
if python_licence is None:
    raise SystemExit(f"Python's LICENSE.txt is not in {' or '.join(str(path.parent) for path in candidates)}; "
                     "build with a python.org or actions/setup-python Python, or Debian's or Ubuntu's")
datas = [(str(metadata), metadata.name), (str(ROOT / 'README.md'), '.'), (str(ROOT / 'LICENSES'), 'LICENSES'),
         (str(python_licence), 'third-party/python')]
datas += copy_metadata('hidapi')

# Windows shows the author, copyright and version in the file's Properties, from a version resource
version_resource = None
if sys.platform == 'win32':
    from packaging.version import Version
    from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct, StringTable,
                                                     VarFileInfo, VarStruct, VSVersionInfo)
    numbers = (Version(VERSION).release + (0, 0, 0, 0))[:4]
    strings = {'CompanyName': 'Jacob Simpson (DJ GLiTCH)', 'FileDescription': 'GP2040-CE Host Lighting tools',
               'FileVersion': VERSION, 'InternalName': 'hlp',
               'LegalCopyright': '© 2026 Jacob Simpson (DJ GLiTCH). GPL-3.0-or-later.',
               'OriginalFilename': 'hlp.exe', 'ProductName': 'gp2040ce-hostlighting', 'ProductVersion': VERSION}
    version_resource = VSVersionInfo(
        ffi=FixedFileInfo(filevers=numbers, prodvers=numbers),
        kids=[StringFileInfo([StringTable('040904B0', [StringStruct(key, value) for key, value in strings.items()])]),
              VarFileInfo([VarStruct('Translation', [0x0409, 1200])])])

analysis = Analysis([str(launcher)], pathex=[str(ROOT)], datas=datas)
if sys.platform.startswith('linux'):
    # The GCC runtime is part of every glibc system, so the app uses the system's
    analysis.binaries = [entry for entry in analysis.binaries
                         if not pathlib.PurePath(entry[0]).name.startswith('libgcc_s.so')]

# The notices of the libraries this build bundles (notices.py), in the app and beside it; on Linux also the source
# packages the release carries, in dist/THIRD-PARTY-SOURCES.txt
loader = importlib.util.spec_from_file_location('notices', pathlib.Path(SPECPATH) / 'notices.py')
notices = importlib.util.module_from_spec(loader)
loader.loader.exec_module(notices)
notices_file = WORK / 'THIRD-PARTY-NOTICES.txt'
notices.write(analysis.binaries, notices_file, f'hlp {VERSION}', python_licence,
              pathlib.Path(DISTPATH) / 'THIRD-PARTY-SOURCES.txt')
analysis.datas += [('THIRD-PARTY-NOTICES.txt', str(notices_file), 'DATA')]

# Windows embeds the icon in the executable. A macOS or Linux executable holds none: scripts/build-app.sh puts it in
# hlp.app with hlp.icns on macOS, and beside hlp.desktop and hlp.png on Linux.
icon = str(ROOT / 'packaging' / 'icons' / 'hlp.ico') if sys.platform == 'win32' else None
# On Linux the libraries lose their symbol tables, which setup-python's libpython keeps (32 MB of 60 MB unpacked).
# A macOS strip would break the libraries' signatures, and Windows has none to strip.
EXE(PYZ(analysis.pure), analysis.scripts, analysis.binaries, analysis.datas, name='hlp', console=True, upx=False,
    strip=sys.platform.startswith('linux'), version=version_resource, icon=icon)
shutil.copyfile(notices_file, pathlib.Path(DISTPATH) / 'THIRD-PARTY-NOTICES.txt')
