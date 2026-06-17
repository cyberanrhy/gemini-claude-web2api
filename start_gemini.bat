@echo off
chcp 65001 >nul
title Gemini Proxy
if exist "venv\Scripts\activate.bat" call venv\Scripts\activate.bat
pythonw gemini\gemini_web2api.py --config ../config.json --cookie-file ../cookie.txt --proxy http://127.0.0.1:12334
pause
