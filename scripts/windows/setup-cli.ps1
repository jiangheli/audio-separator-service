[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$VenvPath = Join-Path $ProjectRoot ".venv-windows"

if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 -m venv $VenvPath
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python -m venv $VenvPath
} else {
    throw "Python 3.10 or newer was not found. Install Python for Windows first."
}

$PythonExe = Join-Path $VenvPath "Scripts\python.exe"
& $PythonExe -m pip install --upgrade pip
& $PythonExe -m pip install -e "$ProjectRoot\backend[windows]"
& $PythonExe -c "from app.runtime import ensure_ffmpeg; print('FFmpeg:', ensure_ffmpeg())"

Write-Host ""
Write-Host "Windows CLI is ready." -ForegroundColor Green
Write-Host "Example:"
Write-Host ".\.venv-windows\Scripts\audio-separator-service.exe --input C:\Videos --output C:\Results --model mdx"
