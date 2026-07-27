[CmdletBinding()]
param(
    [string]$Version = "1.3.0",
    [switch]$SkipDependencyInstall,
    [switch]$SkipAssetDownload
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$BackendPath = Join-Path $ProjectRoot "backend"
$SpecPath = Join-Path $ProjectRoot "packaging\windows\stemflow.spec"
$IconScript = Join-Path $ProjectRoot "packaging\windows\make_icon.py"
$InstallerScript = Join-Path $ProjectRoot "packaging\windows\StemFlow.iss"

if (-not $SkipDependencyInstall) {
    python -m pip install --upgrade pip
    python -m pip install `
        --index-url https://download.pytorch.org/whl/cpu `
        torch torchvision torchaudio
    python -m pip install "$BackendPath[windows]" pyinstaller pillow
}
if (-not $SkipAssetDownload) {
    & (Join-Path $PSScriptRoot "download-bundle-assets.ps1")
}

python $IconScript
python -m PyInstaller --noconfirm --clean `
    --distpath (Join-Path $ProjectRoot "dist") `
    --workpath (Join-Path $ProjectRoot "build\pyinstaller") `
    $SpecPath
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed."
}

$InnoCandidates = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
)
$InnoCompiler = $InnoCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $InnoCompiler) {
    throw "Inno Setup 6 was not found. Install it, then run this script again."
}
New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot "dist\installer") |
    Out-Null
& $InnoCompiler "/DAppVersion=$Version" $InstallerScript
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup build failed."
}

$Installer = Join-Path $ProjectRoot "dist\installer\StemFlow-Setup-$Version-x64.exe"
if (-not (Test-Path $Installer)) {
    throw "Installer was not created: $Installer"
}
Write-Host "Installer ready: $Installer" -ForegroundColor Green
