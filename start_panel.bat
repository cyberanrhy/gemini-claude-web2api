@echo off
set SCRIPT_DIR=%~dp0
set LOG_FILE=%TEMP%\panel.log

cd /d "%SCRIPT_DIR%"

echo Starting proxies...

:: Start Gemini proxy if not running
powershell -NoProfile -Command "if (-not (netstat -ano | Select-String ':8081' | Select-String 'LISTENING')) { Start-Process -WindowStyle Hidden -FilePath pythonw -ArgumentList 'gemini_web2api.py','--config','..\config.json','--cookie-file','..\cookie.txt','--proxy','http://127.0.0.1:12334' -WorkingDirectory (Join-Path '%SCRIPT_DIR%' 'gemini') }"

:: Start Claude proxy if not running
powershell -NoProfile -Command "if (-not (netstat -ano | Select-String ':8082' | Select-String 'LISTENING')) { Start-Process -WindowStyle Hidden -FilePath pythonw -ArgumentList 'claude_web2api.py' -WorkingDirectory (Join-Path '%SCRIPT_DIR%' 'claude') }"

echo Starting panel on http://127.0.0.1:8083 ...
echo   Logs: %%TEMP%%\panel.log

start "" /B powershell -NoProfile -Command "Start-Process -WindowStyle Hidden -FilePath pythonw -ArgumentList 'panel.py','--port','8083' -RedirectStandardError '%LOG_FILE%'"
timeout /t 3 /nobreak >nul
start http://localhost:8083
