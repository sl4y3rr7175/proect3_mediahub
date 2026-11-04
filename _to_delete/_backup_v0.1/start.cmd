@echo off
rem ===========================================================================
rem  MediaHub - запуск сервера (Windows)
rem  Использование: start.cmd [--port 8765] [--check]
rem ===========================================================================
setlocal EnableDelayedExpansion
set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"

rem --- 1. Поиск интерпретатора Python ---------------------------------------
set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY (
  where py >nul 2>&1 && set "PY=py"
)
if not defined PY (
  if exist "%LOCALAPPDATA%\Python\bin\python.exe" set "PY=%LOCALAPPDATA%\Python\bin\python.exe"
)
if not defined PY (
  echo [MediaHub] Python не найден. Установите Python 3.10+ и повторите запуск.
  exit /b 1
)

rem --- 2. Пути к зависимостям проекта ---------------------------------------
set "PYTHONPATH=%ROOT%\.piptarget;%ROOT%"
if exist "%ROOT%\.venv\Scripts\python.exe" set "PY=%ROOT%\.venv\Scripts\python.exe"

rem --- 3. Проверка установленных зависимостей -------------------------------
"%PY%" -c "import fastapi, uvicorn, pypdf, PIL" >nul 2>&1
if errorlevel 1 (
  echo [MediaHub] Зависимости не установлены. Выполняется установка...
  "%PY%" "%ROOT%\tools\bootstrap.py" || goto :failed
)

rem --- 4. Запуск сервера ----------------------------------------------------
"%PY%" -m mediahub %*
exit /b %errorlevel%

:failed
echo.
echo [MediaHub] Не удалось установить зависимости.
echo Установите их вручную:  pip install -r requirements.txt
exit /b 1
