$ErrorActionPreference = "Stop"
& (Join-Path $PSScriptRoot "scripts\windows\status.ps1") @args
exit $LASTEXITCODE
