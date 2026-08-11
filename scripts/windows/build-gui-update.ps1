[CmdletBinding()]
param(
    [string]$Version = "1.7.1",
    [switch]$SkipDependencyInstall,
    [switch]$SkipAssetDownload
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$BackendPath = Join-Path $ProjectRoot "backend"
$SpecPath = Join-Path $ProjectRoot "packaging\windows\stemflow.spec"
$IconScript = Join-Path $ProjectRoot "packaging\windows\make_icon.py"
$InstallerScript = Join-Path $ProjectRoot "packaging\windows\StemFlowUpdate.iss"

if (-not $SkipDependencyInstall) {
    python -m pip install --upgrade pip
    python -m pip install `
        --index-url https://download.pytorch.org/whl/cpu `
        torch torchvision torchaudio
    python -m pip install "$BackendPath[windows]" pyinstaller pillow
}
if (-not $SkipAssetDownload) {
    & (Join-Path $PSScriptRoot "download-bundle-assets.ps1") `
        -SkipCudaWheelhouse
}

$env:STEMFLOW_INCLUDE_OFFLINE_CUDA = "0"
python $IconScript
python -m PyInstaller --noconfirm --clean `
    --distpath (Join-Path $ProjectRoot "dist-update") `
    --workpath (Join-Path $ProjectRoot "build\pyinstaller-update") `
    $SpecPath
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller update build failed."
}

$InnoCandidates = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
)
$InnoCompiler = $InnoCandidates |
    Where-Object { Test-Path $_ } |
    Select-Object -First 1
if (-not $InnoCompiler) {
    throw "Inno Setup 6 was not found."
}
New-Item -ItemType Directory -Force `
    -Path (Join-Path $ProjectRoot "dist\update") | Out-Null
& $InnoCompiler "/DAppVersion=$Version" $InstallerScript
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup update build failed."
}

$Installer = Join-Path `
    $ProjectRoot "dist\update\StemFlow-Update-$Version-x64.exe"
if (-not (Test-Path $Installer)) {
    throw "Update installer was not created: $Installer"
}
$Hash = (Get-FileHash $Installer -Algorithm SHA256).Hash.ToLowerInvariant()
$Checksum = "$Installer.sha256"
"$Hash  $([IO.Path]::GetFileName($Installer))" |
    Set-Content -Path $Checksum -Encoding ascii
Write-Host "Update installer ready: $Installer" -ForegroundColor Green
Write-Host "SHA256: $Hash" -ForegroundColor Green
