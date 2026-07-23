[CmdletBinding()]
param(
    [ValidateSet("cpu", "gpu")]
    [string]$Mode = "cpu",
    [string]$InputPath = "",
    [string]$OutputPath = "",
    [ValidateRange(1, 65535)]
    [int]$WebPort = 3000,
    [ValidateRange(1, 65535)]
    [int]$ApiPort = 8000,
    [int]$Workers = 0,
    [switch]$NoBuild
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
if ($Workers -eq 0) {
    $Workers = if ($Mode -eq "gpu") { 1 } else { 2 }
}
if ($Workers -lt 1 -or $Workers -gt 16) {
    throw "Workers must be between 1 and 16."
}
if ([string]::IsNullOrWhiteSpace($InputPath)) {
    $InputPath = Join-Path $ProjectRoot "data\input"
}
if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $OutputPath = Join-Path $ProjectRoot "data\output"
}

New-Item -ItemType Directory -Force -Path $InputPath | Out-Null
New-Item -ItemType Directory -Force -Path $OutputPath | Out-Null
$ResolvedInput = (Resolve-Path $InputPath).Path.Replace("\", "/")
$ResolvedOutput = (Resolve-Path $OutputPath).Path.Replace("\", "/")

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker Desktop was not found. Install Docker Desktop for Windows first."
}

& docker info *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Docker Desktop is not running. Start Docker Desktop and try again."
}

$env:AUDIO_SERVICE_HOST_INPUT = $ResolvedInput
$env:AUDIO_SERVICE_HOST_OUTPUT = $ResolvedOutput
$env:AUDIO_SERVICE_PORT = [string]$WebPort
$env:AUDIO_SERVICE_API_PORT = [string]$ApiPort
$env:AUDIO_SERVICE_WORKER_CONCURRENCY = [string]$Workers
if ($Mode -eq "gpu") {
    $env:AUDIO_SERVICE_GPU_WORKER_CONCURRENCY = [string]$Workers
}

$ComposeArguments = @(
    "compose",
    "-f", (Join-Path $ProjectRoot "docker-compose.yml")
)
if ($Mode -eq "gpu") {
    $ComposeArguments += @("-f", (Join-Path $ProjectRoot "docker-compose.gpu.yml"))
}
$ComposeArguments += @("up", "-d")
if (-not $NoBuild) {
    $ComposeArguments += "--build"
}

Push-Location $ProjectRoot
try {
    & docker @ComposeArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Compose failed to start StemFlow."
    }
} finally {
    Pop-Location
}

$HealthUrl = "http://localhost:$ApiPort/api/health"
$Healthy = $false
for ($Attempt = 1; $Attempt -le 60; $Attempt++) {
    try {
        $Health = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 2
        if ($Health.status -eq "ok") {
            $Healthy = $true
            break
        }
    } catch {
        Start-Sleep -Seconds 2
    }
}
if (-not $Healthy) {
    throw "StemFlow containers started, but the API did not become healthy: $HealthUrl"
}

Push-Location $ProjectRoot
try {
    $RunningServices = & docker compose -f docker-compose.yml ps --services --filter "status=running"
} finally {
    Pop-Location
}
if ($RunningServices -notcontains "worker") {
    throw "The API is healthy, but the audio worker is not running. Check Docker Desktop logs."
}

Write-Host ""
Write-Host "StemFlow is ready on Windows ($Mode)." -ForegroundColor Green
Write-Host "Web:    http://localhost:$WebPort"
Write-Host "API:    http://localhost:$ApiPort/docs"
Write-Host "Input:  $ResolvedInput -> /input"
Write-Host "Output: $ResolvedOutput -> /output"
Write-Host "Concurrent inference slots: $Workers"
Write-Host "Use /input and /output in the Web form."

if ($Mode -eq "gpu") {
    Write-Host ""
    Write-Host "Checking NVIDIA CUDA inside the worker..."
    Push-Location $ProjectRoot
    try {
        & docker compose -f docker-compose.yml -f docker-compose.gpu.yml exec -T worker python3 -c "import sys, torch; ok=torch.cuda.is_available(); print('CUDA available:', ok); print('Device:', torch.cuda.get_device_name(0) if ok else 'none'); sys.exit(0 if ok else 1)"
        if ($LASTEXITCODE -ne 0) {
            throw "GPU mode started, but CUDA is unavailable inside the worker. Update WSL, the NVIDIA Windows driver, and Docker Desktop."
        }
    } finally {
        Pop-Location
    }
}
