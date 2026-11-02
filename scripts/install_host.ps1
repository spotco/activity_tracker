param(
    [Parameter(Mandatory = $true)]
    [string]$ExtensionId,

    [ValidateSet("Brave", "Chrome")]
    [string]$Browser = "Brave"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot

function Write-Utf8NoBomFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Content
    )
    $encoding = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

$hostDir = Join-Path $env:LOCALAPPDATA "ActivityTracker\native_host"
New-Item -ItemType Directory -Force -Path $hostDir | Out-Null

$builtHost = Join-Path $repoRoot "dist\activity_tracker_native_host.exe"
if (-not (Test-Path $builtHost)) {
    throw "Native host executable not found at '$builtHost'. Build it first with scripts\build_native_host.ps1"
}
$installedHostExe = Join-Path $hostDir "activity_tracker_native_host.exe"
Copy-Item -Path $builtHost -Destination $installedHostExe -Force

$manifestObject = [ordered]@{
    name = "com.spotco.activity_tracker"
    description = "Activity Tracker native messaging host"
    path = $installedHostExe
    type = "stdio"
    allowed_origins = @("chrome-extension://$ExtensionId/")
}
$manifestJson = $manifestObject | ConvertTo-Json -Depth 4

$manifestPath = Join-Path $hostDir "com.spotco.activity_tracker.json"
Write-Utf8NoBomFile -Path $manifestPath -Content $manifestJson

$manifestBytes = [System.IO.File]::ReadAllBytes($manifestPath)
if ($manifestBytes.Length -ge 3 -and $manifestBytes[0] -eq 0xEF -and $manifestBytes[1] -eq 0xBB -and $manifestBytes[2] -eq 0xBF) {
    throw "Native host manifest was written with a UTF-8 BOM."
}

# This Brave build discovers hosts under Chrome's native-messaging key.
$registryPath = "HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.spotco.activity_tracker"
if ($Browser -eq "Chrome") {
    Write-Warning "The Chrome registry location is what the tested Brave build reads. Chrome itself was not smoke-tested."
}

New-Item -Path $registryPath -Force | Out-Null
Set-ItemProperty -Path $registryPath -Name "(default)" -Value $manifestPath

Write-Host "Installed native host manifest:"
Write-Host "  $manifestPath"
Write-Host "Installed native host executable:"
Write-Host "  $installedHostExe"
Write-Host "Tracker configuration remains in the repository config.json beside the source tree."
Write-Host "Registry:"
Write-Host "  $registryPath"
Write-Host "Allowed extension id: $ExtensionId"
