@echo off
chcp 65001 >nul
title gemini-claude-proxy

:: Activate venv if exists
if exist "venv\Scripts\activate.bat" call venv\Scripts\activate.bat

:: Start panel (opens browser)
echo Starting panel on http://127.0.0.1:8083 ...
start /B pythonw panel.py --port 8083
timeout /t 3 /nobreak >nul
start http://localhost:8083
exit
