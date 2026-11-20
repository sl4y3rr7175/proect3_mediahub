@echo off
rem MediaHub - bystryy zapusk (dvoynoy schelchok).
rem Zapuskaet prilozhenie so znachkom v trea i otkryvaet brauzer. Esli uzhe rabotaet - prosto otkryvaet stranitsu.
rem Pervyy raz (net .venv) ili pri oshibkah - polnyy start.cmd.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  call "%~dp0start.cmd"
  exit /b
)
set "PYTHONUTF8=1"
set "PYTHONPATH=%~dp0"
start "" ".venv\Scripts\pythonw.exe" -m mediahub.tray
exit /b 0
