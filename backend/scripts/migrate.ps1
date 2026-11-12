param(
    [ValidateSet("upgrade", "downgrade")]
    [string]$Action = "upgrade",
    [string]$Revision = "head"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Virtual environment not found. Install requirements first."
}
if ($Action -eq "downgrade" -and $Revision -eq "head") {
    $Revision = "-1"
}

$Config = Join-Path $ProjectRoot "services\content_service\alembic.ini"
Write-Output "[content] alembic $Action $Revision"
& $Python -m alembic -c $Config $Action $Revision
if ($LASTEXITCODE -ne 0) {
    throw "Content migration failed with exit code $LASTEXITCODE."
}
