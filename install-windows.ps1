$ErrorActionPreference = "Stop"
& (Join-Path $PSScriptRoot "scripts\windows\install.ps1") @args
exit $LASTEXITCODE
