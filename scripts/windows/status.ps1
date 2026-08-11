[CmdletBinding()]
param(
    [string]$ConfigPath = "",
    [ValidateRange(1, 1000)]
    [int]$Limit = 20,
    [switch]$Json
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
$Arguments = @(
    "-m", "app.cli.main",
    "status",
    "--config", $ConfigPath,
    "--limit", [string]$Limit
)
if ($Json) {
    $Arguments += "--json"
}
& $PythonExe @Arguments
exit $LASTEXITCODE
