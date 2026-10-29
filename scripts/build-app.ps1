# gp2040ce-hostlighting (GP2040-CE Host Lighting library and tools)
# Filename: build-app.ps1
# Folder: scripts
# https://github.com/djGLiTCH/gp2040ce-hostlighting
# SPDX-FileCopyrightText: © 2026 Jacob Simpson (DJ GLiTCH)
# SPDX-License-Identifier: GPL-3.0-or-later
# See LICENSES/GPL-3.0-or-later.txt at the repository root.

# Build the hlp app for Windows as dist\hlp.exe, in a virtual environment (build\app-venv) with the versions in
# requirements.txt. Needs Python 3.14 or later, with the py launcher, and git. To build with another Python, delete
# build\app-venv first.
# Usage: powershell -ExecutionPolicy Bypass -File scripts\build-app.ps1 [-PythonVersion 3.14]
param([string]$PythonVersion = '3.14')
$ErrorActionPreference = 'Stop'

function Invoke-Native([string]$Program, [string[]]$Arguments) {
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program $($Arguments -join ' ') exited with $LASTEXITCODE" }
}

Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    $python = 'build\app-venv\Scripts\python.exe'
    if (-not (Test-Path $python)) { Invoke-Native 'py' @("-$PythonVersion", '-m', 'venv', 'build\app-venv') }
    Invoke-Native $python @('-m', 'pip', 'install', '--disable-pip-version-check', '-r', 'packaging\requirements.txt')
    Invoke-Native $python @('-m', 'PyInstaller', '--noconfirm', '--clean', '--distpath', 'dist', '--workpath',
                            'build\pyinstaller', 'packaging\hlp.spec')
    Invoke-Native 'dist\hlp.exe' @('--version')
}
finally {
    Pop-Location
}
