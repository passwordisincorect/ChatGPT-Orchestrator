$ErrorActionPreference = "Stop"

$ProfileName = "chatgpt-orchestrator"

$HealthUrl = "http://127.0.0.1:8082/healthz"

$Processes = @(Get-CimInstance Win32_Process -Filter "Name='tunnel-client.exe'" -ErrorAction SilentlyContinue | Where-Object {

    $_.CommandLine -and $_.CommandLine -match "(?i)\brun\b" -and $_.CommandLine -match "(?i)--profile" -and $_.CommandLine -match "chatgpt-orchestrator"

})

$Health = $false

try { $Health = ((Invoke-WebRequest -UseBasicParsing -Uri $HealthUrl -TimeoutSec 2).StatusCode -eq 200) } catch {}

[pscustomobject]@{

    profile = $ProfileName

    running = ($Processes.Count -gt 0)

    pids = @($Processes | ForEach-Object { [int]$_.ProcessId })

    health_ok = $Health

    health_url = $HealthUrl

} | ConvertTo-Json -Depth 4
