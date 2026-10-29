#!/bin/sh
# gp2040ce-hostlighting (GP2040-CE Host Lighting library and tools)
# Filename: build-app.sh
# Folder: scripts
# https://github.com/djGLiTCH/gp2040ce-hostlighting
# SPDX-FileCopyrightText: © 2026 Jacob Simpson (DJ GLiTCH)
# SPDX-License-Identifier: GPL-3.0-or-later
# See LICENSES/GPL-3.0-or-later.txt at the repository root.

# Build the hlp app for macOS or Linux as dist/hlp, in a virtual environment (build/app-venv) with the versions
# in requirements.txt. On macOS it also makes dist/hlp.app, which carries the icon and opens hlp in Terminal, signed
# ad hoc; on Linux it puts hlp.desktop and hlp.png beside dist/hlp. Needs Python 3.14 or later and git. On Linux it
# builds hidapi from source against the system's libudev and libusb, so that each library the app bundles comes
# from a Debian package the notices can name; that needs a C compiler, binutils and the libudev and libusb
# development packages (on Debian and Ubuntu: build-essential libudev-dev libusb-1.0-0-dev). The app runs on the
# processor it was built on. To build with another Python, delete build/app-venv first.
# Usage: sh scripts/build-app.sh [python, default python3.14]
set -eu
cd "$(dirname "$0")/.."
python=${1:-python3.14}
[ -x build/app-venv/bin/python ] || "$python" -m venv build/app-venv
source_build=
if [ "$(uname -s)" = Linux ]; then
    source_build='--no-binary hidapi'
    PIP_CONSTRAINT="$(pwd)/packaging/build-constraints.txt"     # pins what pip builds hidapi with
    export PIP_CONSTRAINT
fi
# shellcheck disable=SC2086
build/app-venv/bin/python -m pip install --disable-pip-version-check $source_build -r packaging/requirements.txt
build/app-venv/bin/python -m PyInstaller --noconfirm --clean --distpath dist --workpath build/pyinstaller \
    packaging/hlp.spec
version=$(dist/hlp --version | sed -n 's/^gp2040ce-hostlighting \([^ ]*\) .*/\1/p')
case "$version" in
[0-9]*) echo "dist/hlp $version" ;;
*) echo "no version from dist/hlp --version" >&2; exit 1 ;;
esac
case "$(uname -s)" in
Darwin)
    release=$(echo "$version" | sed -e 's/[^0-9.].*//' -e 's/\.$//')
    rm -rf dist/hlp.app
    mkdir -p dist/hlp.app/Contents/MacOS dist/hlp.app/Contents/Resources
    cp dist/hlp packaging/macos/hlp-launcher dist/hlp.app/Contents/MacOS/
    chmod +x dist/hlp.app/Contents/MacOS/hlp-launcher
    cp packaging/icons/hlp.icns dist/THIRD-PARTY-NOTICES.txt dist/hlp.app/Contents/Resources/
    sed "s/@RELEASE@/$release/" packaging/macos/Info.plist > dist/hlp.app/Contents/Info.plist
    codesign --force --sign - dist/hlp.app
    echo "dist/hlp.app $release"
    ;;
Linux)
    cp packaging/linux/hlp.desktop packaging/icons/hlp.png dist/
    echo "dist/hlp.desktop, dist/hlp.png"
    ;;
esac
