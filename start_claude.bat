@echo off
chcp 65001 >nul
title Claude Proxy
if exist "venv\Scripts\activate.bat" call venv\Scripts\activate.bat
pythonw claude\claude_web2api.py
pause
