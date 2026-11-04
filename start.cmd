@echo off
rem ===========================================================================
rem  MediaHub - запуск на Windows (двойной щелчок)
rem
rem  1. находит Python 3.10+ (предпочтительно 3.13);
rem  2. при первом запуске создаёт .venv и ставит зависимости (нужен интернет);
rem  3. запускает MediaHub со значком в трее и открывает браузер.
rem
rem  start.cmd --console   сервер в этом окне, без значка в трее
rem  start.cmd --check     только проверка окружения
rem  Журнал: _start.log рядом с этим файлом.
rem ===========================================================================
chcp 65001 >nul
setlocal EnableDelayedExpansion
set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
set "LOG=%ROOT%\_start.log"
set "VENV=%ROOT%\.venv"
set "VPY=%VENV%\Scripts\python.exe"
set "VPYW=%VENV%\Scripts\pythonw.exe"

echo [%date% %time%] запуск > "%LOG%"

rem --- 0. Перезапуск: останавливаем уже работающий MediaHub -------------------
rem Иначе новый запуск увидит занятый порт и просто откроет старый экземпляр.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { ($_.Name -in 'python.exe','pythonw.exe') -and $_.CommandLine -match 'mediahub' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >> "%LOG%" 2>&1
timeout /t 1 /nobreak >nul

rem --- 1. Python ------------------------------------------------------------
rem Каждый кандидат проверяем настоящим запуском: «python» из PATH на Windows
rem часто оказывается заглушкой Microsoft Store, которая ничего не запускает.
set "PY="
if exist "%VPY%" goto :have_venv

for %%V in (3.13 3.12 3.14 3.11 3.10) do (
  set "VER=%%V"
  set "NODOT=!VER:.=!"
  if not defined PY call :try "%LOCALAPPDATA%\Python\pythoncore-%%V-64\python.exe"
  if not defined PY call :try "%LOCALAPPDATA%\Programs\Python\Python!NODOT!\python.exe"
  if not defined PY call :try "%ProgramFiles%\Python!NODOT!\python.exe"
)
if not defined PY call :try "%LOCALAPPDATA%\Python\bin\python.exe"
if not defined PY (
  for /f "delims=" %%X in ('where python 2^>nul') do (
    if not defined PY (
      echo %%X | find /i "WindowsApps" >nul || call :try "%%X"
    )
  )
)
if not defined PY (
  for /f "delims=" %%X in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do (
    if not defined PY call :try "%%X"
  )
)
if not defined PY (
  echo [MediaHub] Не найден рабочий Python 3.10+. Установите его с python.org и запустите снова.
  echo Python не найден >> "%LOG%"
  goto :failed
)

rem --- 2. Окружение и зависимости (один раз) --------------------------------
echo [MediaHub] Первый запуск: создаю окружение и ставлю зависимости.
echo            Это займёт пару минут, нужен интернет.
echo Python: !PY! >> "%LOG%"
"!PY!" -m venv "%VENV%" >> "%LOG%" 2>&1
if not exist "%VPY%" (
  echo [MediaHub] Не удалось создать окружение .venv – подробности в _start.log
  goto :failed
)
"%VPY%" -m pip install --disable-pip-version-check --upgrade pip >> "%LOG%" 2>&1
"%VPY%" -m pip install --disable-pip-version-check -r "%ROOT%\requirements.txt" >> "%LOG%" 2>&1
if errorlevel 1 (
  rem Чаще всего не ставится необязательный pillow-heif – пробуем без него.
  echo [MediaHub] Повторная установка без поддержки HEIC...
  findstr /v /i "pillow-heif" "%ROOT%\requirements.txt" > "%VENV%\req-core.txt"
  "%VPY%" -m pip install --disable-pip-version-check -r "%VENV%\req-core.txt" >> "%LOG%" 2>&1
  if errorlevel 1 (
    echo [MediaHub] Не удалось установить зависимости – подробности в _start.log
    rmdir /s /q "%VENV%" >nul 2>&1
    goto :failed
  )
)

:have_venv
rem Старый каталог .piptarget собран под другой Python – в путь его не добавляем.
set "PYTHONPATH=%ROOT%"
set "PYTHONUTF8=1"
cd /d "%ROOT%"

"%VPY%" -c "import fastapi, uvicorn, pypdf, PIL, qrcode" >> "%LOG%" 2>&1
if errorlevel 1 (
  echo [MediaHub] Окружение повреждено – переустанавливаю зависимости...
  "%VPY%" -m pip install --disable-pip-version-check -r "%ROOT%\requirements.txt" >> "%LOG%" 2>&1 || goto :failed
)

if /i "%~1"=="--check" (
  "%VPY%" -m mediahub --check
  pause
  exit /b %errorlevel%
)

if /i "%~1"=="--console" (
  echo [MediaHub] Сервер работает в этом окне. Адрес: http://127.0.0.1:8765
  start "" "http://127.0.0.1:8765/"
  "%VPY%" -m mediahub
  if errorlevel 1 goto :failed
  exit /b 0
)

rem --- 3. Приложение со значком в трее (без консоли) ------------------------
"%VPY%" -c "import pystray" >nul 2>&1
if errorlevel 1 (
  "%VPY%" -m pip install --disable-pip-version-check pystray >> "%LOG%" 2>&1
)
rem Предварительная проверка в обычном python.exe: если код не импортируется,
rem ошибка попадёт в _start.log, а не пропадёт в фоновом процессе.
echo --- проверка приложения >> "%LOG%"
"%VPY%" -c "import mediahub.tray, mediahub.web as w; w.create_app(); print('ok')" >> "%LOG%" 2>&1
if errorlevel 1 (
  echo [MediaHub] Ошибка в приложении – подробности в _start.log
  goto :failed
)
start "MediaHub" "%VPYW%" -m mediahub.tray

rem Ждём, пока сервер ответит, – иначе показываем ошибку, а не пустое окно.
set /a TRIES=0
:wait_loop
timeout /t 1 /nobreak >nul
set /a TRIES+=1
"%VPY%" -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8765/api/health', timeout=1)" >nul 2>&1
if not errorlevel 1 (
  echo [MediaHub] Запущен: http://127.0.0.1:8765  ^(значок – в трее у часов^)
  echo запущен >> "%LOG%"
  timeout /t 3 >nul
  exit /b 0
)
if !TRIES! lss 25 goto :wait_loop
echo [MediaHub] Значок в трее не запустился ^(журнал: storage\logs\mediahub.log^).
echo [MediaHub] Запускаю сервер в этом окне – не закрывайте его.
echo трей не ответил, запуск в консоли >> "%LOG%"
start "" "http://127.0.0.1:8765/"
"%VPY%" -m mediahub
goto :failed

:failed
echo.
echo [MediaHub] Запуск не удался. Журнал: %LOG%
echo ошибка запуска >> "%LOG%"
pause
exit /b 1

rem --- Проверка кандидата: существует, запускается и это Python 3.10+ -------
:try
if not exist "%~1" exit /b 1
"%~1" -c "import sys, venv; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 (
  echo не подходит: %~1 >> "%LOG%"
  exit /b 1
)
set "PY=%~1"
exit /b 0
