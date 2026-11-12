param([ValidateRange(1024, 65535)][int]$Port = 8002)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path (Split-Path -Parent $ProjectRoot) 'launcher\process-utils.ps1')
$PidFile = Join-Path $ProjectRoot "work\logs\processes-$Port.json"
# Inspect the listener as well, including services started by the old launcher.
$Listener = Get-ProjectListener -Port $Port -Root $ProjectRoot -Kind backend
$Records = @()
if (Test-Path -LiteralPath $PidFile) {
    $Records = @((Get-Content -Raw -LiteralPath $PidFile | ConvertFrom-Json).processes)
}
if ($null -ne $Listener -and $Listener.Id -notin @($Records.id)) {
    $Records += New-ProcessRecord -Process $Listener -Root $ProjectRoot -Kind backend
}
foreach ($Record in $Records) {
    Stop-ProjectProcess -Record $Record -Root $ProjectRoot -Kind backend
}
if (Test-Path -LiteralPath $PidFile) { Remove-Item -LiteralPath $PidFile }
if ($Port -eq 8002) {
    $LegacyFile = Join-Path $ProjectRoot 'work\logs\processes.json'
    if (Test-Path -LiteralPath $LegacyFile) {
        Remove-Item -LiteralPath $LegacyFile
    }
}
Write-Output "Content development service stopped on port $Port."
