@echo off
chcp 65001 >nul
echo ============================================
echo   Xiaolong Voice Assistant - Setup
echo ============================================
echo.

cd /d "%~dp0"

echo [1/3] Installing Python dependencies...
pip install -r requirements.txt
if errorlevel 1 (
    echo   Failed!
    pause
    exit /b 1
)
echo   Done
echo.

echo [2/3] Installing Node.js dependencies...
npm install
if errorlevel 1 (
    echo   Failed!
    pause
    exit /b 1
)
echo   Done
echo.

echo [3/3] Downloading models...
call download_models.bat

echo.
echo ============================================
echo   Setup complete! Run start.bat to launch.
echo ============================================
pause
