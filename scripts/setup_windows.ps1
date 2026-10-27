# One-time setup on Windows. Run from the repo folder in PowerShell:
#   powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
Write-Host "Setting up Riya's job agent in $Root" -ForegroundColor Cyan

# 1. Python 3.11+ (prefers the py launcher)
$PyCmd = $null
foreach ($candidate in @("py -3.13", "py -3.12", "py -3.11", "python")) {
    try {
        $ver = & cmd /c "$candidate --version 2>&1"
        if ($LASTEXITCODE -eq 0 -and $ver -match "Python 3\.(1[1-9])") { $PyCmd = $candidate; break }
    } catch { }
}
if (-not $PyCmd) {
    Write-Host "Python 3.11+ not found." -ForegroundColor Yellow
    $answer = Read-Host "Install Python 3.12 with winget now? (y/n)"
    if ($answer -ne "y") { throw "Install Python 3.12 from python.org, then re-run this script." }
    winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
    $PyCmd = "py -3.12"
}
Write-Host "Using $PyCmd" -ForegroundColor Green

# 2. Virtual environment + packages
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    & cmd /c "$PyCmd -m venv .venv"
    if ($LASTEXITCODE -ne 0) { throw "Could not create .venv" }
}
$Py = Join-Path $Root ".venv\Scripts\python.exe"
& $Py -m pip install --upgrade pip --quiet
& $Py -m pip install -e ".[dev]" --quiet
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
# Browser used for auto-apply (Playwright's own Chromium; does not touch Riya's Chrome)
& $Py -m playwright install chromium
if ($LASTEXITCODE -ne 0) { Write-Host "Playwright browser install failed; auto-apply won't work until it succeeds." -ForegroundColor Yellow }

# 3. Local files that are never committed
if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env"; Write-Host "Created .env - add the API keys to it." -ForegroundColor Yellow }
if (-not (Test-Path "kb\answers.yaml")) { Copy-Item "kb\answers.example.yaml" "kb\answers.yaml"; Write-Host "Created kb\answers.yaml - fill it in." -ForegroundColor Yellow }
foreach ($d in @("data", "output", "logs", "credentials")) { New-Item -ItemType Directory -Force -Path $d | Out-Null }

# 4. Health check
& $Py -m jobagent doctor
Write-Host ""
Write-Host "Next steps (details in README.md):" -ForegroundColor Cyan
Write-Host "  1. notepad .env                 -> ANTHROPIC_API_KEY, APOLLO_API_KEY, CONTACT_EMAIL"
Write-Host "  2. notepad kb\answers.yaml      -> Riya's name, email, phone, LinkedIn (and check the rest)"
Write-Host "  3. .venv\Scripts\python -m jobagent doctor --online"
Write-Host "  4. .venv\Scripts\python -m jobagent run --limit 30                (first small run)"
Write-Host "  5. .venv\Scripts\python -m jobagent apply --dry-run --limit 3     (check the screenshots)"
Write-Host "  6. .venv\Scripts\python -m jobagent auth-gmail                    (connect Gmail once)"
Write-Host "  7. powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1"
