@echo off
echo Stopping all proxy processes...
taskkill /F /IM pythonw.exe 2>nul
taskkill /F /IM python.exe 2>nul
echo Done.
pause
