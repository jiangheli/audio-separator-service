[CmdletBinding()]
param(
    [string]$Destination = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
if ([string]::IsNullOrWhiteSpace($Destination)) {
    $Destination = Join-Path $ProjectRoot "packaging\windows\bundle"
}
$ModelDirectory = Join-Path $Destination "models"
New-Item -ItemType Directory -Force -Path $ModelDirectory | Out-Null

function Get-Asset {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][string]$Output,
        [long]$MinimumBytes = 1
    )
    if ((Test-Path $Output) -and (Get-Item $Output).Length -ge $MinimumBytes) {
        Write-Host "Using cached asset: $Output"
        return
    }
    $Temporary = "$Output.download"
    Remove-Item $Temporary -Force -ErrorAction SilentlyContinue
    Write-Host "Downloading $Uri" -ForegroundColor Cyan
    & curl.exe -L --fail --retry 10 --retry-all-errors `
        --connect-timeout 30 --output $Temporary $Uri
    if ($LASTEXITCODE -ne 0) {
        throw "Download failed: $Uri"
    }
    if ((Get-Item $Temporary).Length -lt $MinimumBytes) {
        throw "Downloaded file is unexpectedly small: $Temporary"
    }
    Move-Item $Temporary $Output -Force
}

Get-Asset `
    -Uri "https://github.com/TRvlvr/model_repo/releases/download/all_public_uvr_models/UVR-MDX-NET-Inst_HQ_3.onnx" `
    -Output (Join-Path $ModelDirectory "UVR-MDX-NET-Inst_HQ_3.onnx") `
    -MinimumBytes 60000000
Get-Asset `
    -Uri "https://raw.githubusercontent.com/TRvlvr/application_data/main/filelists/download_checks.json" `
    -Output (Join-Path $ModelDirectory "download_checks.json") `
    -MinimumBytes 10000
Get-Asset `
    -Uri "https://raw.githubusercontent.com/TRvlvr/application_data/main/mdx_model_data/model_data_new.json" `
    -Output (Join-Path $ModelDirectory "mdx_model_data.json") `
    -MinimumBytes 10000
Get-Asset `
    -Uri "https://raw.githubusercontent.com/TRvlvr/application_data/main/vr_model_data/model_data_new.json" `
    -Output (Join-Path $ModelDirectory "vr_model_data.json") `
    -MinimumBytes 3000
Get-Asset `
    -Uri "https://aka.ms/vs/17/release/vc_redist.x64.exe" `
    -Output (Join-Path $Destination "vc_redist.x64.exe") `
    -MinimumBytes 10000000

$Signature = Get-AuthenticodeSignature (Join-Path $Destination "vc_redist.x64.exe")
if ($Signature.Status -ne "Valid") {
    throw "Microsoft Visual C++ installer signature is not valid: $($Signature.Status)"
}
Write-Host "Windows bundle assets are ready." -ForegroundColor Green
