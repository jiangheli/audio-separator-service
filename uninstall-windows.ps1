$ErrorActionPreference = "Stop"
& (Join-Path $PSScriptRoot "scripts\windows\uninstall.ps1") @args
exit $LASTEXITCODE
