param(
    [ValidateRange(1, 100)]
    [int]$Iterations = 5
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv/Scripts/python.exe"
$Output = Join-Path $Root "data/soak-v063-last.json"
$Suites = @(
    "tests.test_edge_hybrid_adapter",
    "tests.test_edge_tabs_adapter",
    "tests.test_async_jobs",
    "tests.test_autonomy"
)

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python venv not found: $Python"
}

$started = Get-Date
$passed = 0
$failedIteration = $null

for ($i = 1; $i -le $Iterations; $i++) {
    Write-Host "[soak] iteration $i/$Iterations"
    & $Python -m unittest @Suites
    if ($LASTEXITCODE -ne 0) {
        $failedIteration = $i
        break
    }
    $passed++
}

$finished = Get-Date
$version = (& $Python -c "import chatgpt_orchestrator; print(chatgpt_orchestrator.__version__)").Trim()
$result = [ordered]@{
    version = $version
    requested_iterations = $Iterations
    passed_iterations = $passed
    failed_iteration = $failedIteration
    success = ($passed -eq $Iterations)
    started_at = $started.ToString("o")
    finished_at = $finished.ToString("o")
    elapsed_seconds = [math]::Round(($finished - $started).TotalSeconds, 3)
    suites = $Suites
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Output) | Out-Null
$result | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $Output -Encoding UTF8
$result | ConvertTo-Json -Depth 4

if (-not $result.success) {
    exit 1
}
