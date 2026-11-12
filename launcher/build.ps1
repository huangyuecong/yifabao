$ErrorActionPreference = 'Stop'
$LauncherRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $LauncherRoot
$Compiler = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$Source = Join-Path $LauncherRoot 'ContentLauncher.cs'
$Output = Join-Path $ProjectRoot ('AI' + -join @(
        [char]0x4E2A, [char]0x6027, [char]0x5316, [char]0x5185, [char]0x5BB9,
        [char]0x667A, [char]0x80FD, [char]0x4F53, [char]0x542F, [char]0x52A8, [char]0x5668, '.exe'
    ))

if (-not (Test-Path -LiteralPath $Compiler)) {
    throw "Cannot find .NET Framework C# compiler: $Compiler"
}

& $Compiler /nologo /target:winexe /platform:anycpu /optimize+ /codepage:65001 `
        /reference:System.dll `
        /reference:System.Core.dll `
        /reference:System.Windows.Forms.dll `
        /reference:System.Drawing.dll `
        /out:$Output `
        $Source (Join-Path $LauncherRoot 'ScriptRunner.cs')
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $Output)) {
    throw "Could not write $Output (close the running launcher if it is open)."
}
Write-Output "Launcher generated: $Output"
