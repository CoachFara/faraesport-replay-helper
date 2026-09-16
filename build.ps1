# Build dist\FaraEsportReplay.exe (standalone, unsigned). Run from replay_helper\:
#   powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path .build-venv)) { python -m venv .build-venv }
.build-venv\Scripts\python.exe -m pip install --quiet --upgrade pyinstaller

.build-venv\Scripts\pyinstaller.exe --onefile --windowed --clean --noconfirm `
    --name FaraEsportReplay `
    --distpath dist --workpath build --specpath build `
    faraesport_link.py

Write-Host "Built $PSScriptRoot\dist\FaraEsportReplay.exe"
