@echo off
rem moments2tg launcher for Windows: creates .venv on first run, installs deps, starts the panel.
rem   start.bat                 -> control panel in the browser
rem   start.bat --once ...      -> any moments2tg.py arguments
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "VENV=%~dp0.venv"
set "PY=%VENV%\Scripts\python.exe"

if not exist "%PY%" (
  echo [moments2tg] Первый запуск: создаю окружение Python в .venv ...
  py -3 -m venv "%VENV%" 2>nul || python -m venv "%VENV%"
)
if not exist "%PY%" (
  echo.
  echo Не найден Python 3.10+. Установите его с https://www.python.org/downloads/
  echo и при установке отметьте "Add python.exe to PATH". Потом запустите start.bat снова.
  pause
  exit /b 1
)
"%PY%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
  echo Нужен Python 3.10 или новее. Удалите папку .venv, обновите Python и запустите снова.
  pause
  exit /b 1
)

fc /b requirements.txt "%VENV%\requirements.installed" >nul 2>nul
if errorlevel 1 (
  echo [moments2tg] Устанавливаю зависимости ...
  "%PY%" -m pip install --disable-pip-version-check -q -r requirements.txt
  if errorlevel 1 (
    echo Не удалось установить зависимости, см. сообщения выше.
    pause
    exit /b 1
  )
  copy /y requirements.txt "%VENV%\requirements.installed" >nul
)

if /i "%~1"=="--setup" (
  "%PY%" install_alpha.py %2 %3
  echo.
  echo [moments2tg] Готово. Дальше запускайте start.bat без аргументов — откроется панель.
  pause
  exit /b 0
)

if "%~1"=="" (
  "%PY%" moments2tg.py --panel
) else (
  "%PY%" moments2tg.py %*
)
if errorlevel 1 pause
