$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv-build\Scripts\python.exe')) {
    python -m venv .venv-build
    if ($LASTEXITCODE -ne 0) { throw 'Could not create build environment.' }
}
& '.\.venv-build\Scripts\python.exe' -m pip install -r requirements-build.txt
if ($LASTEXITCODE -ne 0) { throw 'Could not install build dependencies.' }
& '.\.venv-build\Scripts\python.exe' -m PyInstaller --noconfirm --clean --onefile --windowed --icon assets\logo.ico --add-data "assets/*.ico;assets" --name CQU-Net-Switch tray.py
if ($LASTEXITCODE -ne 0) { throw 'Build failed.' }
Get-Item -LiteralPath 'dist\CQU-Net-Switch.exe' | Select-Object FullName, Length
