@echo off
setlocal EnableExtensions
rem Personal Shopping Agent one-click Windows setup. It only runs the project's public commands.
cd /d "%~dp0"
echo Personal Shopping Agent - Windows setup
echo.

where uv >nul 2>nul
if errorlevel 1 (
  echo uv was not found. uv installs Python 3.12 and this project's locked dependencies.
  choice /C YN /M "Run the official uv installer from https://astral.sh/uv/install.ps1 now"
  if errorlevel 2 goto :cancelled
  powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
  if errorlevel 1 goto :failed
  set "PATH=%USERPROFILE%\.local\bin;%PATH%"
  where uv >nul 2>nul
  if errorlevel 1 goto :failed
)

echo.
choice /C YN /M "Enable live JD shopping (you will sign in to JD yourself)"
if errorlevel 2 (set "LIVE=") else (set "LIVE=--live-jd")

uv run --locked python scripts\bootstrap.py --claude-desktop %LIVE%
if errorlevel 1 goto :failed
if not defined LIVE goto :done

uv run --locked playwright install chromium
if errorlevel 1 goto :failed

echo.
echo A browser window will open. Sign in to JD yourself, then come back here and press Enter.
uv run --locked personal-shopping-agent login jd
if errorlevel 1 goto :failed

echo.
choice /C YN /M "Download the Geekerwan chip ranking for chip scoring now"
if errorlevel 2 goto :done
uv run --locked personal-shopping-agent benchmark refresh
if errorlevel 1 echo Chip ranking refresh failed. Shopping still works without chip scoring.

:done
echo.
echo Setup finished. Fully quit Claude Desktop (tray icon, Quit) and open it again.
pause
exit /b 0

:cancelled
echo Setup cancelled. Nothing else was changed.
pause
exit /b 1

:failed
echo Setup stopped because the previous step failed. Copy the messages above when asking for help.
pause
exit /b 1
