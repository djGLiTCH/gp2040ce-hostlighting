#!/bin/sh
# gp2040ce-hostlighting (GP2040-CE Host Lighting library and tools)
# Filename: fetch-sources.sh
# Folder: scripts
# https://github.com/djGLiTCH/gp2040ce-hostlighting
# SPDX-FileCopyrightText: © 2026 Jacob Simpson (DJ GLiTCH)
# SPDX-License-Identifier: GPL-3.0-or-later
# See LICENSES/GPL-3.0-or-later.txt at the repository root.

# Download the Debian source packages that dist/THIRD-PARTY-SOURCES.txt lists, those of the libraries the Linux app
# bundles with a GPL-family licence named in their copyright file, at the exact versions bundled, and pack them as
# dist/NAME.tar.gz for the release. Run after build-app.sh, on the system that built the app. apt-get source takes
# each from apt's deb-src entries; a version they no longer carry comes from Launchpad, which keeps every published
# version, checked against the SHA-256 sums its .dsc lists.
# Usage: sh scripts/fetch-sources.sh NAME
set -eu
cd "$(dirname "$0")/.."
name=${1:?usage: sh scripts/fetch-sources.sh NAME}
rm -rf build/sources
mkdir -p "build/sources/$name"
cp dist/THIRD-PARTY-NOTICES.txt "build/sources/$name/"
launchpad=https://launchpad.net/ubuntu/+archive/primary/+files
fetch() {
    apt-get source --download-only "$1" && return
    echo "no deb-src for $1: taking it from Launchpad"
    package=${1%%=*}
    version=${1#*=}
    dsc="${package}_${version#*:}.dsc"
    curl -fsSLO "$launchpad/$dsc"
    for file in $(awk '/^Files:/ {f = 1; next} /^[^ ]/ {f = 0} f {print $3}' "$dsc"); do
        curl -fsSLO "$launchpad/$file"
    done
    awk '/^Checksums-Sha256:/ {f = 1; next} /^[^ ]/ {f = 0} f {print $1 "  " $3}' "$dsc" | sha256sum -c -
}
while read -r package; do
    (cd "build/sources/$name" && fetch "$package")
done < dist/THIRD-PARTY-SOURCES.txt
tar -czf "dist/$name.tar.gz" -C build/sources "$name"
echo "dist/$name.tar.gz"
