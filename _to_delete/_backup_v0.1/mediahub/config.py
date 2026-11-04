"""Конфигурация приложения.

Все значения переопределяются переменными окружения (или файлом .env рядом с
корнем проекта). Настройки читаются один раз при импорте модуля.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    return default if raw is None or raw.strip() == "" else raw.strip()


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "да"}


@dataclass(frozen=True)
class Settings:
    """Неизменяемый снимок настроек приложения."""

    # --- Сеть -------------------------------------------------------------
    host: str = field(default_factory=lambda: _env_str("MEDIAHUB_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("MEDIAHUB_PORT", 8765))
    #: Принудительно заданный адрес для QR-ссылок (иначе определяется автоматически).
    public_host: str | None = field(
        default_factory=lambda: (os.environ.get("MEDIAHUB_PUBLIC_HOST") or "").strip() or None
    )

    # --- Хранилище и жизненный цикл сессий --------------------------------
    storage_dir: Path = field(
        default_factory=lambda: Path(
            _env_str("MEDIAHUB_STORAGE_DIR", str(PROJECT_ROOT / "storage" / "tasks"))
        )
    )
    #: TTL сессии задачи в секундах (ТЗ 2.2: ровно 30 минут).
    task_ttl_seconds: int = field(default_factory=lambda: _env_int("MEDIAHUB_TASK_TTL", 1800))
    #: Период опроса сборщика мусора в секундах (ТЗ 2.2: каждые 5 минут).
    gc_interval_seconds: int = field(default_factory=lambda: _env_int("MEDIAHUB_GC_INTERVAL", 300))
    #: Задержка самоуничтожения после полной выдачи файла (ТЗ 2.2: 60 секунд).
    burn_after_download_seconds: int = field(
        default_factory=lambda: _env_int("MEDIAHUB_BURN_DELAY", 60)
    )
    #: Включён ли триггер однократного использования.
    burn_after_download: bool = field(
        default_factory=lambda: _env_bool("MEDIAHUB_BURN_AFTER_DOWNLOAD", False)
    )

    # --- Лимиты входных данных -------------------------------------------
    max_files_per_request: int = field(
        default_factory=lambda: _env_int("MEDIAHUB_MAX_FILES", 50)
    )
    max_request_bytes: int = field(
        default_factory=lambda: _env_int("MEDIAHUB_MAX_REQUEST_BYTES", 200 * 1024 * 1024)
    )
    max_pdf_merge_files: int = field(default_factory=lambda: _env_int("MEDIAHUB_MAX_PDF_MERGE", 20))
    #: Жёсткий лимит времени на одну команду FFmpeg (ТЗ 6: 120 секунд).
    ffmpeg_timeout_seconds: int = field(
        default_factory=lambda: _env_int("MEDIAHUB_FFMPEG_TIMEOUT", 120)
    )

    # --- Качество по умолчанию -------------------------------------------
    default_image_quality: int = field(
        default_factory=lambda: _env_int("MEDIAHUB_IMAGE_QUALITY", 85)
    )
    default_pdf_dpi: int = field(default_factory=lambda: _env_int("MEDIAHUB_PDF_DPI", 150))
    default_gif_fps: int = field(default_factory=lambda: _env_int("MEDIAHUB_GIF_FPS", 12))
    default_gif_width: int = field(default_factory=lambda: _env_int("MEDIAHUB_GIF_WIDTH", 480))
    default_gif_seconds: int = field(default_factory=lambda: _env_int("MEDIAHUB_GIF_SECONDS", 15))
    default_mp3_bitrate: int = field(default_factory=lambda: _env_int("MEDIAHUB_MP3_BITRATE", 192))
    default_video_crf: int = field(default_factory=lambda: _env_int("MEDIAHUB_VIDEO_CRF", 27))

    # --- Прочее -----------------------------------------------------------
    workers: int = field(default_factory=lambda: max(1, _env_int("MEDIAHUB_WORKERS", 2)))
    log_level: str = field(default_factory=lambda: _env_str("MEDIAHUB_LOG_LEVEL", "INFO"))
    #: Разрешать обращения не из локальных сетей (по умолчанию запрещено).
    allow_public_clients: bool = field(
        default_factory=lambda: _env_bool("MEDIAHUB_ALLOW_PUBLIC", False)
    )
    #: Каталог, куда `tools/install_ffmpeg.py` складывает бинарник.
    ffmpeg_vendor_dir: Path = field(
        default_factory=lambda: Path(
            _env_str("MEDIAHUB_FFMPEG_DIR", str(PROJECT_ROOT / "vendor" / "ffmpeg"))
        )
    )

    def ensure_dirs(self) -> None:
        """Создаёт рабочие каталоги, если их ещё нет."""
        self.storage_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
