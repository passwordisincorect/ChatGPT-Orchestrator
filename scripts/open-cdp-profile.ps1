$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$config = Get-Content ".\config\config.json" -Raw | ConvertFrom-Json
$edge = $config.edge_executable
if (-not $edge) {
    $edge = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
}
$profile = $config.cdp_profile_dir
if (-not $profile) {
    $profile = "data\edge-cdp-profile"
}
if (-not [System.IO.Path]::IsPathRooted($profile)) {
    $profile = Join-Path $Root $profile
}

New-Item -ItemType Directory -Force $profile | Out-Null
Start-Process -FilePath $edge -ArgumentList @(
    "--remote-debugging-port=0",
    "--remote-allow-origins=*",
    "--user-data-dir=$profile",
    "--no-first-run",
    "--no-default-browser-check",
    "--new-window",
    "https://chatgpt.com/"
)
Write-Host "Opened the dedicated ChatGPT-Orchestrator CDP profile."
Write-Host "Sign in to ChatGPT once in this window if needed, then run scripts\cdp-status.ps1."
