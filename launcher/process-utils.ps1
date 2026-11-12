function Test-ProjectProcess {
    param($ProcessInfo, [string]$Root, [ValidateSet('frontend', 'backend')][string]$Kind)
    if ($null -eq $ProcessInfo -or [string]::IsNullOrEmpty($ProcessInfo.CommandLine)) { return $false }
    $Command = $ProcessInfo.CommandLine.Replace('/', '\').ToLowerInvariant()
    $ExpectedRoot = [IO.Path]::GetFullPath($Root).TrimEnd('\').ToLowerInvariant() + '\'
    if (-not $Command.Contains($ExpectedRoot)) { return $false }
    if ($Kind -eq 'frontend') {
        return $ProcessInfo.Name -eq 'node.exe' -and $Command.Contains('\vite\bin\vite.js')
    }
    return $ProcessInfo.Name -eq 'python.exe' -and $Command.Contains('content_service.main:app')
}

function Test-PortOpen {
    param([int]$Port)
    $Client = New-Object Net.Sockets.TcpClient
    try { return $Client.ConnectAsync('127.0.0.1', $Port).Wait(500) -and $Client.Connected }
    catch { return $false }
    finally { $Client.Dispose() }
}

function Find-ProjectProcessByPort {
    # Fallback when Windows hides socket owners: this project's own process started with --port.
    param([int]$Port, [string]$Root, [string]$Kind)
    $Candidates = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
        $_.CommandLine -match "--port\s+`"?$Port`"?(\s|$)" -and (Test-ProjectProcess -ProcessInfo $_ -Root $Root -Kind $Kind)
    })
    # A venv python.exe re-launches the real interpreter; the listener is the innermost one.
    $Parents = @($Candidates | Select-Object -ExpandProperty ParentProcessId)
    foreach ($Info in @($Candidates | Where-Object { $Parents -notcontains $_.ProcessId })) {
        Get-Process -Id $Info.ProcessId -ErrorAction SilentlyContinue
    }
}

function Get-ProjectListener {
    param([int]$Port, [string]$Root, [string]$Kind)
    $Found = $false
    $Listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    foreach ($ProcessId in @($Listeners | Select-Object -ExpandProperty OwningProcess -Unique)) {
        # Some Windows setups hide socket owners and report PID 0. That is never one of our
        # processes, so it is resolved by command line below instead.
        if ($ProcessId -le 4) { continue }
        $Info = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction Stop
        if (-not (Test-ProjectProcess -ProcessInfo $Info -Root $Root -Kind $Kind)) {
            throw "Port $Port belongs to another process (PID $ProcessId). No process was stopped."
        }
        $Found = $true
        Get-Process -Id $ProcessId -ErrorAction Stop
    }
    if (-not $Found -and (Test-PortOpen -Port $Port)) {
        $Owner = @(Find-ProjectProcessByPort -Port $Port -Root $Root -Kind $Kind)
        if ($Owner.Count -ne 1) {
            throw "Port $Port is in use, but it could not be matched to this project's $Kind. No process was started or stopped."
        }
        $Owner[0]
    }
}

function New-ProcessRecord {
    param([Diagnostics.Process]$Process, [string]$Root, [string]$Kind)
    return [pscustomobject]@{
        id = $Process.Id
        startedAt = $Process.StartTime.ToUniversalTime().Ticks.ToString()
        root = [IO.Path]::GetFullPath($Root)
        kind = $Kind
    }
}

function Stop-ProjectProcess {
    param($Record, [string]$Root, [string]$Kind)
    $Process = Get-Process -Id $Record.id -ErrorAction SilentlyContinue
    if ($null -eq $Process) { return }
    $Info = Get-CimInstance Win32_Process -Filter "ProcessId=$($Process.Id)" -ErrorAction Stop
    # A venv parent may exit as soon as its child interpreter is stopped.
    $Process.Refresh()
    if ($Process.HasExited) { return }
    $StartedAt = $Process.StartTime
    if ($null -eq $StartedAt) {
        $Process.Refresh()
        if ($Process.HasExited) { return }
        throw "Cannot verify the start time of PID $($Process.Id). No process was stopped."
    }
    if ($Record.root -ne [IO.Path]::GetFullPath($Root) -or $Record.kind -ne $Kind -or
        $Record.startedAt -ne $StartedAt.ToUniversalTime().Ticks.ToString() -or
        -not (Test-ProjectProcess -ProcessInfo $Info -Root $Root -Kind $Kind)) {
        throw "Refusing to stop PID $($Process.Id): its recorded project or process identity no longer matches."
    }
    Stop-Process -InputObject $Process -ErrorAction Stop
    if (-not $Process.WaitForExit(10000)) { throw "PID $($Process.Id) did not exit within 10 seconds." }
}

function Open-LauncherLock {
    param([string]$LogRoot)
    New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null
    try {
        return [IO.File]::Open((Join-Path $LogRoot 'operation.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    }
    catch {
        throw 'Another start or stop operation is in progress. Wait for it to finish and retry.'
    }
}
