param(
    [ValidateRange(1024, 65535)][int]$Port = 8002,
    [ValidateRange(5, 360)][int]$TimeoutSeconds = 180,
    [switch]$PassThru
)

$ErrorActionPreference = "Stop"
$OutputEncoding = [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$LogRoot = Join-Path $ProjectRoot "work\logs"
New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null
. (Join-Path (Split-Path -Parent $ProjectRoot) 'launcher\process-utils.ps1')
$PidFile = Join-Path $LogRoot "processes-$Port.json"
$ErrorLog = Join-Path $LogRoot "content-$Port.err.log"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Virtual environment not found at $Python. Restore backend\.venv or run: .\.venv\Scripts\python -m pip install -r requirements.txt"
}

function Test-ContentHealth {
    try {
        $Response = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3
        return $Response.status -eq "ok" -and $Response.service -eq "content-service"
    }
    catch {
        return $false
    }
}

$Existing = Get-ProjectListener -Port $Port -Root $ProjectRoot -Kind backend
if ($null -ne $Existing -and -not (Test-ContentHealth)) {
    throw "Port $Port is already in use, but content-service did not pass its health check."
}

$StartedProcess = $null
try {
if ($null -eq $Existing) {
    $Arguments = @(
        "-m", "uvicorn", "content_service.main:app",
        "--app-dir", ('"' + (Join-Path $ProjectRoot "services\content_service\src") + '"'),
        "--host", "127.0.0.1", "--port", "$Port", "--no-proxy-headers"
    )
    $EnvFile = Join-Path $ProjectRoot ".env"
    if (Test-Path -LiteralPath $EnvFile) {
        $Arguments += @("--env-file", ('"' + $EnvFile + '"'))
    }
    $StartedProcess = Start-Process -FilePath $Python -ArgumentList $Arguments -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $LogRoot "content-$Port.out.log") `
        -RedirectStandardError $ErrorLog

    $Deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $Deadline) {
        if (Test-ContentHealth) {
            $Existing = Get-ProjectListener -Port $Port -Root $ProjectRoot -Kind backend
            if ($null -eq $Existing) {
                # Some Windows setups hide listening sockets from Get-NetTCPConnection. The port
                # was free when we started, so a live process we launched is the one answering.
                $StartedProcess.Refresh()
                if (-not $StartedProcess.HasExited) { $Existing = $StartedProcess }
            }
            break
        }
        $StartedProcess.Refresh()
        if ($StartedProcess.HasExited) {
            $Tail = if (Test-Path -LiteralPath $ErrorLog) {
                (Get-Content -LiteralPath $ErrorLog -Tail 20) -join [Environment]::NewLine
            }
            else {
                "No error log was created."
            }
            throw "content-service exited during startup (code $($StartedProcess.ExitCode)).`n$Tail"
        }
        Start-Sleep -Seconds 1
    }
}
if ($null -eq $Existing -or -not (Test-ContentHealth)) {
    throw "content-service did not become healthy on port $Port within $TimeoutSeconds seconds."
}

$Records = @(New-ProcessRecord -Process $Existing -Root $ProjectRoot -Kind backend)
if ($null -ne $StartedProcess -and -not $StartedProcess.HasExited -and $StartedProcess.Id -ne $Existing.Id) {
    $Records += New-ProcessRecord -Process $StartedProcess -Root $ProjectRoot -Kind backend
}
@{
    processes = $Records
} | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 -LiteralPath $PidFile

Write-Host "Content API ready on $Port (PID $($Existing.Id))"
if ($PassThru) {
    [pscustomobject]@{ started = $null -ne $StartedProcess; processes = $Records }
}
}
catch {
    # Windows venv Python can delegate to a child interpreter. Clean up both.
    if ($null -ne $StartedProcess -and -not $StartedProcess.HasExited) {
        $Children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($StartedProcess.Id)")
        foreach ($Child in $Children) {
            if (Test-ProjectProcess -ProcessInfo $Child -Root $ProjectRoot -Kind backend) {
                $Process = Get-Process -Id $Child.ProcessId -ErrorAction SilentlyContinue
                if ($null -ne $Process) {
                    Stop-ProjectProcess -Record (New-ProcessRecord $Process $ProjectRoot backend) -Root $ProjectRoot -Kind backend
                }
            }
        }
        if (-not $StartedProcess.HasExited) {
            Stop-ProjectProcess -Record (New-ProcessRecord $StartedProcess $ProjectRoot backend) -Root $ProjectRoot -Kind backend
        }
    }
    throw
}
