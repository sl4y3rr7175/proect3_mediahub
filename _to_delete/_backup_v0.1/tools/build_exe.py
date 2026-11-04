"""Сборка портативного исполняемого файла MediaHub.

Запуск:  python tools/build_exe.py
Требуется PyInstaller (pip install pyinstaller). Результат: dist/MediaHub.exe
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    if not (ROOT / "mediahub").is_dir():
        print("Запускайте из корня проекта.")
        return 1
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name",
        "MediaHub",
        "--add-data",
        f"{ROOT / 'web'}{';' if sys.platform == 'win32' else ':'}web",
        "--collect-all",
        "pillow_heif",
        "--hidden-import",
        "uvicorn.logging",
        "--hidden-import",
        "uvicorn.loops.auto",
        "--hidden-import",
        "uvicorn.protocols.http.auto",
        str(ROOT / "mediahub" / "__main__.py"),
    ]
    print(" ".join(command))
    return subprocess.call(command, cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
