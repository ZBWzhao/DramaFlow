$ErrorActionPreference = "Stop"

Write-Host "[1/4] Installing Redis and PostgreSQL in Ubuntu WSL..." -ForegroundColor Cyan
Write-Host "Ubuntu may ask for your Linux sudo password." -ForegroundColor Yellow
wsl -d Ubuntu -- bash -lc 'sudo apt-get update && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y redis-server postgresql postgresql-contrib'
if ($LASTEXITCODE -ne 0) { throw "WSL package installation failed" }

Write-Host "[2/4] Starting services..." -ForegroundColor Cyan
$windowsScript = (Resolve-Path "$PSScriptRoot/setup-wsl-database.sh").Path
$drive = $windowsScript.Substring(0, 1).ToLowerInvariant()
$linuxScript = "/mnt/$drive" + ($windowsScript.Substring(2) -replace '\\', '/')
wsl -d Ubuntu -u root -- bash $linuxScript
if ($LASTEXITCODE -ne 0) { throw "Failed to initialize Redis/PostgreSQL" }

Write-Host "[3/4] Database initialized." -ForegroundColor Cyan
Write-Host "[4/4] Connectivity verified by redis-cli and pg_isready." -ForegroundColor Cyan

Write-Host "Full backend services are ready." -ForegroundColor Green
Write-Host "Next: python run_full.py"
