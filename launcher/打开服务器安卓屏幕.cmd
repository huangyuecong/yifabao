@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0open-server-android.ps1" %*
if errorlevel 1 (
  echo.
  echo Android viewer could not start. Read the error above and launcher-logs.
  pause
  exit /b 1
)
