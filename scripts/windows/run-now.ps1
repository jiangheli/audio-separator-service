[CmdletBinding()]
param(
    [string]$ConfigPath = "",
    [ValidateRange(0, 1000000)]
    [int]$MaxFiles = 0,
    [switch]$VerboseLog
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $ConfigPath = Join-Path $ProjectRoot "config\stemflow.json"
}
$PythonExe = Join-Path $ProjectRoot ".venv-windows\Scripts\python.exe"
if (-not (Test-Path $PythonExe)) {
    throw "StemFlow is not installed. Run install-windows.ps1 first."
}
if (-not (Test-Path $ConfigPath)) {
    throw "Configuration file not found: $ConfigPath"
}

$Arguments = @(
    "-m", "app.cli.main",
    "run-once",
    "--config", $ConfigPath,
    "--max-files", [string]$MaxFiles
)
if ($VerboseLog) {
    $Arguments += "--verbose"
}
& $PythonExe @Arguments
exit $LASTEXITCODE
