@echo off
rem ===========================================================================
rem  MediaHub – чистка проекта и подключение Git (запустить один раз)
rem
rem  1. Переносит устаревшее в папку _to_delete (ничего не удаляет насовсем):
rem     .piptarget (библиотеки под удалённый Python 3.12), старые журналы,
rem     .coverage, скриншоты _shots*, mockups, папки _backup_*.
rem  2. Находит Git, при необходимости ставит его через winget.
rem  3. Создаёт репозиторий и сохраняет текущую версию первым коммитом.
rem
rem  Повторный запуск безопасен: перенесённое не трогается, коммит – только
rem  если есть изменения.
rem ===========================================================================
chcp 65001 >nul
setlocal EnableDelayedExpansion
set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
cd /d "%ROOT%"

echo.
echo [1/3] Убираю устаревшее в _to_delete ...
if not exist "_to_delete" mkdir "_to_delete"
for %%D in (.piptarget _shots _shots-dark mockups _backup_ui _backup_v0.1 .pytest-tmp .tmp) do (
  if exist "%%D\" (
    move "%%D" "_to_delete\" >nul 2>&1 && echo        папка %%D || echo        не удалось перенести %%D ^(занята?^)
  )
)
for %%F in (_srv.err _srv.out .coverage) do (
  if exist "%%F" (
    move "%%F" "_to_delete\" >nul 2>&1 && echo        файл %%F
  )
)

echo.
echo [2/3] Ищу Git ...
set "GIT="
where git >nul 2>&1 && set "GIT=git"
if not defined GIT if exist "%ProgramFiles%\Git\cmd\git.exe" set "GIT=%ProgramFiles%\Git\cmd\git.exe"
if not defined GIT if exist "%LOCALAPPDATA%\Programs\Git\cmd\git.exe" set "GIT=%LOCALAPPDATA%\Programs\Git\cmd\git.exe"
if not defined GIT (
  echo        Git не установлен – ставлю через winget ^(нужен интернет^) ...
  winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements --silent
  if exist "%ProgramFiles%\Git\cmd\git.exe" set "GIT=%ProgramFiles%\Git\cmd\git.exe"
  if exist "%LOCALAPPDATA%\Programs\Git\cmd\git.exe" set "GIT=%LOCALAPPDATA%\Programs\Git\cmd\git.exe"
)
if not defined GIT (
  echo.
  echo [MediaHub] Git установить не удалось. Скачайте его с https://git-scm.com и запустите этот файл снова.
  goto :end
)
echo        Git: !GIT!

echo.
echo [3/3] Сохраняю версию в Git ...
if not exist ".git" (
  "!GIT!" init -b main >nul || goto :git_failed
  echo        репозиторий создан
)
rem Имя автора нужно для коммита; если оно не задано глобально – задаём только для проекта.
"!GIT!" config user.name >nul 2>&1 || "!GIT!" config user.name "%USERNAME%"
"!GIT!" config user.email >nul 2>&1 || "!GIT!" config user.email "%USERNAME%@localhost"
"!GIT!" config core.quotepath false
"!GIT!" add -A || goto :git_failed
"!GIT!" diff --cached --quiet
if errorlevel 1 (
  "!GIT!" commit -q -m "MediaHub 0.2.0: дизайн в стиле Apple, новые функции, стабильность" || goto :git_failed
  echo        коммит создан:
  "!GIT!" log --oneline -1
) else (
  echo        изменений нет – коммит не нужен
)
echo.
"!GIT!" count-objects -vH | findstr /b "size-pack"
echo.
echo Готово.
echo  • Историю изменений видно командой:  git log --oneline
echo  • Откатить файл к сохранённой версии:  git checkout -- путь\к\файлу
echo  • Папку _to_delete можно удалить, когда убедитесь, что всё работает.
goto :end

:git_failed
echo.
echo [MediaHub] Git вернул ошибку – текст выше.

:end
echo.
pause
