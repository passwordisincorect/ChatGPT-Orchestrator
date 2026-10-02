$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

& ".\.venv\Scripts\python.exe" -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& ".\.venv\Scripts\python.exe" -m chatgpt_orchestrator.doctor
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
