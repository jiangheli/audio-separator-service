[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$TaskName = "StemFlow-Video-BGM-Removal",
    [switch]$RemoveEnvironment,
    [switch]$RemoveData
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $Task -and $PSCmdlet.ShouldProcess($TaskName, "Unregister scheduled task")) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Scheduled task removed: $TaskName" -ForegroundColor Green
}

if ($RemoveEnvironment) {
    $VenvPath = Join-Path $ProjectRoot ".venv-windows"
    if ((Test-Path $VenvPath) -and $PSCmdlet.ShouldProcess($VenvPath, "Remove Python environment")) {
        Remove-Item -LiteralPath $VenvPath -Recurse -Force
        Write-Host "Python environment removed." -ForegroundColor Green
    }
}

if ($RemoveData) {
    $DataPath = Join-Path $ProjectRoot "data"
    if ((Test-Path $DataPath) -and $PSCmdlet.ShouldProcess($DataPath, "Remove logs, models, state, reports and work files")) {
        Remove-Item -LiteralPath $DataPath -Recurse -Force
        Write-Host "Runtime data removed." -ForegroundColor Yellow
    }
} else {
    Write-Host "Logs, models, status database, report and output files were preserved."
}
