$ErrorActionPreference = "Stop"

# 生产/演示密钥只放在本机 .env.production，不进入 Git。
# 采用简单 KEY=VALUE 解析，避免依赖额外 PowerShell 模块；变量会继承给 API 进程。
$scriptRoot = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($scriptRoot)) {
    $scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition
}
if ([string]::IsNullOrWhiteSpace($scriptRoot)) {
    $scriptRoot = (Get-Location).Path
}
$dotenvPath = Join-Path (Resolve-Path (Join-Path $scriptRoot '..')).Path '.env.production'
if (Test-Path -LiteralPath $dotenvPath) {
    foreach ($line in Get-Content -LiteralPath $dotenvPath) {
        if ($line -match '^\s*([^#=\s]+)\s*=\s*(.*?)\s*$') {
            $name = $Matches[1]
            $value = $Matches[2]
            if ($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'")))) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            [Environment]::SetEnvironmentVariable($name, $value, 'Process')
        }
    }
}

$wslIpLine = wsl -d Ubuntu -- hostname -I
if ($LASTEXITCODE -ne 0 -or -not $wslIpLine) {
    throw "Unable to discover the Ubuntu WSL address"
}
$wslIp = ($wslIpLine.Trim() -split '\s+')[0]

# WSL may tear down its VM when no Windows-side WSL process remains, even though
# systemd services are running inside it. Keep one harmless process attached for
# the lifetime of the Windows-hosted API so Redis/PostgreSQL do not disappear.
$keepAlive = Start-Process -FilePath "wsl.exe" `
    -ArgumentList @('-d', 'Ubuntu', '--', 'sleep', 'infinity') `
    -WindowStyle Hidden -PassThru

try {
    $env:REDIS_URL = "redis://:dramaflow@${wslIp}:6379/0"
    $env:DATABASE_URL = "postgresql://postgres:postgres@${wslIp}:5432/dramaflow"

    $redisReady = $false
    $postgresReady = $false
    for ($attempt = 1; $attempt -le 30; $attempt++) {
        $redisReady = Test-NetConnection -ComputerName $wslIp -Port 6379 -InformationLevel Quiet -WarningAction SilentlyContinue
        $postgresReady = Test-NetConnection -ComputerName $wslIp -Port 5432 -InformationLevel Quiet -WarningAction SilentlyContinue
        if ($redisReady -and $postgresReady) { break }
        Start-Sleep -Seconds 1
    }
    if (-not $redisReady -or -not $postgresReady) {
        throw "WSL Redis/PostgreSQL did not become ready; run scripts/setup-wsl-full-backend.ps1"
    }

    Write-Host "Redis:      redis://:***@${wslIp}:6379/0" -ForegroundColor Cyan
    Write-Host "PostgreSQL: postgresql://postgres:***@${wslIp}:5432/dramaflow" -ForegroundColor Cyan
    Write-Host "DramaFlow:  http://127.0.0.1:8000" -ForegroundColor Green

    python run_full.py
    if ($LASTEXITCODE -ne 0) { throw "DramaFlow full mode exited with code $LASTEXITCODE" }
}
finally {
    if ($keepAlive -and -not $keepAlive.HasExited) {
        Stop-Process -Id $keepAlive.Id -Force -ErrorAction SilentlyContinue
    }
}
