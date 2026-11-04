#!/usr/bin/env sh
# ============================================================================
#  MediaHub — запуск сервера (Linux / macOS)
#  Использование: ./start.sh [--port 8765] [--check]
# ============================================================================
set -eu

ROOT=$(cd "$(dirname "$0")" && pwd)
cd "$ROOT"

# --- 1. Интерпретатор Python -------------------------------------------------
PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then PY="$candidate"; break; fi
done
if [ -z "$PY" ]; then
  echo "[MediaHub] Python не найден. Установите Python 3.10+." >&2
  exit 1
fi

# --- 2. Виртуальное окружение и зависимости ---------------------------------
if [ ! -x "$ROOT/.venv/bin/python" ]; then
  echo "[MediaHub] Первый запуск: создаю .venv и ставлю зависимости..."
  "$PY" "$ROOT/tools/bootstrap.py"
fi
PY="$ROOT/.venv/bin/python"
export PYTHONPATH="$ROOT"

if ! "$PY" -c "import fastapi, uvicorn, pypdf, PIL" >/dev/null 2>&1; then
  echo "[MediaHub] Окружение повреждено – переустанавливаю зависимости..."
  "$PY" -m pip install -r "$ROOT/requirements.txt"
fi

# --- 3. FFmpeg (необязательно) ----------------------------------------------
if ! command -v ffmpeg >/dev/null 2>&1 && [ ! -d "$ROOT/vendor/ffmpeg" ]; then
  echo "[MediaHub] Внимание: FFmpeg не найден — функции видео и аудио вернут 503."
  echo "           Установка: python tools/install_ffmpeg.py"
fi

exec "$PY" -m mediahub "$@"
