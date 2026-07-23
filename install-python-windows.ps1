[CmdletBinding()]
param(
    [string]$PythonVersion = "3.12.10",
    [string]$DownloadUrl = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Test-Administrator {
    $Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $Principal = New-Object Security.Principal.WindowsPrincipal($Identity)
    return $Principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
}

function Find-CompatiblePython {
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
        $Candidates += [pscustomobject]@{
            Executable = "python.exe"
            Prefix = @()
        }
    }
    foreach ($Path in @(
        "C:\Program Files\Python312\python.exe",
        "C:\Program Files\Python311\python.exe",
        "C:\Program Files\Python310\python.exe",
        (Join-Path $env:LocalAppData "Programs\Python\Python312\python.exe"),
        (Join-Path $env:LocalAppData "Programs\Python\Python311\python.exe"),
        (Join-Path $env:LocalAppData "Programs\Python\Python310\python.exe")
    )) {
        if (Test-Path $Path) {
            $Candidates += [pscustomobject]@{
                Executable = $Path
                Prefix = @()
            }
        }
    }
    foreach ($Candidate in $Candidates) {
        try {
            $Prefix = $Candidate.Prefix
            $VersionText = & $Candidate.Executable @Prefix `
                -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"
            $Version = [version]$VersionText.Trim()
            if ($Version -ge [version]"3.10" -and $Version -lt [version]"3.13") {
                return [pscustomobject]@{
                    Executable = $Candidate.Executable
                    Prefix = $Candidate.Prefix
                    Version = $Version
                }
            }
        } catch {
            continue
        }
    }
    return $null
}

if (-not (Test-Administrator)) {
    throw "请右键 install-python-windows.cmd，选择“以管理员身份运行”。"
}

$ExistingPython = Find-CompatiblePython
if ($null -ne $ExistingPython) {
    Write-Host "已安装兼容的 Python，无需重复安装。" -ForegroundColor Green
    Write-Host "版本: $($ExistingPython.Version)"
    Write-Host "程序: $($ExistingPython.Executable)"
    exit 0
}

if ([string]::IsNullOrWhiteSpace($DownloadUrl)) {
    $DownloadUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe"
}

$TemporaryDirectory = Join-Path `
    ([System.IO.Path]::GetTempPath()) `
    ("stemflow-python-" + [guid]::NewGuid().ToString("N"))
$InstallerPath = Join-Path `
    $TemporaryDirectory `
    "python-$PythonVersion-amd64.exe"

try {
    New-Item -ItemType Directory -Force -Path $TemporaryDirectory | Out-Null
    [Net.ServicePointManager]::SecurityProtocol = `
        [Net.ServicePointManager]::SecurityProtocol -bor `
        [Net.SecurityProtocolType]::Tls12

    Write-Host "正在从 Python 官方下载 Python $PythonVersion x64..." `
        -ForegroundColor Cyan
    try {
        Invoke-WebRequest `
            -Uri $DownloadUrl `
            -OutFile $InstallerPath `
            -UseBasicParsing
    } catch {
        throw "Python 下载失败。请检查服务器网络、代理和防火墙。地址: $DownloadUrl。错误: $($_.Exception.Message)"
    }

    if (-not (Test-Path $InstallerPath) -or (Get-Item $InstallerPath).Length -lt 1MB) {
        throw "下载的 Python 安装包不完整，请删除后重试。"
    }

    $Signature = Get-AuthenticodeSignature -FilePath $InstallerPath
    if ($Signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
        throw "Python 安装包数字签名验证失败，已停止安装。签名状态: $($Signature.Status)"
    }

    Write-Host "正在静默安装 Python $PythonVersion..." -ForegroundColor Cyan
    $InstallerArguments = @(
        "/quiet",
        "InstallAllUsers=1",
        "PrependPath=1",
        "Include_pip=1",
        "Include_launcher=1",
        "InstallLauncherAllUsers=1",
        "Include_test=0"
    )
    $Process = Start-Process `
        -FilePath $InstallerPath `
        -ArgumentList $InstallerArguments `
        -Wait `
        -PassThru
    if ($Process.ExitCode -notin @(0, 3010)) {
        throw "Python 安装失败，安装程序退出码: $($Process.ExitCode)"
    }

    $InstalledPython = Find-CompatiblePython
    if ($null -eq $InstalledPython) {
        throw "安装程序已结束，但没有找到 Python。请重启服务器后再次运行本脚本。"
    }

    Write-Host ""
    Write-Host "Python 安装成功。" -ForegroundColor Green
    Write-Host "版本: $($InstalledPython.Version)"
    Write-Host "程序: $($InstalledPython.Executable)"
    Write-Host ""
    Write-Host "下一步请运行 install-windows.ps1 安装 StemFlow。" `
        -ForegroundColor Green
    if ($Process.ExitCode -eq 3010) {
        Write-Host "Windows 提示需要重启，建议先重启服务器。" `
            -ForegroundColor Yellow
    }
} finally {
    if (Test-Path $TemporaryDirectory) {
        Remove-Item -Path $TemporaryDirectory -Recurse -Force `
            -ErrorAction SilentlyContinue
    }
}
