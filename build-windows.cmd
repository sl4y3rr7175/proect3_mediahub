@echo off
rem ===========================================================================
rem  MediaHub – сборка приложения и установщика для Windows (двойной щелчок)
rem
rem  1. берёт окружение .venv (его создаёт start.cmd при первом запуске);
rem  2. ставит PyInstaller и pystray;
rem  3. проверяет FFmpeg в vendor\ffmpeg (при необходимости скачивает);
rem  4. собирает dist\MediaHub\MediaHub.exe;
rem  5. находит или ставит Inno Setup и собирает установщик
rem     installer\Output\MediaHub-Setup-<версия>.exe.
rem  Подробный журнал: _build.log
rem ===========================================================================
chcp 65001 >nul
setlocal EnableDelayedExpansion
set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
cd /d "%ROOT%"
set "LOG=%ROOT%\_build.log"
set "PY=%ROOT%\.venv\Scripts\python.exe"
echo [%date% %time%] сборка > "%LOG%"

if not exist "%PY%" (
  echo [MediaHub] Сначала запустите start.cmd один раз – он создаст окружение .venv.
  goto :failed
)
set "PYTHONPATH=%ROOT%"

echo [1/4] Ставлю инструменты сборки ...
"%PY%" -m pip install --disable-pip-version-check -q pyinstaller pystray >> "%LOG%" 2>&1 || (
  echo        не удалось поставить PyInstaller – подробности в _build.log
  goto :failed
)

echo [2/4] Проверяю FFmpeg ...
if not exist "%ROOT%\vendor\ffmpeg\ffmpeg.exe" (
  dir /s /b "%ROOT%\vendor\ffmpeg\ffmpeg.exe" >nul 2>&1 || (
    echo        FFmpeg нет – скачиваю ^(около 80 МБ^) ...
    "%PY%" tools\install_ffmpeg.py >> "%LOG%" 2>&1 || echo        не скачался – видео в сборке работать не будет
  )
)

echo [3/4] Собираю приложение ^(1–3 минуты^) ...
"%PY%" tools\build_exe.py >> "%LOG%" 2>&1
if errorlevel 1 (
  echo        сборка не удалась – подробности в _build.log
  goto :failed
)
if not exist "%ROOT%\dist\MediaHub\MediaHub.exe" (
  echo        MediaHub.exe не появился – подробности в _build.log
  goto :failed
)
echo        dist\MediaHub\MediaHub.exe готов

echo [4/4] Собираю установщик ...
set "ISCC="
for %%P in (
  "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
  "%ProgramFiles%\Inno Setup 6\ISCC.exe"
  "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
) do if not defined ISCC if exist %%P set "ISCC=%%~P"
if not defined ISCC (
  echo        Inno Setup нет – ставлю через winget ...
  winget install --id JRSoftware.InnoSetup -e --source winget --accept-package-agreements --accept-source-agreements --silent >> "%LOG%" 2>&1
  for %%P in (
    "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
    "%ProgramFiles%\Inno Setup 6\ISCC.exe"
    "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
  ) do if not defined ISCC if exist %%P set "ISCC=%%~P"
)
if not defined ISCC (
  echo        Inno Setup не найден. Приложение уже собрано: dist\MediaHub\MediaHub.exe
  echo        Для установщика поставьте Inno Setup 6 с jrsoftware.org и запустите этот файл снова.
  goto :done
)
"%ISCC%" /Q "%ROOT%\installer\MediaHub.iss" >> "%LOG%" 2>&1
if errorlevel 1 (
  echo        установщик не собрался – подробности в _build.log
  goto :failed
)
for %%F in ("%ROOT%\installer\Output\MediaHub-Setup-*.exe") do set "SETUP=%%~fF"
echo        готово: !SETUP!
explorer /select,"!SETUP!"

:done
echo.
echo Сборка завершена.
pause
exit /b 0

:failed
echo.
echo [MediaHub] Сборка не удалась. Журнал: %LOG%
pause
exit /b 1
