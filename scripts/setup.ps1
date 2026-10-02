$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    py -3.12 -m venv .venv
}

& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& ".\.venv\Scripts\python.exe" -m pip install -e .
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "ChatGPT-Orchestrator setup complete."
