@echo off
chcp 65001 >nul
title xiaolong
cd /d "%~dp0"

:loop
echo [%date% %time%] starting...
set PYTHONIOENCODING=utf-8
python -X utf8 src/main.py
echo.
echo [%date% %time%] restarting in 3s...
timeout /t 3 /nobreak >nul
goto loop
