# Called by Windows Task Scheduler. Runs the pipeline and records the exit code.
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Py = Join-Path $Root ".venv\Scripts\python.exe"
New-Item -ItemType Directory -Force -Path "logs" | Out-Null
$SchedLog = Join-Path $Root "logs\scheduler.log"
$Start = Get-Date
Add-Content -Path $SchedLog -Value "$($Start.ToString('yyyy-MM-dd HH:mm:ss')) start"
if (-not (Test-Path $Py)) {
    Add-Content -Path $SchedLog -Value "$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss')) ERROR .venv missing; run scripts\setup_windows.ps1"
    exit 2
}
# Console output goes to the scheduler log too (the detailed log is logs\jobagent.log).
& $Py -m jobagent run *>> (Join-Path $Root "logs\scheduler_console.log")
$Code = $LASTEXITCODE
$Mins = [math]::Round(((Get-Date) - $Start).TotalMinutes, 1)
Add-Content -Path $SchedLog -Value "$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss')) end exit=$Code minutes=$Mins"
exit $Code
