@echo off
chcp 65001 >nul
title Install gemini-claude-web2api
echo ============================================
echo   gemini-claude-web2api — установка
echo ============================================
echo.

:: Check Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python не найден. Установи Python 3.10+ с python.org
    pause
    exit /b 1
)

:: Create venv
if not exist "venv" (
    echo [1/3] Создание виртуального окружения...
    python -m venv venv
) else (
    echo [1/3] Виртуальное окружение уже существует
)

:: Activate and install
echo [2/3] Установка зависимостей...
call venv\Scripts\activate.bat
pip install -r requirements.txt
pip install -r gemini\requirements.txt 2>nul
pip install -r claude\requirements.txt 2>nul

:: Gemini config
if not exist "config.json" (
    echo [3/3] Создание config.json из примера...
    copy gemini\config.json.example config.json >nul
) else (
    echo [3/3] config.json уже существует
)

echo.
echo ============================================
echo   УСТАНОВКА ЗАВЕРШЕНА
echo ============================================
echo.
echo Дальше:
echo   1. Экспортируй куки из Firefox (cookies.txt extension):
echo      - Зайди на gemini.google.com и нажми экспорт =^> cookie.txt
echo      - Зайди на claude.ai и нажми экспорт =^> cookie_claude.txt
echo      Помести оба файла в папку проекта
echo.
echo   2. Запусти панель:  start.bat
echo      или напрямую:    start_gemini.bat / start_claude.bat
echo.
echo ПРОБЛЕМЫ:
echo   - curl_cffi на Linux:  pip install --break-system-packages curl_cffi
echo   - macOS:  не тестировалось, могут быть нюансы с curl_cffi
echo.
pause
