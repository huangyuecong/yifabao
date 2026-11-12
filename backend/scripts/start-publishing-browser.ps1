[CmdletBinding()]
param(
    [ValidateRange(1024, 65535)]
    [int]$Port = 9222,
    [string]$ProfileDirectory = '',
    [string]$BrowserPath = '',
    [ValidateSet('xianyu', 'douyin', 'xiaohongshu', 'kuaishou')]
    [string[]]$Platforms = @('xianyu', 'douyin', 'xiaohongshu', 'kuaishou')
)

$ErrorActionPreference = 'Stop'

if (-not $ProfileDirectory) {
    $backendRoot = Split-Path -Parent $PSScriptRoot
    $ProfileDirectory = Join-Path $backendRoot '.data\content\private\publishing-browser-profile'
}
$ProfileDirectory = [IO.Path]::GetFullPath($ProfileDirectory)
if ($ProfileDirectory.Contains('"')) {
    throw 'ProfileDirectory cannot contain quotes.'
}

if (-not $BrowserPath) {
    $browserCandidates = @(
        "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
        "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
        "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
        "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
    )
    $BrowserPath = $browserCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
}
if (-not $BrowserPath -or -not (Test-Path -LiteralPath $BrowserPath -PathType Leaf)) {
    throw 'Chrome or Edge was not found. Supply -BrowserPath with its executable path.'
}

if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    throw "Port $Port is already listening. Reuse that browser or choose another port; no existing process was stopped."
}
New-Item -ItemType Directory -Path $ProfileDirectory -Force | Out-Null
$browserArguments = @(
    "--remote-debugging-port=$Port",
    '--remote-debugging-address=127.0.0.1',
    "--user-data-dir=`"$ProfileDirectory`"",
    '--no-first-run',
    '--no-default-browser-check'
)
foreach ($platform in ($Platforms | Select-Object -Unique)) {
    if ($platform -eq 'xianyu') {
        $browserArguments += 'https://www.goofish.com/publish'
    } elseif ($platform -eq 'douyin') {
        $browserArguments += 'https://creator.douyin.com/'
    } elseif ($platform -eq 'xiaohongshu') {
        $browserArguments += 'https://creator.xiaohongshu.com/'
    } elseif ($platform -eq 'kuaishou') {
        $browserArguments += 'https://cp.kuaishou.com/'
    }
}
# This window is intentionally visible: the operator must log in or resolve a
# platform challenge. This script never reads/copies the normal browser profile.
Start-Process -FilePath $BrowserPath -ArgumentList $browserArguments -WindowStyle Normal | Out-Null
Write-Host "Publishing browser started at http://127.0.0.1:$Port."
Write-Host 'Log in to the platforms in this dedicated window. Sessions persist in the dedicated profile.'
Write-Host 'Use one dedicated profile and debugging port per account; keep this window open while publishing.'
