@echo off
REM ============================================================
REM  DAV Consolidator v3 — PyInstaller build script (Windows)
REM ============================================================
REM
REM  Prerequisites:
REM    pip install pyinstaller PyQt6
REM    ffmpeg.exe and ffprobe.exe must exist in the project root.
REM
REM  Output:
REM    dist\DAVConsolidator.exe  (single self-contained executable)
REM
REM  Usage:
REM    build.bat
REM ============================================================

setlocal

set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"

if not exist "%ROOT%\ffmpeg.exe" (
    echo [ERROR] ffmpeg.exe not found in: %ROOT%
    echo         Place ffmpeg.exe alongside main.py before building.
    exit /b 1
)
if not exist "%ROOT%\ffprobe.exe" (
    echo [ERROR] ffprobe.exe not found in: %ROOT%
    echo         Place ffprobe.exe alongside main.py before building.
    exit /b 1
)

echo [INFO] Building DAVConsolidator.exe ...
echo [INFO] Project root: %ROOT%

pyinstaller ^
    --onefile ^
    --windowed ^
    --name DAVConsolidator ^
    --add-binary "%ROOT%\ffmpeg.exe;." ^
    --add-binary "%ROOT%\ffprobe.exe;." ^
    --hidden-import PyQt6.sip ^
    --hidden-import PyQt6.QtCore ^
    --hidden-import PyQt6.QtGui ^
    --hidden-import PyQt6.QtWidgets ^
    --collect-all PyQt6 ^
    --clean ^
    "%ROOT%\main.py"

if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Build failed with exit code %ERRORLEVEL%
    exit /b %ERRORLEVEL%
)

echo.
echo [SUCCESS] Build complete!
echo           Executable: %ROOT%\dist\DAVConsolidator.exe
echo           ffmpeg.exe and ffprobe.exe are bundled inside — no
echo           external dependencies required on the target machine.

endlocal
