@echo off
REM DAV Consolidator v4 — build script
REM Prerequisites: pip install pyinstaller PyQt6
REM                ffmpeg.exe + ffprobe.exe in this folder

setlocal
set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"

if not exist "%ROOT%\ffmpeg.exe"  ( echo [ERROR] ffmpeg.exe missing  & exit /b 1 )
if not exist "%ROOT%\ffprobe.exe" ( echo [ERROR] ffprobe.exe missing & exit /b 1 )

echo [BUILD] Running PyInstaller...
pyinstaller "%ROOT%\DAVConsolidator.spec" --clean

if %ERRORLEVEL% NEQ 0 ( echo [ERROR] Build failed & exit /b %ERRORLEVEL% )

echo.
echo [OK] dist\DAVConsolidator.exe is ready.
echo      ffmpeg + ffprobe are bundled inside — no external deps needed.
endlocal
