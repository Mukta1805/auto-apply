# Registers (or updates) the two daily scheduled tasks:
#   RiyaJobAgent          full run (fetch, score, apply, outreach drafts, workbook, summary)  default 06:00
#   RiyaJobAgentOutreach  Gmail sync, follow-ups and sending, inside business hours            default 10:45
#   powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 [-Time 06:00] [-OutreachTime 10:45] [-Remove]
param(
    [string]$Time = "06:00",
    [string]$OutreachTime = "10:45",
    [switch]$Remove
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Tasks = @{ "RiyaJobAgent" = @{ Mode = "run"; At = $Time; Hours = 4 };
            "RiyaJobAgentOutreach" = @{ Mode = "outreach"; At = $OutreachTime; Hours = 1 } }

if ($Remove) {
    foreach ($name in $Tasks.Keys) { Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue }
    Write-Host "Removed scheduled tasks."
    exit 0
}

$Script = Join-Path $Root "scripts\run_daily.ps1"
# Interactive: runs while Riya is signed in (the auto-apply browser window needs her desktop session).
$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
foreach ($name in $Tasks.Keys) {
    $t = $Tasks[$name]
    $Action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Script`" -Mode $($t.Mode)" -WorkingDirectory $Root
    $Trigger = New-ScheduledTaskTrigger -Daily -At $t.At
    $Settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -RunOnlyIfNetworkAvailable `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Hours $t.Hours)
    Register-ScheduledTask -TaskName $name -Action $Action -Trigger $Trigger -Settings $Settings `
        -Principal $Principal -Description "Riya's job agent ($($t.Mode))" -Force | Out-Null
    Write-Host "Scheduled '$name' daily at $($t.At)." -ForegroundColor Green
}
Write-Host "Tasks wake the laptop if it is asleep and plugged in; if it was off, they run at the next start-up."
Write-Host "Run one now to test:  Start-ScheduledTask -TaskName RiyaJobAgent"
