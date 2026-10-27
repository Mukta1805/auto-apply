# Registers (or updates) the daily scheduled task "RiyaJobAgent".
#   powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 [-Time 06:00] [-Remove]
param(
    [string]$Time = "06:00",
    [switch]$Remove
)
$ErrorActionPreference = "Stop"
$TaskName = "RiyaJobAgent"
$Root = Split-Path -Parent $PSScriptRoot

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task $TaskName"
    exit 0
}

$Script = Join-Path $Root "scripts\run_daily.ps1"
$Action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Script`"" -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -Daily -At $Time
$Settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -RunOnlyIfNetworkAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4)
# Interactive: runs while Riya is signed in (phase 2's visible browser needs her desktop session).
$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
    -Principal $Principal -Description "Daily job discovery and scoring for Riya" -Force | Out-Null
Write-Host "Scheduled task '$TaskName' will run daily at $Time." -ForegroundColor Green
Write-Host "It wakes the laptop if it is asleep and plugged in; if the laptop was off, it runs at next start-up."
Write-Host "Run it now to test:  Start-ScheduledTask -TaskName $TaskName"
