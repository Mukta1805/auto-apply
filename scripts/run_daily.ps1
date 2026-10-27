# Called by Windows Task Scheduler.
#   -Mode run       full daily pipeline (6 AM task)
#   -Mode outreach  Gmail sync, follow-ups and new emails only (late-morning task, inside the send window)
param([ValidateSet("run", "outreach")][string]$Mode = "run")
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Py = Join-Path $Root ".venv\Scripts\python.exe"
New-Item -ItemType Directory -Force -Path "logs" | Out-Null
$SchedLog = Join-Path $Root "logs\scheduler.log"
$Start = Get-Date
Add-Content -Path $SchedLog -Value "$($Start.ToString('yyyy-MM-dd HH:mm:ss')) start mode=$Mode"
if (-not (Test-Path $Py)) {
    Add-Content -Path $SchedLog -Value "$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss')) ERROR .venv missing; run scripts\setup_windows.ps1"
    exit 2
}
$env:PYTHONIOENCODING = "utf-8"
# Console output is kept too; the detailed log is logs\jobagent.log and logs\runs\<run id>.log.
& $Py -m jobagent $Mode *>> (Join-Path $Root "logs\scheduler_console.log")
$Code = $LASTEXITCODE
$Mins = [math]::Round(((Get-Date) - $Start).TotalMinutes, 1)
Add-Content -Path $SchedLog -Value "$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss')) end mode=$Mode exit=$Code minutes=$Mins"
exit $Code
