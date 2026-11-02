param(
    [ValidateSet("Brave", "Chrome", "Both")]
    [string]$Browser = "Both"
)

$ErrorActionPreference = "Stop"

$paths = @()
if ($Browser -in @("Brave", "Both")) {
    $paths += "HKCU:\Software\BraveSoftware\Brave-Browser\NativeMessagingHosts\com.spotco.activity_tracker"
    $paths += "HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.spotco.activity_tracker"
}
if ($Browser -eq "Chrome") {
    $paths += "HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.spotco.activity_tracker"
}

foreach ($path in $paths) {
    if (Test-Path $path) {
        Remove-Item -Path $path -Recurse -Force
        Write-Host "Removed registry key $path"
    }
}

$hostDir = Join-Path $env:LOCALAPPDATA "ActivityTracker\native_host"
if (Test-Path $hostDir) {
    Remove-Item -Path $hostDir -Recurse -Force
    Write-Host "Removed $hostDir"
}
