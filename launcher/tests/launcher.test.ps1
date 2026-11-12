$ErrorActionPreference = 'Stop'
$LauncherRoot = Split-Path -Parent $PSScriptRoot
$ProjectRoot = Split-Path -Parent $LauncherRoot
$FixtureRoot = Join-Path $PSScriptRoot 'fixtures'
. (Join-Path $LauncherRoot 'process-utils.ps1')
$Passed = 0

function Assert-True {
    param([bool]$Value, [string]$Label)
    if (-not $Value) { throw "FAIL: $Label" }
    $script:Passed++
    Write-Output "PASS: $Label"
}

function Assert-Rejected {
    param([scriptblock]$Action, [string]$Label)
    $Rejected = $false
    try { & $Action | Out-Null }
    catch { $Rejected = $true }
    Assert-True $Rejected $Label
}

$Frontend = [pscustomobject]@{
    Name = 'node.exe'
    CommandLine = 'node "' + (Join-Path $FixtureRoot 'vite\bin\vite.js') + '"'
}
Assert-True (Test-ProjectProcess $Frontend $FixtureRoot frontend) 'recognizes project frontend'
Assert-True (-not (Test-ProjectProcess $Frontend ($FixtureRoot + '-other') frontend)) 'rejects another project'
Assert-True (-not (Test-ProjectProcess $Frontend $FixtureRoot backend)) 'rejects wrong service kind'
Assert-True (-not (Test-ProjectProcess $null $FixtureRoot frontend)) 'rejects missing process'

foreach ($File in @(Get-ChildItem -LiteralPath $LauncherRoot -Filter '*.ps1') +
    @(Get-ChildItem -LiteralPath (Join-Path $ProjectRoot 'backend\scripts') -Filter '*-dev.ps1')) {
    $Tokens = $null
    $Errors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile($File.FullName, [ref]$Tokens, [ref]$Errors)
    Assert-True ($Errors.Count -eq 0) "PowerShell syntax: $($File.Name)"
}

$TestLogs = Join-Path $ProjectRoot 'launcher-logs\tests'
$Lock = Open-LauncherLock $TestLogs
try {
    Assert-Rejected { $Other = Open-LauncherLock $TestLogs; $Other.Dispose() } 'serializes start and stop operations'
}
finally { $Lock.Dispose() }
$Lock = Open-LauncherLock $TestLogs
$Lock.Dispose()
Assert-True $true 'releases lock after operation'

$Fixture = Start-Process -FilePath (Get-Command node.exe).Source -ArgumentList @(
    ('"' + (Join-Path $FixtureRoot 'vite\bin\vite.js') + '"')
) -WindowStyle Hidden -PassThru
try {
    $Deadline = (Get-Date).AddSeconds(15)
    do {
        $Listener = Get-NetTCPConnection -State Listen -ErrorAction Stop |
            Where-Object OwningProcess -eq $Fixture.Id | Select-Object -First 1
        if ($null -eq $Listener) { Start-Sleep -Milliseconds 200 }
    } while ($null -eq $Listener -and (Get-Date) -lt $Deadline)
    Assert-True ($null -ne $Listener) 'test-owned process is listening'
    Assert-Rejected { Get-ProjectListener -Port $Listener.LocalPort -Root ($FixtureRoot + '-other') -Kind frontend } 'rejects occupied port owned by another project'
    Assert-True ((Get-ProjectListener -Port $Listener.LocalPort -Root $FixtureRoot -Kind frontend).Id -eq $Fixture.Id) 'accepts verified project listener'
    $Record = New-ProcessRecord $Fixture $FixtureRoot frontend
    $Record.startedAt = '0'
    Assert-Rejected { Stop-ProjectProcess $Record $FixtureRoot frontend } 'rejects stale PID start time'
    $Fixture.Refresh()
    Assert-True (-not $Fixture.HasExited) 'stale PID refusal leaves process alive'
    $Record = New-ProcessRecord $Fixture $FixtureRoot frontend
    Stop-ProjectProcess $Record $FixtureRoot frontend
    Assert-True $Fixture.HasExited 'stops only the verified process'
    Stop-ProjectProcess $Record $FixtureRoot frontend
    Assert-True $true 'repeated stop is harmless'
}
finally {
    if (-not $Fixture.HasExited) { Stop-Process -InputObject $Fixture }
}

$Fixture = Start-Process -FilePath (Get-Command node.exe).Source -ArgumentList @(
    ('"' + (Join-Path $FixtureRoot 'vite\bin\vite.js') + '"')
) -WindowStyle Hidden -PassThru
try {
    $Record = New-ProcessRecord $Fixture $FixtureRoot frontend
    # Reproduce a venv parent exiting between the process and CIM snapshots.
    function Get-CimInstance {
        param($ClassName, $Filter, $ErrorAction)
        $Info = CimCmdlets\Get-CimInstance -ClassName $ClassName -Filter $Filter -ErrorAction $ErrorAction
        Stop-Process -InputObject $Fixture
        [void]$Fixture.WaitForExit(5000)
        return $Info
    }
    Stop-ProjectProcess $Record $FixtureRoot frontend
    Assert-True $Fixture.HasExited 'concurrent process exit is harmless during cleanup'
}
finally {
    Remove-Item Function:Get-CimInstance -ErrorAction SilentlyContinue
    if (-not $Fixture.HasExited) { Stop-Process -InputObject $Fixture }
}

$Check = Start-Process -FilePath (Get-Command node.exe).Source -ArgumentList @(
    ('"' + (Join-Path $LauncherRoot 'check-frontend.mjs') + '"'),
    ('"' + (Join-Path $FixtureRoot 'missing') + '"')
) -WindowStyle Hidden -PassThru -Wait `
    -RedirectStandardOutput (Join-Path $TestLogs 'missing.out.log') `
    -RedirectStandardError (Join-Path $TestLogs 'missing.err.log')
Assert-True ($Check.ExitCode -eq 1) 'missing dependency fails preflight'
Assert-True ((Get-Content -LiteralPath (Join-Path $TestLogs 'missing.err.log') -Raw).Contains('missing-launcher-test-package')) 'failure identifies the exact missing dependency'
Assert-True (-not (Test-Path -LiteralPath (Join-Path $FixtureRoot 'missing\node_modules'))) 'preflight does not install dependencies'

$RootExe = @(Get-ChildItem -LiteralPath $ProjectRoot -Filter '*.exe')
Assert-True ($RootExe.Count -eq 1) 'one root launcher executable'
Assert-True (-not (Test-Path -LiteralPath (Join-Path $LauncherRoot 'ContentLauncher.exe'))) 'duplicate executable removed'
Assert-True (@(Get-ChildItem -LiteralPath $ProjectRoot -Filter '*.cmd').Count -eq 0) 'duplicate CMD entry removed'

$Compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$RunnerTests = Join-Path $TestLogs 'ScriptRunnerTests.exe'
& $Compiler /nologo /target:exe /out:$RunnerTests (Join-Path $LauncherRoot 'ScriptRunner.cs') (Join-Path $PSScriptRoot 'ScriptRunnerTests.cs')
Assert-True ($LASTEXITCODE -eq 0) 'script runner tests compile'
& $RunnerTests
Assert-True ($LASTEXITCODE -eq 0) 'script runner timeout, cancellation, and pipe regressions'
Write-Output "$Passed launcher checks passed."
