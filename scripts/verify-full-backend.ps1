$ErrorActionPreference = "Stop"

$wslIp = ((wsl -d Ubuntu -- hostname -I).Trim() -split '\s+')[0]
$redis = Test-NetConnection -ComputerName $wslIp -Port 6379 -WarningAction SilentlyContinue
$postgres = Test-NetConnection -ComputerName $wslIp -Port 5432 -WarningAction SilentlyContinue

if (-not $redis.TcpTestSucceeded) {
    throw "Redis is not reachable at ${wslIp}:6379"
}
if (-not $postgres.TcpTestSucceeded) {
    throw "PostgreSQL is not reachable at ${wslIp}:5432"
}

python demo/full_stack_acceptance.py http://127.0.0.1:8000
if ($LASTEXITCODE -ne 0) { throw "DramaFlow full-stack acceptance failed" }

Write-Host "DramaFlow full backend passed." -ForegroundColor Green
