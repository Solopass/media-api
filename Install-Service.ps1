# ================================
# Install YouTube API as a Windows Service
# Requires: nssm (Non-Sucking Service Manager)
# ================================
param(
    [string]$ServiceName = "YouTubeLocalAPI",
    [int]$Port = 8080,
    [string]$Token = ""
)

$scriptPath = Join-Path $PSScriptRoot "YouTubeAPI.ps1"

if (-not (Test-Path $scriptPath)) {
    Write-Host "Error: Cannot find $scriptPath" -ForegroundColor Red
    exit 1
}

if (-not (Get-Command "nssm" -ErrorAction SilentlyContinue)) {
    Write-Host "Error: nssm is not installed. Please install it using 'winget install NSSM.NSSM'." -ForegroundColor Red
    exit 1
}

# The executable is pwsh (PowerShell Core) or powershell
$pwsh = (Get-Command "pwsh" -ErrorAction SilentlyContinue).Source
if (-not $pwsh) {
    $pwsh = "powershell.exe"
}

Write-Host "Installing service $ServiceName..."
$args = "-ExecutionPolicy Bypass -NoProfile -File `"$scriptPath`" -Port $Port"
if ($Token -ne "") {
    $args += " -Token `"$Token`""
}

nssm install $ServiceName $pwsh $args
nssm set $ServiceName AppDirectory $PSScriptRoot
nssm set $ServiceName Description "Local YouTube API Service running on port $Port"
nssm start $ServiceName

Write-Host "Service $ServiceName installed and started successfully!" -ForegroundColor Green
Write-Host "To stop it, run: nssm stop $ServiceName"
Write-Host "To remove it, run: nssm remove $ServiceName confirm"
