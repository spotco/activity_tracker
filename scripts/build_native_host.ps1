$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot

try {
    if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
        throw "Python launcher 'py' was not found."
    }
    py -3.11 -m pip install --upgrade pyinstaller
    if (-not (Test-Path "dist")) {
        New-Item -ItemType Directory -Path "dist" | Out-Null
    }
    py -3.11 -m PyInstaller `
        --onefile `
        --name activity_tracker_native_host `
        --distpath dist `
        --workpath build\native_host `
        --specpath build\native_host `
        browser_native_host.py
    Write-Host "Built dist\activity_tracker_native_host.exe"
} finally {
    Pop-Location
}
