param(
    [ValidateRange(1024, 65535)][int]$FrontendPort = 8200,
    [ValidateRange(1024, 65535)][int]$BackendPort = 8002
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$FrontendRoot = Join-Path $ProjectRoot 'frontend'
$BackendRoot = Join-Path $ProjectRoot 'backend'
$LogRoot = Join-Path $ProjectRoot 'launcher-logs'
. (Join-Path $PSScriptRoot 'process-utils.ps1')
$Lock = Open-LauncherLock -LogRoot $LogRoot
try {
    $Listener = Get-ProjectListener -Port $FrontendPort -Root $FrontendRoot -Kind frontend
    $PidFile = Join-Path $LogRoot "frontend-$FrontendPort.json"
    $Record = $null
    if (Test-Path -LiteralPath $PidFile) {
        $Record = Get-Content -LiteralPath $PidFile -Raw | ConvertFrom-Json
    }
    if ($null -ne $Record) { Stop-ProjectProcess -Record $Record -Root $FrontendRoot -Kind frontend }
    if ($null -ne $Listener -and ($null -eq $Record -or $Listener.Id -ne $Record.id)) {
        Stop-ProjectProcess -Record (New-ProcessRecord $Listener $FrontendRoot frontend) -Root $FrontendRoot -Kind frontend
    }
    if (Test-Path -LiteralPath $PidFile) { Remove-Item -LiteralPath $PidFile }
    & (Join-Path $BackendRoot 'scripts\stop-dev.ps1') -Port $BackendPort
    Write-Output 'Content frontend and backend have stopped.'
}
finally { $Lock.Dispose() }
