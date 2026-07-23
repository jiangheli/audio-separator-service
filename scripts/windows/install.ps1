[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InputPath,

    [Parameter(Mandatory = $true)]
    [string]$OutputPath,

    [ValidatePattern("^(?:[01]\d|2[0-3]):[0-5]\d$")]
    [string]$ScheduleTime = "00:00",

    [ValidateSet("cpu", "gpu")]
    [string]$Mode = "cpu",

    [string]$DataPath = "",
    [string]$TaskName = "StemFlow-Video-BGM-Removal",
    [string]$Model = "",

    [ValidateRange(0, 86400)]
    [int]$StableSeconds = 120,

    [ValidateRange(0, 20)]
    [int]$MaxRetries = 3,

    [bool]$RunAsSystem = $true,
    [switch]$SkipScheduledTask,
    [switch]$RunNow,
    [string]$CudaWheelIndex = "https://download.pytorch.org/whl/cu128"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$BackendPath = Join-Path $ProjectRoot "backend"
$VenvPath = Join-Path $ProjectRoot ".venv-windows"
$ConfigDirectory = Join-Path $ProjectRoot "config"
$ConfigPath = Join-Path $ConfigDirectory "stemflow.json"

function Test-Administrator {
    $Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $Principal = New-Object Security.Principal.WindowsPrincipal($Identity)
    return $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Find-Python {
    $Candidates = @()
    if (Get-Command py.exe -ErrorAction SilentlyContinue) {
        foreach ($Selector in @("-3.12", "-3.11", "-3.10")) {
            $Candidates += [pscustomobject]@{
                Executable = "py.exe"
                Prefix = @($Selector)
            }
        }
    }
    if (Get-Command python.exe -ErrorAction SilentlyContinue) {
        $Candidates += [pscustomobject]@{ Executable = "python.exe"; Prefix = @() }
    }
    $SearchRoots = @(
        (Join-Path $env:LocalAppData "Programs\Python"),
        "C:\Program Files\Python312",
        "C:\Program Files\Python311",
        "C:\Program Files\Python310"
    )
    foreach ($Root in $SearchRoots) {
        if (Test-Path $Root) {
            Get-ChildItem $Root -Filter python.exe -Recurse -ErrorAction SilentlyContinue |
                ForEach-Object {
                    $Candidates += [pscustomobject]@{
                        Executable = $_.FullName
                        Prefix = @()
                    }
                }
        }
    }
    foreach ($Candidate in $Candidates) {
        try {
            $Prefix = $Candidate.Prefix
            $VersionText = & $Candidate.Executable @Prefix -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
            $Version = [version]$VersionText.Trim()
            if ($Version -ge [version]"3.10" -and $Version -lt [version]"3.13") {
                return $Candidate
            }
        } catch {
            continue
        }
    }
    return $null
}

if ($RunAsSystem -and -not $SkipScheduledTask -and -not (Test-Administrator)) {
    throw "Run PowerShell as Administrator to register a SYSTEM scheduled task."
}

$Python = Find-Python
if ($null -eq $Python) {
    if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
        throw "Python 3.10-3.12 was not found and winget is unavailable. Install Python 3.12 x64, then run this script again."
    }
    Write-Host "Installing Python 3.12..." -ForegroundColor Cyan
    & winget.exe install --exact --id Python.Python.3.12 --silent `
        --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        throw "Python installation failed with exit code $LASTEXITCODE."
    }
    $Python = Find-Python
    if ($null -eq $Python) {
        throw "Python was installed but could not be located. Open a new Administrator PowerShell window and run the script again."
    }
}

New-Item -ItemType Directory -Force -Path $InputPath | Out-Null
New-Item -ItemType Directory -Force -Path $OutputPath | Out-Null
if ([string]::IsNullOrWhiteSpace($DataPath)) {
    $DataPath = Join-Path $ProjectRoot "data"
}
New-Item -ItemType Directory -Force -Path $DataPath | Out-Null
New-Item -ItemType Directory -Force -Path $ConfigDirectory | Out-Null

$ResolvedInput = (Resolve-Path $InputPath).Path
$ResolvedOutput = (Resolve-Path $OutputPath).Path
$ResolvedData = (Resolve-Path $DataPath).Path
if ($ResolvedInput -eq $ResolvedOutput) {
    throw "InputPath and OutputPath must be different directories."
}

Write-Host "Creating Python environment..." -ForegroundColor Cyan
$PythonPrefix = $Python.Prefix
& $Python.Executable @PythonPrefix -m venv $VenvPath
$PythonExe = Join-Path $VenvPath "Scripts\python.exe"
if (-not (Test-Path $PythonExe)) {
    throw "Virtual environment creation failed: $PythonExe"
}

& $PythonExe -m pip install --upgrade pip setuptools wheel
if ($LASTEXITCODE -ne 0) {
    throw "Could not upgrade pip, setuptools and wheel."
}
if ($Mode -eq "gpu") {
    & $PythonExe -m pip install -e "$BackendPath[gpu]"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not install StemFlow GPU dependencies."
    }
    & $PythonExe -m pip uninstall -y onnxruntime
    if ($LASTEXITCODE -ne 0) {
        throw "Could not remove the CPU ONNX Runtime package."
    }
    & $PythonExe -m pip install --upgrade onnxruntime-gpu
    if ($LASTEXITCODE -ne 0) {
        throw "Could not install ONNX Runtime GPU."
    }
    & $PythonExe -m pip install --upgrade torch torchaudio --index-url $CudaWheelIndex
    if ($LASTEXITCODE -ne 0) {
        throw "Could not install CUDA-enabled PyTorch."
    }
} else {
    & $PythonExe -m pip install -e "$BackendPath[windows]"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not install StemFlow CPU dependencies."
    }
}

if ([string]::IsNullOrWhiteSpace($Model)) {
    $Model = if ($Mode -eq "gpu") { "roformer" } else { "mdx" }
}

$Configuration = [ordered]@{
    input_dir = $ResolvedInput
    output_dir = $ResolvedOutput
    data_dir = $ResolvedData
    model_dir = (Join-Path $ResolvedData "models")
    work_dir = (Join-Path $ResolvedData "work")
    log_dir = (Join-Path $ResolvedData "logs")
    database_path = (Join-Path $ResolvedData "processing.db")
    report_path = (Join-Path $ResolvedData "processing_status.xlsx")
    model = $Model
    schedule_time = $ScheduleTime
    stable_seconds = $StableSeconds
    max_retries = $MaxRetries
    output_suffix = "_vocals_only"
    audio_bitrate = "192k"
    recursive = $true
    keep_failed_work = $false
    video_copy = $true
}
$Configuration | ConvertTo-Json -Depth 4 |
    Set-Content -Path $ConfigPath -Encoding UTF8

Write-Host "Validating runtime..." -ForegroundColor Cyan
& $PythonExe -m app.cli.main validate --config $ConfigPath
if ($LASTEXITCODE -ne 0) {
    throw "StemFlow runtime validation failed."
}

if ($Mode -eq "gpu") {
    Write-Host "Checking NVIDIA CUDA..." -ForegroundColor Cyan
    & $PythonExe -c "import sys, torch; ok=torch.cuda.is_available(); print('CUDA available:', ok); print('Device:', torch.cuda.get_device_name(0) if ok else 'none'); sys.exit(0 if ok else 1)"
    if ($LASTEXITCODE -ne 0) {
        throw "GPU mode was requested, but CUDA is unavailable. Check the NVIDIA driver and CUDA-compatible PyTorch installation."
    }
}

if (-not $SkipScheduledTask) {
    $RunnerScript = Join-Path $ProjectRoot "scripts\windows\run-now.ps1"
    $ActionArguments = "-NoProfile -ExecutionPolicy Bypass -File `"$RunnerScript`" -ConfigPath `"$ConfigPath`""
    $Action = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument $ActionArguments `
        -WorkingDirectory $ProjectRoot
    $Trigger = New-ScheduledTaskTrigger -Daily -At $ScheduleTime
    $Settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew `
        -RestartCount 2 `
        -RestartInterval (New-TimeSpan -Minutes 5) `
        -ExecutionTimeLimit (New-TimeSpan -Days 7)
    if ($RunAsSystem) {
        $Principal = New-ScheduledTaskPrincipal `
            -UserId "SYSTEM" `
            -LogonType ServiceAccount `
            -RunLevel Highest
    } else {
        $UserId = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        $Principal = New-ScheduledTaskPrincipal `
            -UserId $UserId `
            -LogonType Interactive `
            -RunLevel Highest
    }
    $Task = New-ScheduledTask `
        -Action $Action `
        -Trigger $Trigger `
        -Settings $Settings `
        -Principal $Principal `
        -Description "StemFlow scans a fixed folder and creates vocals-only videos."
    Register-ScheduledTask -TaskName $TaskName -InputObject $Task -Force | Out-Null
    Write-Host "Scheduled task registered: $TaskName at $ScheduleTime" -ForegroundColor Green
}

if ($RunNow) {
    & (Join-Path $ProjectRoot "scripts\windows\run-now.ps1") -ConfigPath $ConfigPath
    if ($LASTEXITCODE -ne 0) {
        throw "The initial batch run failed. Check the log file."
    }
}

Write-Host ""
Write-Host "StemFlow Video BGM Removal is installed." -ForegroundColor Green
Write-Host "Mode:    $Mode"
Write-Host "Input:   $ResolvedInput"
Write-Host "Output:  $ResolvedOutput"
Write-Host "Config:  $ConfigPath"
Write-Host "Log:     $(Join-Path $ResolvedData 'logs\stemflow-video.log')"
Write-Host "Report:  $(Join-Path $ResolvedData 'processing_status.xlsx')"
if (-not $SkipScheduledTask) {
    Write-Host "Schedule: every day at $ScheduleTime"
}
