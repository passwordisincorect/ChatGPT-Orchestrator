$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root
& ".\.venv\Scripts\python.exe" -m chatgpt_orchestrator.server
