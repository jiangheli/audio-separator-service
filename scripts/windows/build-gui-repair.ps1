[CmdletBinding()]
param(
    [string]$Version = "1.4.2",
    [switch]$SkipDependencyInstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$BackendPath = Join-Path $ProjectRoot "backend"
$SpecPath = Join-Path $ProjectRoot "packaging\windows\stemflow.spec"
$IconScript = Join-Path $ProjectRoot "packaging\windows\make_icon.py"
$InstallerScript = Join-Path $ProjectRoot "packaging\windows\StemFlowRepair.iss"

if (-not $SkipDependencyInstall) {
    python -m pip install --upgrade pip
    python -m pip install `
        --index-url https://download.pytorch.org/whl/cpu `
        torch torchvision torchaudio
    python -m pip install "$BackendPath[windows]" pyinstaller pillow
}

& (Join-Path $PSScriptRoot "download-bundle-assets.ps1") `
    -SkipCudaWheelhouse
python -m pip cache purge | Out-Null

$env:STEMFLOW_INCLUDE_OFFLINE_CUDA = "0"
python $IconScript
python -m PyInstaller --noconfirm --clean `
    --distpath (Join-Path $ProjectRoot "dist-repair") `
    --workpath (Join-Path $ProjectRoot "build\pyinstaller-repair") `
    $SpecPath
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller repair build failed."
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
    -Path (Join-Path $ProjectRoot "dist\repair") | Out-Null
& $InnoCompiler "/DAppVersion=$Version" $InstallerScript
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup repair build failed."
}

$Installer = Join-Path `
    $ProjectRoot "dist\repair\StemFlow-Repair-$Version-x64.exe"
if (-not (Test-Path $Installer)) {
    throw "Repair installer was not created: $Installer"
}
Write-Host "Repair installer ready: $Installer" -ForegroundColor Green
