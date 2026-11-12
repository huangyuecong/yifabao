param(
    # Server and key come from parameters, ANDROID_SERVER_HOST / ANDROID_SERVER_SSH_KEY,
    # or the git-ignored open-server-android.local.psd1 (@{ ServerHost = '...'; KeyPath = '...' }).
    [string]$ServerHost = $env:ANDROID_SERVER_HOST,
    [ValidatePattern('^[A-Za-z_][A-Za-z0-9_-]*$')][string]$ServerUser = 'root',
    [string]$KeyPath = $env:ANDROID_SERVER_SSH_KEY,
    [string]$ScrcpyDirectory = (Join-Path $PSScriptRoot 'tools/scrcpy'),
    [ValidateRange(1024, 65535)][int]$LocalPort = 15555,
    [ValidateRange(1024, 65535)][int]$ServerAdbPort = 15555,
    [ValidateRange(1024, 65535)][int]$AdbServerPort = 15037,
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
$ProjectDirectory = Split-Path -Parent $PSScriptRoot
$LogDirectory = Join-Path $ProjectDirectory 'launcher-logs'
$ScrcpyExe = Join-Path $ScrcpyDirectory 'scrcpy.exe'
$AdbExe = Join-Path $ScrcpyDirectory 'adb.exe'
$SshCommand = Get-Command ssh.exe -ErrorAction SilentlyContinue
$TunnelProcess = $null
$AdbProcess = $null
$ScrcpyProcess = $null
$Failure = $null
$OriginalEnvironment = @{}

function Test-ListeningPort {
    param([int]$Port)
    return @([Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners() |
        Where-Object { $_.Port -eq $Port }).Count -gt 0
}

function Quote-NativeArgument {
    param([string]$Value)
    # Windows CommandLineToArgvW quoting, including paths containing spaces.
    return '"' + (($Value -replace '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"'
}

function Start-HiddenProcess {
    param([string]$Executable, [string[]]$Arguments, [string]$LogName)
    $QuotedArguments = ($Arguments | ForEach-Object { Quote-NativeArgument $_ }) -join ' '
    return Start-Process -FilePath $Executable -ArgumentList $QuotedArguments -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogDirectory "$RunId-$LogName.out.log") `
        -RedirectStandardError (Join-Path $LogDirectory "$RunId-$LogName.err.log")
}

function Wait-Listener {
    param([Diagnostics.Process]$Process, [int]$Port, [string]$Label)
    $Deadline = [DateTime]::UtcNow.AddSeconds(15)
    while ([DateTime]::UtcNow -lt $Deadline) {
        $Process.Refresh()
        if ($Process.HasExited) { throw "$Label exited (code $($Process.ExitCode)). See logs: $LogDirectory" }
        if (Test-ListeningPort $Port) { return }
        Start-Sleep -Milliseconds 200
    }
    throw "$Label did not become ready within 15 seconds. See logs: $LogDirectory"
}

function Stop-OwnedProcess {
    param([Diagnostics.Process]$Process)
    if ($null -eq $Process) { return }
    try {
        $Process.Refresh()
        if (-not $Process.HasExited) {
            $Process.Kill()
            [void]$Process.WaitForExit(5000)
        }
    }
    catch { Write-Warning "Could not close owned process: $($_.Exception.Message)" }
    finally { $Process.Dispose() }
}

try {
    $LocalSettings = Join-Path $PSScriptRoot 'open-server-android.local.psd1'
    if ((-not $ServerHost -or -not $KeyPath) -and (Test-Path -LiteralPath $LocalSettings -PathType Leaf)) {
        $Saved = Import-PowerShellDataFile -LiteralPath $LocalSettings
        if (-not $ServerHost) { $ServerHost = [string]$Saved.ServerHost }
        if (-not $KeyPath) { $KeyPath = [string]$Saved.KeyPath }
    }
    if ($ServerHost -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*$') {
        throw 'Set the Android server with -ServerHost, ANDROID_SERVER_HOST or open-server-android.local.psd1.'
    }
    $Missing = @()
    if ($null -eq $SshCommand) { $Missing += 'Windows OpenSSH client (ssh.exe)' }
    if (-not $KeyPath -or -not (Test-Path -LiteralPath $KeyPath -PathType Leaf)) { $Missing += "SSH private key file: $KeyPath" }
    if (-not (Test-Path -LiteralPath $ScrcpyExe -PathType Leaf)) { $Missing += "Official portable scrcpy: $ScrcpyExe" }
    if (-not (Test-Path -LiteralPath $AdbExe -PathType Leaf)) { $Missing += "Bundled adb.exe: $AdbExe" }
    if ($LocalPort -eq $AdbServerPort) { throw 'LocalPort and AdbServerPort must be different.' }
    if ($Missing.Count -gt 0) { throw ('Missing dependencies:' + [Environment]::NewLine + ($Missing -join [Environment]::NewLine)) }
    $ScrcpyDirectory = (Resolve-Path -LiteralPath $ScrcpyDirectory).Path
    $ScrcpyExe = Join-Path $ScrcpyDirectory 'scrcpy.exe'
    $AdbExe = Join-Path $ScrcpyDirectory 'adb.exe'
    if ($CheckOnly) {
        Write-Output 'Dependency check passed. No connection, process, or Android screen was started.'
        Write-Output "Target: $ServerUser@$ServerHost; local forwarding: 127.0.0.1:${LocalPort}; private ADB server: 127.0.0.1:${AdbServerPort}"
        exit 0
    }

    foreach ($Port in @($LocalPort, $AdbServerPort)) {
        if (Test-ListeningPort $Port) {
            throw "Local port $Port is already in use. Close the existing Android viewer or choose another port. No existing process was stopped."
        }
    }
    New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
    $RunId = 'server-android-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + $PID
    Write-Output "Connecting to server Android. Logs: $LogDirectory/$RunId-*.log"

    # Strict host verification relies on the existing trusted known_hosts entry.
    $SshArguments = @('-N', '-T', '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
        '-o', 'StrictHostKeyChecking=yes', '-o', 'ExitOnForwardFailure=yes',
        '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
        '-i', (Resolve-Path -LiteralPath $KeyPath).Path,
        '-L', "127.0.0.1:${LocalPort}:127.0.0.1:${ServerAdbPort}", "$ServerUser@$ServerHost")
    $TunnelProcess = Start-HiddenProcess $SshCommand.Source $SshArguments 'ssh'
    Wait-Listener $TunnelProcess $LocalPort 'SSH tunnel'

    # A dedicated foreground ADB server gives this launcher ownership of its lifecycle.
    # Override inherited remote ADB settings only within this PowerShell process.
    $EnvironmentOverrides = @{
        ADB = $AdbExe
        ADB_SERVER_SOCKET = "tcp:127.0.0.1:${AdbServerPort}"
        ANDROID_ADB_SERVER_ADDRESS = '127.0.0.1'
        ANDROID_ADB_SERVER_PORT = [string]$AdbServerPort
        ADB_MDNS_AUTO_CONNECT = '0'
        ADB_LOCAL_TRANSPORT_MAX_PORT = '0'
    }
    foreach ($Name in $EnvironmentOverrides.Keys) {
        $OriginalEnvironment[$Name] = [Environment]::GetEnvironmentVariable($Name, 'Process')
        [Environment]::SetEnvironmentVariable($Name, $EnvironmentOverrides[$Name], 'Process')
    }
    # ADB's server listener accepts tcp:PORT and binds loopback unless -a is used.
    $AdbProcess = Start-HiddenProcess $AdbExe @('-L', "tcp:${AdbServerPort}", '--one-device', 'server-android-no-usb', 'server', 'nodaemon') 'adb-server'
    Wait-Listener $AdbProcess $AdbServerPort 'Private ADB server'

    $DeviceSerial = "127.0.0.1:${LocalPort}"
    $ConnectProcess = Start-HiddenProcess $AdbExe @('connect', $DeviceSerial) 'adb-connect'
    try {
        if (-not $ConnectProcess.WaitForExit(20000)) {
            $ConnectProcess.Kill()
            throw 'ADB connect timed out after 20 seconds. Check server Android status and SSH logs.'
        }
        if ($ConnectProcess.ExitCode -ne 0) { throw "ADB connect failed. See $RunId-adb-connect logs." }
    }
    finally { $ConnectProcess.Dispose() }

    # adb connect may return zero even when its output reports a connection failure.
    $StateProcess = Start-HiddenProcess $AdbExe @('-s', $DeviceSerial, 'get-state') 'adb-state'
    try {
        if (-not $StateProcess.WaitForExit(10000)) {
            $StateProcess.Kill()
            throw 'Android device state check timed out. See the adb-state logs.'
        }
        $DeviceState = ([string](Get-Content -LiteralPath (Join-Path $LogDirectory "$RunId-adb-state.out.log") -Raw)).Trim()
        if ($StateProcess.ExitCode -ne 0 -or $DeviceState -ne 'device') {
            throw "Android is not connected and ready. See $RunId-adb-connect and $RunId-adb-state logs."
        }
    }
    finally { $StateProcess.Dispose() }

    Write-Output 'The Android screen is opening. Close its window to end this session and its SSH tunnel.'
    $ScrcpyArguments = @('--serial', $DeviceSerial, '--window-title', 'Server Android', '--no-audio', '--max-size=1280', '--max-fps=15', '--video-bit-rate=2M')
    $ScrcpyProcess = Start-Process -FilePath $ScrcpyExe -WorkingDirectory $ScrcpyDirectory `
        -ArgumentList (($ScrcpyArguments | ForEach-Object { Quote-NativeArgument $_ }) -join ' ') `
        -PassThru -WindowStyle Normal `
        -RedirectStandardOutput (Join-Path $LogDirectory "$RunId-scrcpy.out.log") `
        -RedirectStandardError (Join-Path $LogDirectory "$RunId-scrcpy.err.log")
    while (-not $ScrcpyProcess.WaitForExit(500)) {
        $TunnelProcess.Refresh()
        $AdbProcess.Refresh()
        if ($TunnelProcess.HasExited -or $AdbProcess.HasExited) { throw 'The SSH tunnel or private ADB server stopped. See session logs.' }
    }
    if ($ScrcpyProcess.ExitCode -ne 0) { throw "Android viewer exited with code $($ScrcpyProcess.ExitCode). See $RunId-scrcpy.err.log." }
}
catch {
    $Failure = $_.Exception.Message
    Write-Host $Failure -ForegroundColor Red
}
finally {
    Stop-OwnedProcess $ScrcpyProcess
    Stop-OwnedProcess $AdbProcess
    Stop-OwnedProcess $TunnelProcess
    foreach ($Name in $OriginalEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($Name, $OriginalEnvironment[$Name], 'Process')
    }
}
if ($null -ne $Failure) { exit 1 }
