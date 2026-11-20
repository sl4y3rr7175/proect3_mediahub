@echo off
setlocal
cd /d "%~dp0"
if not exist logs mkdir logs
if not exist ".venv\Scripts\python.exe" (
  echo Seychas ne nayden .venv. Snachala zapustite start.cmd odin raz.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install -q pytest httpx > logs\pip.txt 2>&1
".venv\Scripts\python.exe" -m pip install -q httpx2 >> logs\pip.txt 2>&1
".venv\Scripts\python.exe" -m pip install -q cryptography >> logs\pip.txt 2>&1
echo Zapusk testov, podozhdite...
".venv\Scripts\python.exe" -m pytest -q > logs\tests.txt 2>&1
set RC=%ERRORLEVEL%
echo.>> logs\tests.txt
echo EXIT CODE: %RC%>> logs\tests.txt
type logs\tests.txt
echo.
if "%RC%"=="0" (echo === VSE TESTY PROSHLI ===) else (echo === ESTY OSHIBKI, sm. logs\tests.txt ===)
pause
exit /b %RC%
