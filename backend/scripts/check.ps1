$ErrorActionPreference = "Continue"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Virtual environment not found at $Python"
}

Set-Location -LiteralPath $ProjectRoot
$failed = @()

function Invoke-Check {
    param(
        [string]$Name,
        [string[]]$Arguments
    )
    Write-Host ""
    Write-Host "===== $Name ====="
    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) {
        $script:failed += $Name
    }
}

Invoke-Check "ruff check" @("-m", "ruff", "check", "packages", "services", "tests", "scripts")
Invoke-Check "ruff format" @("-m", "ruff", "format", "--check", "packages", "services", "tests", "scripts")
Invoke-Check "mypy" @("-m", "mypy")
Invoke-Check "pytest+cov" @("-m", "pytest", "--cov", "--cov-report=term-missing")
# 内容侧未安装 transformers 4.x，不沿用会议侧的 CVE 忽略清单。
Invoke-Check "pip-audit" @("-m", "pip_audit", "--skip-editable")

Write-Host ""
if ($failed.Count) {
    Write-Host "Failed: $($failed -join ', ')"
    exit 1
}
Write-Host "All checks passed."
exit 0
