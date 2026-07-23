$ErrorActionPreference = "Stop"
& (Join-Path $PSScriptRoot "scripts\windows\run-now.ps1") @args
exit $LASTEXITCODE
