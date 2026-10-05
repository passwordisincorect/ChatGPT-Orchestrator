$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot

$KeyFile = Join-Path $ProjectRoot "deployment\control-plane-key.dpapi"

$LogDir = Join-Path $ProjectRoot "logs"

$TunnelClient = Join-Path $env:LOCALAPPDATA "OpenAI\TunnelClient\tunnel-client.exe"

$ProfileName = "chatgpt-orchestrator"



if (Test-Path $KeyFile) {

    $Protected = [System.IO.File]::ReadAllText($KeyFile).Trim()

    if ($Protected) {

        $Secure = ConvertTo-SecureString $Protected

        $Bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)

        try {

            $env:CONTROL_PLANE_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Bstr)

        } finally {

            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Bstr)

        }

    }

}



$Existing = @(Get-CimInstance Win32_Process -Filter "Name='tunnel-client.exe'" -ErrorAction SilentlyContinue | Where-Object {

    $_.CommandLine -and $_.CommandLine -match "(?i)\brun\b" -and $_.CommandLine -match "(?i)--profile" -and $_.CommandLine -match "chatgpt-orchestrator"

})

if ($Existing.Count -gt 0) { exit 0 }



New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

Start-Process -FilePath $TunnelClient -ArgumentList @("run","--profile",$ProfileName) -WindowStyle Hidden -RedirectStandardOutput (Join-Path $LogDir "tunnel.stdout.log") -RedirectStandardError (Join-Path $LogDir "tunnel.stderr.log") | Out-Null
