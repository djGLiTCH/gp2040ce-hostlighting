# gp2040ce-hostlighting (GP2040-CE Host Lighting library and tools)
# Filename: notices.py
# Folder: packaging
# https://github.com/djGLiTCH/gp2040ce-hostlighting
# SPDX-FileCopyrightText: © 2026 Jacob Simpson (DJ GLiTCH)
# SPDX-License-Identifier: GPL-3.0-or-later
# See LICENSES/GPL-3.0-or-later.txt at the repository root.

"""Write the hlp app's third-party notices, from the libraries a build bundles. hlp.spec calls it.

Each library is taken, in order, as hidapi's by name; on Linux, as part of the Debian package that installed it, with
the package's version and its copyright file; as a component of third-party.toml by name; or as Python's when it is
one of Python's own modules, with Python's LICENSE.txt and Doc/license.rst. A library none of these covers stops the
build, so that every library the app ships has its notice. On Linux the source packages with a GPL-family licence
named in their copyright file are also listed, for the release to carry their source.
"""
import fnmatch
import hashlib
import pathlib
import subprocess  # nosec B404
import sys
import sysconfig
import tomllib
import urllib.error
import urllib.request
from importlib import metadata
from typing import Any, Iterable, Optional

HERE = pathlib.Path(__file__).parent


def _fetch(url: str, sha256: str, what: str) -> str:
    """Fetch a text and check its SHA-256."""
    try:
        with urllib.request.urlopen(url, timeout=60) as response:      # nosec B310
            data: bytes = response.read()
    except (urllib.error.URLError, TimeoutError) as error:
        raise SystemExit(f"cannot fetch {what} for the third-party notices ({getattr(error, 'reason', error)}); the "
                         "build needs the network") from None
    if hashlib.sha256(data).hexdigest() != sha256:
        raise SystemExit(f"{what} does not match its SHA-256 in packaging/third-party.toml")
    return data.decode('utf-8')


def _named(name: str, components: Iterable[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Find the first component with a pattern that matches a library's file name."""
    base = pathlib.PurePath(name).name.lower()
    return next((component for component in components
                 if any(fnmatch.fnmatchcase(base, pattern) for pattern in component['files'])), None)


# The top folders usrmerge links into /usr: dpkg knows a library by the path its package installed, which on older
# releases is /lib/... and on newer ones /usr/lib/...
USRMERGE = ('lib', 'lib32', 'lib64', 'libx32', 'bin', 'sbin')


def _candidates(source: str) -> list[str]:
    """Give the paths dpkg may know a library by: as given and resolved, each with and without usrmerge's /usr."""
    paths = []
    for path in (pathlib.PurePosixPath(source), pathlib.PurePosixPath(pathlib.Path(source).resolve().as_posix())):
        parts = path.parts
        if len(parts) > 2 and parts[1] == 'usr' and parts[2] in USRMERGE:
            alias: Optional[pathlib.PurePosixPath] = pathlib.PurePosixPath('/', *parts[2:])
        elif len(parts) > 1 and parts[1] in USRMERGE:
            alias = pathlib.PurePosixPath('/usr', *parts[1:])
        else:
            alias = None
        paths += [str(path)] + ([str(alias)] if alias else [])
    return list(dict.fromkeys(paths))


def _debian(source: str) -> Optional[dict[str, str]]:
    """Find the Debian package that installed a file: its name, version, source package and copyright file."""
    for path in _candidates(source):
        try:
            found = subprocess.run(['dpkg', '-S', path], capture_output=True, text=True)  # nosec B603, B607
        except FileNotFoundError:
            raise SystemExit("the Linux build needs a Debian-based system: dpkg names the package of each library "
                             "for the third-party notices") from None
        if found.returncode == 0:
            break
    else:
        return None
    line = found.stdout.strip().splitlines()[-1]          # after any "diversion by" lines
    package = line.rsplit(': ', 1)[0].split(', ')[0]       # "libudev1:amd64: /usr/lib/..." gives "libudev1:amd64"
    query = ['dpkg-query', '-W', '-f', '${Version}\t${source:Package}\t${source:Version}', package]
    fields = subprocess.run(query, capture_output=True, text=True, check=True).stdout.split('\t')  # nosec B603, B607
    copyright_file = pathlib.Path('/usr/share/doc', package.split(':')[0], 'copyright')
    if not copyright_file.is_file():
        raise SystemExit(f"{package} has no {copyright_file} for the third-party notices")
    return {'package': package, 'version': fields[0], 'source': fields[1], 'source_version': fields[2],
            'copyright': copyright_file.read_text(encoding='utf-8', errors='replace')}


def _python_dirs() -> list[pathlib.Path]:
    """Give the folders of Python's own modules: its standard library and, on Windows, its DLLs folder."""
    paths = sysconfig.get_paths()
    folders = {paths['stdlib'], paths['platstdlib'], str(pathlib.Path(sys.base_prefix, 'DLLs'))}
    return [pathlib.Path(folder).resolve() for folder in folders if pathlib.Path(folder).is_dir()]


def _hidapi_texts() -> str:
    """Read hidapi's licence files from its installed metadata."""
    files = [file for file in metadata.distribution('hidapi').files or [] if 'licenses/' in str(file)]
    if not files:
        raise SystemExit("hidapi's installed metadata has no licence files for the third-party notices")
    return '\n\n'.join(f"{file.name}\n\n{file.read_text().strip()}" for file in sorted(files, key=str))


def _sorted(entries: dict[str, list[str]]) -> dict[str, str]:
    """Join each entry's files, sorted and without repeats."""
    return {key: ', '.join(sorted(set(files))) for key, files in entries.items()}


def _place(binaries: Iterable[tuple[str, str, str]],
           config: dict[str, Any]) -> tuple[dict[str, list[str]], dict[str, dict[str, Any]]]:
    """Place each library: as a component of the table, or on Linux as a Debian package.

    :return: the files of each component used, by its name, and the Debian packages, by name
    :raises SystemExit: for a library nothing covers
    """
    first = [component for component in config['component'] if component.get('first')]
    others = [component for component in config['component'] if not component.get('first')]
    python = next(component for component in config['component'] if component.get('python'))
    python_dirs = _python_dirs()
    named: dict[str, list[str]] = {}
    packages: dict[str, dict[str, Any]] = {}
    unmatched = []
    for name, source, _ in binaries:
        base = pathlib.PurePath(name).name
        real = pathlib.Path(source).resolve()
        component = _named(base, first)
        package = _debian(source) if component is None and sys.platform.startswith('linux') else None
        if package is not None:
            packages.setdefault(package['package'], {**package, 'files': []})['files'].append(base)
            continue
        own = any(real.is_relative_to(folder) for folder in python_dirs)
        component = component or _named(base, others) or (python if own else None)
        if component is None:
            unmatched.append(f"{base} ({real})")
        else:
            named.setdefault(component['name'], []).append(base)
    if unmatched:
        raise SystemExit(f"no third-party notice for {', '.join(sorted(unmatched))}: add it to "
                         "packaging/third-party.toml, or on Linux check that dpkg names its package")
    return named, packages


def _texts(component: dict[str, Any], config: dict[str, Any], python_licence: pathlib.Path) -> list[str]:
    """Give the licence texts a component's notice names, each under a heading."""
    def heading(title: str, text: str) -> list[str]:
        """Put a text under a heading."""
        return ['=' * 78, title, '=' * 78, text.strip(), ""]
    lines = []
    if component.get('licence') in config['licences']:
        licence = component['licence']
        lines += heading(f"{component['name']}: {licence}",
                         _fetch(config['spdx'].format(id=licence), config['licences'][licence], f"the {licence} text"))
    if component.get('texts') == 'hidapi':
        lines += heading(f"{component['name']}: its licence files", _hidapi_texts())
    if component.get('python'):
        version = f"{sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}"
        if version not in config['python_licences']:
            raise SystemExit(f"no SHA-256 for Python {version}'s Doc/license.rst in packaging/third-party.toml")
        rst = _fetch(config['python_licence'].format(version=version), config['python_licences'][version],
                     f"Python {version}'s Doc/license.rst")
        lines += heading(f"Python {version}: LICENSE.txt", python_licence.read_text(encoding='utf-8'))
        lines += heading(f"Python {version}: Doc/license.rst", rst)
    return lines


def write(binaries: Iterable[tuple[str, str, str]], path: pathlib.Path, product: str,
          python_licence: pathlib.Path, sources: pathlib.Path) -> None:
    """Write the notices for the libraries PyInstaller bundles.

    :param binaries: PyInstaller's (name in the app, source, kind) for each library
    :param path: the notices file to write
    :param product: names the app and its version in the first line
    :param python_licence: Python's LICENSE.txt, as the app bundles it
    :param sources: the file to list the Debian source packages to carry, as name=version lines (Linux)
    :raises SystemExit: for a library nothing covers, or a text that cannot be fetched or fails its check
    """
    config = tomllib.loads((HERE / 'third-party.toml').read_text(encoding='utf-8'))
    named, packages = _place(binaries, config)
    files = _sorted(named)
    used = [component for component in config['component'] if component['name'] in files]
    debian = sorted(packages.values(), key=lambda package: package['package'])
    carried = sorted({f"{package['source']}={package['source_version']}" for package in debian
                      if 'GPL' in package['copyright']})
    lines = [f"Third-party notices for {product}", "",
             "The app bundles the libraries below, each listed with its licence and source; the licence texts "
             "follow the list. The app itself is GPL-3.0-or-later: hlp --licences prints its licence.", ""]
    for component in used:
        copyright_line = [f"  {component['copyright']}"] if 'copyright' in component else []
        lines += [component['name'], f"  licence: {component['licence']}", *copyright_line,
                  f"  source: {component['source']}", f"  files: {files[component['name']]}", ""]
    for package in debian:
        carry = (" (its source is in the release, in hlp-linux-*-sources.tar.gz)"
                 if f"{package['source']}={package['source_version']}" in carried else "")
        lines += [f"{package['package']} {package['version']}, from the Debian source package {package['source']} "
                  f"{package['source_version']}{carry}", "  licence and copyright: its Debian copyright file, below",
                  f"  files: {', '.join(sorted(set(package['files'])))}", ""]
    for component in used:
        lines += _texts(component, config, python_licence)
    for package in debian:
        lines += ['=' * 78, f"{package['package']} {package['version']}: copyright", '=' * 78,
                  package['copyright'].strip(), ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(lines), encoding='utf-8', newline='\n')
    if carried:
        sources.write_text('\n'.join(carried) + '\n', encoding='utf-8', newline='\n')
