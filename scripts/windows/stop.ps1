[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

Push-Location $ProjectRoot
try {
    & docker compose -f docker-compose.yml -f docker-compose.gpu.yml down
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Compose failed to stop StemFlow."
    }
} finally {
    Pop-Location
}

Write-Host "StemFlow has stopped. Models, results, and task history are preserved." -ForegroundColor Green
