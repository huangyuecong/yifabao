param(
    [switch]$NoOpen,
    [switch]$CheckOnly,
    [ValidateRange(1024, 65535)][int]$FrontendPort = 8200,
    [ValidateRange(1024, 65535)][int]$BackendPort = 8002,
    [ValidateRange(5, 360)][int]$TimeoutSeconds = 180
)

$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BackendRoot = Join-Path $ProjectRoot 'backend'
$FrontendRoot = Join-Path $ProjectRoot 'frontend'
$LogRoot = Join-Path $ProjectRoot 'launcher-logs'
$FrontendBase = "http://127.0.0.1:$FrontendPort/content"
$FrontendUrl = "$FrontendBase/"
$FrontendErr = Join-Path $LogRoot "frontend-$FrontendPort.err.log"
$PidFile = Join-Path $LogRoot "frontend-$FrontendPort.json"
. (Join-Path $PSScriptRoot 'process-utils.ps1')

function Invoke-HttpCheck {
    param(
        [Parameter(Mandatory)][string]$Uri,
        [int]$TimeoutMs = 5000
    )
    $Request = [Net.HttpWebRequest]::Create($Uri)
    $Request.Method = 'GET'
    $Request.Timeout = $TimeoutMs
    $Request.ReadWriteTimeout = $TimeoutMs
    $Request.KeepAlive = $false
    $Request.AllowAutoRedirect = $true
    try {
        $Response = $Request.GetResponse()
        try {
            $Reader = New-Object IO.StreamReader($Response.GetResponseStream())
            try {
                return [pscustomobject]@{
                    Content = $Reader.ReadToEnd()
                    ContentType = [string]$Response.ContentType
                    Status = [int]$Response.StatusCode
                }
            }
            finally { $Reader.Dispose() }
        }
        finally { $Response.Close() }
    }
    catch [Net.WebException] {
        if ($null -ne $_.Exception.Response) { $_.Exception.Response.Close() }
        throw
    }
}

function Wait-FrontendReady {
    param([Diagnostics.Process]$Process)
    $Deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $LastError = 'No response'
    $LastLog = Get-Date
    Write-Output "Waiting for frontend at $FrontendUrl (timeout ${TimeoutSeconds}s)..."
    while ((Get-Date) -lt $Deadline) {
        $Process.Refresh()
        if ($Process.HasExited) {
            $Tail = if (Test-Path -LiteralPath $FrontendErr) {
                (Get-Content -LiteralPath $FrontendErr -Tail 25) -join [Environment]::NewLine
            }
            throw "Frontend exited during startup (code $($Process.ExitCode)).`n$Tail"
        }
        try {
            $Page = Invoke-HttpCheck -Uri $FrontendUrl -TimeoutMs 3000
            if ($Page.Content -notmatch '/src/main.ts') { throw 'The frontend entry page is not the expected Vite application.' }
            foreach ($Path in @('src/main.ts', 'src/views/agent/ContentWorkbench.vue', 'src/styles/brand-theme.scss')) {
                $Module = Invoke-HttpCheck -Uri "$FrontendBase/$Path" -TimeoutMs 10000
                if ($Module.ContentType -notmatch '(javascript|ecmascript)') {
                    throw "Frontend module did not compile: $Path"
                }
            }
            $HealthRaw = Invoke-HttpCheck -Uri "http://127.0.0.1:$FrontendPort/content/api/health" -TimeoutMs 3000
            $Health = $HealthRaw.Content | ConvertFrom-Json
            if ($Health.service -ne 'content-service' -or $Health.status -ne 'ok') { throw 'Content API proxy health check failed.' }
            return
        }
        catch {
            $LastError = $_.Exception.Message
            if (((Get-Date) - $LastLog).TotalSeconds -ge 5) {
                Write-Output "Frontend is still starting: $LastError"
                $LastLog = Get-Date
            }
        }
        Start-Sleep -Seconds 1
    }
    throw "Frontend did not become ready within $TimeoutSeconds seconds: $LastError. See $FrontendErr"
}

if ($FrontendPort -eq $BackendPort) { throw 'Frontend and backend ports must be different.' }
$Lock = Open-LauncherLock -LogRoot $LogRoot
$NewFrontend = $null
$StartedBackendRecords = @()
try {
    Write-Output "Project root: $ProjectRoot"
    $Node = (Get-Command node.exe -ErrorAction Stop).Source
    $Python = Join-Path $BackendRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $Python)) { throw "Backend Python is missing: $Python" }
    & $Python -c 'import uvicorn, fastapi, dotenv'
    if ($LASTEXITCODE -ne 0) { throw 'Backend dependencies are incomplete. Restore the backend virtual environment.' }

    Write-Output 'Checking frontend dependencies before starting any services...'
    & $Node (Join-Path $PSScriptRoot 'check-frontend.mjs') $FrontendRoot
    if ($LASTEXITCODE -ne 0) {
        throw 'Frontend preflight failed. No services were started. See the missing packages above; restore dependencies before retrying.'
    }
    if ($CheckOnly) {
        Write-Output 'Dependency checks passed. No services were started.'
        return
    }

    $Frontend = Get-ProjectListener -Port $FrontendPort -Root $FrontendRoot -Kind frontend
    $BackendResult = & (Join-Path $BackendRoot 'scripts\run-dev.ps1') -Port $BackendPort -TimeoutSeconds $TimeoutSeconds -PassThru
    if ($BackendResult.started) { $StartedBackendRecords = @($BackendResult.processes) }

    if ($null -eq $Frontend) {
        $OldProxy = $env:VITE_CONTENT_PROXY_TARGET
        $OldOpen = $env:VITE_OPEN
        try {
            $env:VITE_CONTENT_PROXY_TARGET = "http://127.0.0.1:$BackendPort"
            $env:VITE_OPEN = 'false'
            $Vite = Join-Path $FrontendRoot 'node_modules\vite\bin\vite.js'
            Write-Output "Starting content frontend on port $FrontendPort..."
            $NewFrontend = Start-Process -FilePath $Node -ArgumentList @(
                "`"$Vite`"", '--mode', 'dev', '--host', '127.0.0.1', '--port', "$FrontendPort", '--strictPort'
            ) -WorkingDirectory $FrontendRoot -WindowStyle Hidden -PassThru `
                -RedirectStandardOutput (Join-Path $LogRoot "frontend-$FrontendPort.log") -RedirectStandardError $FrontendErr
            $Frontend = $NewFrontend
        }
        finally {
            $env:VITE_CONTENT_PROXY_TARGET = $OldProxy
            $env:VITE_OPEN = $OldOpen
        }
    }
    else { Write-Output "Reusing this project's frontend (PID $($Frontend.Id))." }

    Wait-FrontendReady -Process $Frontend
    New-ProcessRecord -Process $Frontend -Root $FrontendRoot -Kind frontend |
        ConvertTo-Json | Set-Content -LiteralPath $PidFile -Encoding UTF8
    Write-Output "Content agent is ready: $FrontendUrl"
    if (-not $NoOpen) { Start-Process $FrontendUrl }
}
catch {
    $StartupError = $_
    if ($null -ne $NewFrontend -and -not $NewFrontend.HasExited) {
        try {
            Stop-ProjectProcess -Record (New-ProcessRecord $NewFrontend $FrontendRoot frontend) -Root $FrontendRoot -Kind frontend
        }
        catch { Write-Warning "Frontend cleanup failed: $($_.Exception.Message)" }
    }
    foreach ($Record in $StartedBackendRecords) {
        try { Stop-ProjectProcess -Record $Record -Root $BackendRoot -Kind backend }
        catch { Write-Warning "Backend cleanup failed: $($_.Exception.Message)" }
    }
    throw $StartupError
}
finally { $Lock.Dispose() }
