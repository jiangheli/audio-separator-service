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
$GpuBootstrapDirectory = Join-Path $Destination "gpu-bootstrap"
$WheelhouseDirectory = Join-Path $GpuBootstrapDirectory "wheelhouse"
New-Item -ItemType Directory -Force `
    -Path $ModelDirectory, $GpuBootstrapDirectory, $WheelhouseDirectory | Out-Null

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
Get-Asset `
    -Uri "https://raw.githubusercontent.com/jrsoftware/issrc/main/Files/Languages/ChineseSimplified.isl" `
    -Output (Join-Path $Destination "ChineseSimplified.isl") `
    -MinimumBytes 15000
Get-Asset `
    -Uri "https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip" `
    -Output (Join-Path $GpuBootstrapDirectory "python-3.12.10-embed-amd64.zip") `
    -MinimumBytes 10000000
Get-Asset `
    -Uri "https://bootstrap.pypa.io/get-pip.py" `
    -Output (Join-Path $GpuBootstrapDirectory "get-pip.py") `
    -MinimumBytes 1000000

$WheelhouseManifest = Join-Path $WheelhouseDirectory "wheelhouse-manifest.json"
$RequiredTorchWheel = Get-ChildItem $WheelhouseDirectory `
    -Filter "torch-2.7.1+cu128-cp312-cp312-win_amd64.whl" `
    -ErrorAction SilentlyContinue |
    Select-Object -First 1
if (-not $RequiredTorchWheel -or -not (Test-Path $WheelhouseManifest)) {
    Write-Host "Downloading complete offline CUDA PyTorch wheelhouse..." `
        -ForegroundColor Cyan
    Get-ChildItem $WheelhouseDirectory -File -ErrorAction SilentlyContinue |
        Remove-Item -Force
    python -m pip download `
        --dest $WheelhouseDirectory `
        --only-binary=:all: `
        --index-url "https://download.pytorch.org/whl/cu128" `
        --extra-index-url "https://pypi.org/simple" `
        "pip==25.1.1" `
        "setuptools>=75,<81" `
        "wheel>=0.45,<1" `
        "torch==2.7.1+cu128" `
        "torchvision==0.22.1+cu128" `
        "torchaudio==2.7.1+cu128" `
        "audio-separator>=0.44.5,<0.45" `
        "imageio-ffmpeg>=0.6,<1" `
        "onnxruntime-gpu>=1.21,<1.23" `
        "XlsxWriter>=3.2,<4"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not download the offline CUDA PyTorch wheelhouse."
    }

    $WheelFiles = @(
        Get-ChildItem $WheelhouseDirectory -Filter "*.whl" -File |
            Sort-Object Name |
            ForEach-Object {
                @{
                    name = $_.Name
                    size = $_.Length
                    sha256 = (
                        Get-FileHash $_.FullName -Algorithm SHA256
                    ).Hash.ToLowerInvariant()
                }
            }
    )
    if ($WheelFiles.Count -lt 10) {
        throw "Offline CUDA wheelhouse is unexpectedly incomplete."
    }
    @{
        format_version = 1
        created_at = [DateTime]::UtcNow.ToString("o")
        python = "3.12"
        platform = "win_amd64"
        cuda = "cu128"
        torch = "2.7.1"
        files = $WheelFiles
    } | ConvertTo-Json -Depth 5 |
        Set-Content -Path $WheelhouseManifest -Encoding UTF8
}

$OfflineSize = (
    Get-ChildItem $WheelhouseDirectory -Filter "*.whl" -File |
        Measure-Object -Property Length -Sum
).Sum
if ($OfflineSize -lt 3GB) {
    throw "Offline CUDA wheelhouse is unexpectedly small: $OfflineSize bytes"
}

$PythonArchive = Join-Path $GpuBootstrapDirectory "python-3.12.10-embed-amd64.zip"
$ExpectedPythonSha256 = "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"
$ActualPythonSha256 = (Get-FileHash $PythonArchive -Algorithm SHA256).Hash.ToLowerInvariant()
if ($ActualPythonSha256 -ne $ExpectedPythonSha256) {
    throw "Embedded Python SHA256 mismatch: $ActualPythonSha256"
}

$Signature = Get-AuthenticodeSignature (Join-Path $Destination "vc_redist.x64.exe")
if ($Signature.Status -ne "Valid") {
    throw "Microsoft Visual C++ installer signature is not valid: $($Signature.Status)"
}
Write-Host "Windows bundle assets are ready." -ForegroundColor Green
