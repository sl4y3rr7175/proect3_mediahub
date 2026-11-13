@echo off
chcp 65001 >nul
cd /d "%~dp0"
where git >nul 2>nul || (echo Git не найден. Сначала запустите setup-git.cmd & pause & exit /b 1)
if not exist ".git" (echo Нет репозитория. Сначала запустите setup-git.cmd & pause & exit /b 1)
set REPO=https://github.com/sl4y3rr7175/proect3_mediahub.git
git remote get-url origin >nul 2>nul && (git remote set-url origin %REPO%) || (git remote add origin %REPO%)
git add -A
git commit -m "MediaHub 0.4.0" >nul 2>nul
git branch -M main
rem Забываем сохранённый вход (там другой аккаунт), чтобы Git спросил заново
(echo protocol=https& echo host=github.com& echo.) | git credential reject >nul 2>nul
cmdkey /delete:git:https://github.com >nul 2>nul
echo ВАЖНО: в окне входа выберите аккаунт sl4y3rr7175 (не sl4y3rr71).
echo Отправляю на GitHub. Если откроется окно входа - войдите в аккаунт.
git push -u origin main
if errorlevel 1 (echo. & echo Не получилось. Пришлите скриншот этого окна.) else (echo. & echo Готово: https://github.com/sl4y3rr7175/proect3_mediahub)
pause
