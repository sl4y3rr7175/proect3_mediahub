"""Этап 2c – модуль видео- и аудио-транскодирования на FFmpeg.

ТЗ 3.3:
  * pre-flight проверка наличия FFmpeg в PATH; при отсутствии – 503 с инструкцией;
  * Режим A «Извлечение аудиодорожки» – -vn, MP3, 44.1 кГц, битрейт пользователя;
  * Режим B «GIF» – ограничение длительности, ширина 480px, 10–15 fps,
    двухпроходная палитра (palettegen + paletteuse с дизерингом);
  * Режим C «Сжатие видео» – H.264 profile main, CRF 26–28, AAC 128 kbps;
  * жёсткий таймаут на процесс; по истечении – SIGKILL и ошибка 504.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from ..config import Settings, settings as default_settings
from ..errors import (
    DependencyUnavailableError,
    ProcessingTimeoutError,
    UnprocessableFileError,
    ValidationError,
)

logger = logging.getLogger("mediahub.services.media")

#: Сколько секунд ждать ответа на -version при проверке окружения.
PROBE_TIMEOUT_SECONDS = 8

INSTALL_HINT = (
    "FFmpeg не найден в PATH. Установите его одним из способов:\n"
    "  • Windows: winget install --id Gyan.FFmpeg -e\n"
    "  • macOS:   brew install ffmpeg\n"
    "  • Debian:  sudo apt-get install -y ffmpeg\n"
    "  • без прав администратора: python tools/install_ffmpeg.py "
    "(скачает сборку в каталог vendor/ffmpeg)\n"
    "После установки перезапустите сервер MediaHub."
)

#: Расширения, которые модуль считает медиа.
VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".wmv", ".flv", ".mpeg", ".mpg", ".ts", ".3gp"}
AUDIO_SUFFIXES = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma"}
MEDIA_SUFFIXES = VIDEO_SUFFIXES | AUDIO_SUFFIXES

AUDIO_ACTIONS = {"mp3", "audio", "extract_audio"}
GIF_ACTIONS = {"gif", "to_gif"}
COMPRESS_ACTIONS = {"compress", "video", "compress_video"}

MP3_BITRATES = {128, 192, 256, 320}
_ERROR_PATTERNS = (
    (re.compile(r"Invalid data found when processing input", re.I),
     "Файл повреждён или не является медиаконтейнером."),
    (re.compile(r"moov atom not found", re.I),
     "Видеофайл не докачался: отсутствует индекс (moov atom)."),
    (re.compile(r"does not contain any stream", re.I),
     "В файле не найдено ни одного потока."),
    (re.compile(r"Output file does not contain any stream", re.I),
     "В файле нет потока запрошенного типа (например, отсутствует звук)."),
    (re.compile(r"matches no streams", re.I),
     "В файле нет потока запрошенного типа (например, отсутствует звук)."),
    (re.compile(r"Unknown decoder", re.I),
     "Кодек файла не поддерживается установленной сборкой FFmpeg."),
    (re.compile(r"No such file or directory", re.I),
     "Файл не найден внутри сессии."),
    (re.compile(r"Permission denied", re.I),
     "Нет прав на чтение или запись файла."),
)


@dataclass
class MediaOutcome:
    files: list[Path] = field(default_factory=list)
    input_bytes: int = 0
    output_bytes: int = 0
    duration_seconds: float | None = None
    warnings: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)

    @property
    def savings_percent(self) -> float:
        if self.input_bytes <= 0:
            return 0.0
        return round((1 - self.output_bytes / self.input_bytes) * 100, 1)


# ---------------------------------------------------------------------------
# Pre-flight проверка окружения
# ---------------------------------------------------------------------------
def _candidate_paths(vendor_dir: Path) -> list[Path]:
    candidates: list[Path] = []
    if vendor_dir.is_dir():
        candidates.extend(sorted(vendor_dir.rglob("ffmpeg.exe")))
        candidates.extend(sorted(vendor_dir.rglob("ffmpeg")))
    env_dir = os.environ.get("MEDIAHUB_FFMPEG_BIN", "").strip()
    if env_dir:
        candidates.append(Path(env_dir))
    return candidates


@functools.lru_cache(maxsize=4)
def _resolve_ffmpeg_cached(vendor_dir: str) -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    for candidate in _candidate_paths(Path(vendor_dir)):
        if candidate.is_file():
            return str(candidate)
    return None


def reset_probe_cache() -> None:
    """Сбрасывает кэш поиска бинарника и результатов его проверки."""
    _resolve_ffmpeg_cached.cache_clear()
    _checked_version.cache_clear()


def find_ffmpeg(config: Settings | None = None) -> str | None:
    """Ищет ffmpeg в PATH, затем в каталоге vendor/ffmpeg (без проверки запуска)."""
    cfg = config or default_settings
    return _resolve_ffmpeg_cached(str(cfg.ffmpeg_vendor_dir))


def ffmpeg_version(config: Settings | None = None) -> str | None:
    """Возвращает первую строку `ffmpeg -version` или None, если запуск не удался."""
    path = find_ffmpeg(config)
    if not path:
        return None
    try:
        return _checked_version(path)
    except DependencyUnavailableError:
        return None


def availability(config: Settings | None = None) -> dict:
    """Отчёт о готовности медиа-модуля: найден ли бинарник и запускается ли он."""
    path = find_ffmpeg(config)
    version = ffmpeg_version(config) if path else None
    return {
        "available": bool(path and version),
        "path": path,
        "version": version,
        "install_hint": None if path and version else INSTALL_HINT,
    }


# ---------------------------------------------------------------------------
# Запуск процесса с жёстким таймаутом
# ---------------------------------------------------------------------------
def _terminate_tree(process: subprocess.Popen) -> None:
    """Принудительно убивает процесс и всё его дерево (SIGKILL-семантика).

    Способы применяются каскадом: в ограниченных средах (песочница, отсутствие
    прав) `taskkill` может вернуть «Access denied», поэтому всегда выполняется
    жёсткое завершение через API процесса и закрытие канала прогресса.
    """
    killed = False
    if process.poll() is None and os.name == "nt":
        try:
            completed = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                check=False,
                timeout=15,
            )
            if completed.returncode == 0:
                killed = True
            else:
                logger.debug(
                    "taskkill не смог завершить процесс %s: %s",
                    process.pid,
                    (completed.stderr or b"").decode("utf-8", "replace").strip(),
                )
        except Exception as exc:  # noqa: BLE001 – переходим к жёсткому завершению
            logger.debug("taskkill недоступен: %s", exc)
    elif process.poll() is None:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            killed = True
        except Exception as exc:  # noqa: BLE001
            logger.debug("killpg недоступен: %s", exc)

    if process.poll() is None:
        with contextlib.suppress(Exception):
            process.kill()
            killed = True

    # Канал прогресса закрываем до ожидания: на Windows Popen.wait() сначала
    # вычитывает stdout, а читающий поток держит дескриптор открытым.
    if process.stdout is not None:
        with contextlib.suppress(Exception):
            process.stdout.close()
    with contextlib.suppress(Exception):
        process.wait(timeout=5)
    if not killed and process.poll() is None:
        logger.error("Не удалось завершить процесс FFmpeg %s", process.pid)


def _popen_kwargs() -> dict:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


class _UnusableBinaryError(OSError):
    """Найденный файл ffmpeg существует, но не запускается (битая сборка)."""


def _probe_binary(binary: str) -> str:
    """Запускает `ffmpeg -version`, превращая любую ошибку запуска в 503.

    Файл с именем ffmpeg может оказаться повреждённым, несовместимым с системой
    или без прав на выполнение. Пользователь в таком случае должен получить
    понятную ошибку с инструкцией по установке, а не внутреннюю ошибку 500.
    """
    try:
        completed = subprocess.run(
            [binary, "-version"],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Файл %s найден, но не запускается: %s", binary, exc)
        raise DependencyUnavailableError(
            f"Найденный FFmpeg ({binary}) не запускается: {exc}. "
            "Переустановите его: python tools/install_ffmpeg.py --force\n\n" + INSTALL_HINT,
            details={"dependency": "ffmpeg", "path": binary, "reason": str(exc),
                     "install_hint": INSTALL_HINT},
        ) from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[:200]
        logger.warning("ffmpeg -version завершился кодом %s: %s", completed.returncode, detail)
        raise DependencyUnavailableError(
            f"Найденный FFmpeg ({binary}) неработоспособен (код {completed.returncode}). "
            "Переустановите его: python tools/install_ffmpeg.py --force\n\n" + INSTALL_HINT,
            details={"dependency": "ffmpeg", "path": binary, "reason": detail,
                     "install_hint": INSTALL_HINT},
        )

    first_line = (completed.stdout or "").strip().splitlines()
    return first_line[0] if first_line else "ffmpeg (версия неизвестна)"


def _ffmpeg_version_cached(binary: str) -> str:
    """Кэширует результат проверки работоспособности конкретного файла."""
    return _probe_binary(binary)


@functools.lru_cache(maxsize=4)
def _checked_version(binary: str) -> str:
    return _ffmpeg_version_cached(binary)


def require_ffmpeg(config: Settings | None = None) -> str:
    """Возвращает путь к рабочему ffmpeg или поднимает 503 (ТЗ 3.3).

    Проверяется не только наличие файла, но и возможность его запуска: битая
    или несовместимая сборка приводит к 503 с инструкцией, а не к ошибке 500.
    """
    path = find_ffmpeg(config)
    if not path:
        raise DependencyUnavailableError(
            INSTALL_HINT, details={"dependency": "ffmpeg", "install_hint": INSTALL_HINT}
        )
    _checked_version(path)
    return path


def _classify_error(stderr: str, *, fallback: str) -> str:
    for pattern, message in _ERROR_PATTERNS:
        if pattern.search(stderr):
            return message
    tail = [line for line in stderr.strip().splitlines() if line.strip()][-3:]
    return f"{fallback} ({' | '.join(tail)})" if tail else fallback


def _parse_progress(line: str) -> tuple[str, str] | None:
    if "=" not in line:
        return None
    key, _, value = line.partition("=")
    return key.strip(), value.strip()


def _spawn_ffmpeg(
    args: Sequence[str],
    *,
    timeout: int | None = None,
    on_progress: Callable[[float], None] | None = None,
    duration: float | None = None,
    config: Settings | None = None,
    error_hint: str = "FFmpeg не смог обработать файл.",
) -> str:
    """Запускает процесс; неработоспособный бинарник превращает в ошибку 503."""
    try:
        return run_ffmpeg(
            args,
            timeout=timeout,
            on_progress=on_progress,
            duration=duration,
            config=config,
            error_hint=error_hint,
        )
    except DependencyUnavailableError:
        raise
    except OSError as exc:
        # Файл есть, но запустить его нельзя (битая сборка, нет прав, не та разрядность).
        logger.warning("Не удалось запустить FFmpeg: %s", exc)
        raise DependencyUnavailableError(
            f"FFmpeg не запускается: {exc}. Переустановите его: "
            "python tools/install_ffmpeg.py --force\n\n" + INSTALL_HINT,
            details={"dependency": "ffmpeg", "reason": str(exc), "install_hint": INSTALL_HINT},
        ) from exc


def _durations(path: Path, config: Settings | None) -> float | None:
    """Определяет длительность, не позволяя битому бинарнику уронить задачу."""
    try:
        return probe_duration(path, config=config)
    except OSError as exc:
        logger.debug("Длительность не определена: %s", exc)
        return None


def run_ffmpeg(
    args: Sequence[str],
    *,
    timeout: int | None = None,
    on_progress: Callable[[float], None] | None = None,
    duration: float | None = None,
    config: Settings | None = None,
    error_hint: str = "FFmpeg не смог обработать файл.",
) -> str:
    """Запускает ffmpeg с жёстким лимитом времени и стримингом прогресса.

    Возвращает накопленный stderr. Прогресс читается отдельным потоком, чтобы
    блокирующее чтение канала не мешало сработать таймауту. По истечении
    таймаута процесс (и всё его дерево) уничтожается, поднимается
    ProcessingTimeoutError – HTTP 504.
    """
    cfg = config or default_settings
    binary = require_ffmpeg(cfg)
    limit = cfg.ffmpeg_timeout_seconds if timeout is None else timeout
    command = [binary, "-hide_banner", "-nostdin", "-y", *args]

    # Канал прогресса открываем только когда он действительно нужен: открытый
    # stdout задерживает завершение процесса при принудительном убийстве.
    want_progress = on_progress is not None and duration is not None and duration > 0

    with tempfile.TemporaryFile(mode="w+b") as error_log:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if want_progress else subprocess.DEVNULL,
            stderr=error_log,
            text=True,
            encoding="utf-8",
            errors="replace",
            **_popen_kwargs(),
        )

        reader: threading.Thread | None = None
        if want_progress and process.stdout is not None:
            stream = process.stdout

            def _consume() -> None:
                try:
                    for raw_line in stream:
                        parsed = _parse_progress(raw_line.strip())
                        if not parsed:
                            continue
                        key, value = parsed
                        if key in {"out_time_ms", "out_time_us"}:
                            try:
                                seconds = int(value) / 1_000_000
                            except ValueError:
                                continue
                            on_progress(max(0.0, min(1.0, seconds / duration)))
                        elif key == "progress" and value == "end":
                            on_progress(1.0)
                except (ValueError, OSError):
                    # Канал закрылся из-за уничтожения процесса – это ожидаемо.
                    pass

            reader = threading.Thread(target=_consume, name="mediahub-ffmpeg-progress", daemon=True)
            reader.start()

        try:
            process.wait(timeout=limit)
        except subprocess.TimeoutExpired:
            logger.warning("FFmpeg превысил лимит %d с – процесс уничтожается", limit)
            _terminate_tree(process)
            if reader is not None:
                reader.join(timeout=5)
            raise ProcessingTimeoutError(
                f"Обработка превысила лимит {limit} секунд и была прервана. "
                "Попробуйте обработать файл меньшего размера или укоротить фрагмент.",
                details={"timeout_seconds": limit},
            ) from None
        finally:
            if reader is not None:
                reader.join(timeout=10)
            if process.stdout is not None:
                with contextlib.suppress(Exception):
                    process.stdout.close()

        error_log.seek(0)
        stderr = error_log.read().decode("utf-8", errors="replace")

    if process.returncode != 0:
        logger.warning("FFmpeg завершился с кодом %s: %s", process.returncode, stderr[-800:])
        raise UnprocessableFileError(_classify_error(stderr, fallback=error_hint))
    return stderr


def _find_ffprobe(binary: str) -> Path | None:
    """Ищет ffprobe рядом с ffmpeg, затем в PATH."""
    neighbour = Path(binary).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
    if neighbour.is_file():
        return neighbour
    found = shutil.which("ffprobe")
    return Path(found) if found else None


def _duration_via_ffmpeg(binary: str, path: Path) -> float | None:
    """Резервное определение длительности: разбор вывода `ffmpeg -i`."""
    try:
        completed = subprocess.run(
            [binary, "-hide_banner", "-nostdin", "-i", str(path)],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS * 3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    stderr = completed.stderr or ""
    match = re.search(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", stderr)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def probe_duration(path: Path, *, config: Settings | None = None) -> float | None:
    """Определяет длительность файла: через ffprobe, иначе через сам ffmpeg."""
    binary = find_ffmpeg(config)
    if not binary:
        return None

    probe = _find_ffprobe(binary)
    if probe is not None:
        try:
            completed = subprocess.run(
                [
                    str(probe), "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=PROBE_TIMEOUT_SECONDS * 3,
                check=False,
            )
            value = float((completed.stdout or "").strip())
            # Некоторые контейнеры отдают отрицательную или нулевую длительность.
            if value > 0:
                return value
        except (OSError, ValueError, subprocess.SubprocessError):
            pass

    return _duration_via_ffmpeg(binary, path)


# ---------------------------------------------------------------------------
# Режим A – извлечение аудиодорожки
# ---------------------------------------------------------------------------
def extract_audio(
    source: Path,
    out_dir: Path,
    *,
    bitrate: int = 192,
    sample_rate: int = 44100,
    config: Settings | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> MediaOutcome:
    """Отключает видеопоток (-vn) и кодирует первый аудиопоток в MP3."""
    if bitrate not in MP3_BITRATES:
        raise ValidationError(
            f"Битрейт MP3 должен быть одним из {sorted(MP3_BITRATES)} kbps.",
            details={"bitrate": bitrate, "allowed": sorted(MP3_BITRATES)},
        )
    if not source.is_file():
        raise UnprocessableFileError("Файл не найден в сессии.", filename=source.name)

    out_dir.mkdir(parents=True, exist_ok=True)
    destination = out_dir / f"{source.stem}.mp3"
    duration = _durations(source, config)

    _spawn_ffmpeg(
        [
            "-i", str(source),
            "-vn",                       # видео не нужно
            "-map", "0:a:0",             # первый аудиопоток
            "-c:a", "libmp3lame",
            "-b:a", f"{bitrate}k",
            "-ar", str(sample_rate),
            "-ac", "2",
            "-progress", "pipe:1", "-nostats",
            str(destination),
        ],
        duration=duration,
        on_progress=on_progress,
        config=config,
        error_hint="Не удалось извлечь аудиодорожку: возможно, в файле нет звука.",
    )

    outcome = MediaOutcome(
        files=[destination],
        input_bytes=source.stat().st_size,
        output_bytes=destination.stat().st_size,
        duration_seconds=duration,
        details={"bitrate_kbps": bitrate, "sample_rate": sample_rate, "action": "mp3"},
    )
    return outcome


# ---------------------------------------------------------------------------
# Режим B – GIF с двухпроходной палитрой
# ---------------------------------------------------------------------------
def to_gif(
    source: Path,
    out_dir: Path,
    *,
    start: float = 0.0,
    seconds: int = 15,
    fps: int = 12,
    width: int = 480,
    config: Settings | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> MediaOutcome:
    """Создаёт компактный GIF: обрезка, масштаб, fps, двухпроходная палитра."""
    if not 1 <= seconds <= 60:
        raise ValidationError("Длительность GIF должна быть от 1 до 60 секунд.", details={"seconds": seconds})
    if not 5 <= fps <= 30:
        raise ValidationError("Частота кадров GIF должна быть от 5 до 30.", details={"fps": fps})
    if not 120 <= width <= 1280:
        raise ValidationError("Ширина GIF должна быть от 120 до 1280 пикселей.", details={"width": width})
    if not source.is_file():
        raise UnprocessableFileError("Файл не найден в сессии.", filename=source.name)

    out_dir.mkdir(parents=True, exist_ok=True)
    destination = out_dir / f"{source.stem}.gif"
    palette_path = out_dir / f".{source.stem}.palette.png"
    duration = _durations(source, config)
    segment = min(float(seconds), duration) if duration else float(seconds)

    trim = ["-ss", f"{max(0.0, start)}", "-t", f"{seconds}"]
    scale = f"scale={width}:-2:flags=lanczos"

    try:
        # Проход 1: анализ отрезка и построение оптимальной палитры на 256 цветов.
        _spawn_ffmpeg(
            [
                *trim, "-i", str(source),
                "-vf", f"fps={fps},{scale},palettegen=max_colors=256:stats_mode=full",
                "-progress", "pipe:1", "-nostats",
                str(palette_path),
            ],
            duration=segment,
            on_progress=(lambda value: on_progress(value * 0.4)) if on_progress else None,
            config=config,
            error_hint="Не удалось построить палитру GIF.",
        )

        # Проход 2: рендер GIF с дизерингом (диффузия ошибок) по этой палитре.
        _spawn_ffmpeg(
            [
                *trim, "-i", str(source),
                "-i", str(palette_path),
                "-lavfi", f"fps={fps},{scale}[x];[x][1:v]paletteuse=dither=sierra2_4a:diff_mode=rectangle",
                "-loop", "0",
                "-progress", "pipe:1", "-nostats",
                str(destination),
            ],
            duration=segment,
            on_progress=(lambda value: on_progress(0.4 + value * 0.6)) if on_progress else None,
            config=config,
            error_hint="Не удалось собрать GIF из кадров.",
        )
    finally:
        palette_path.unlink(missing_ok=True)

    outcome = MediaOutcome(
        files=[destination],
        input_bytes=source.stat().st_size,
        output_bytes=destination.stat().st_size,
        duration_seconds=duration,
        details={
            "action": "gif",
            "fps": fps,
            "width": width,
            "segment_seconds": round(segment, 2),
            "palette_colors": 256,
            "dither": "sierra2_4a",
        },
    )
    return outcome


# ---------------------------------------------------------------------------
# Режим C – сжатие видео для мессенджеров
# ---------------------------------------------------------------------------
def compress_video(
    source: Path,
    out_dir: Path,
    *,
    crf: int = 27,
    preset: str = "veryfast",
    audio_bitrate: int = 128,
    max_width: int | None = None,
    config: Settings | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> MediaOutcome:
    """Пережимает видео в H.264 (profile main) + AAC 128 kbps."""
    if not 18 <= crf <= 35:
        raise ValidationError("CRF должен быть в диапазоне 18–35.", details={"crf": crf})
    if not source.is_file():
        raise UnprocessableFileError("Файл не найден в сессии.", filename=source.name)

    out_dir.mkdir(parents=True, exist_ok=True)
    destination = out_dir / f"{source.stem}_compressed.mp4"
    duration = _durations(source, config)

    filters = []
    if max_width:
        filters.append(f"scale='min({max_width},iw)':-2:flags=lanczos")
    video_args = [
        "-c:v", "libx264",
        "-profile:v", "main",   # профиль совместимости с мессенджерами
        "-level", "4.0",
        "-pix_fmt", "yuv420p",
        "-crf", str(crf),
        "-preset", preset,
        "-g", "48",
    ]
    if filters:
        video_args += ["-vf", ",".join(filters)]

    _spawn_ffmpeg(
        [
            "-i", str(source),
            "-map", "0:v:0",
            "-map", "0:a:0?",
            *video_args,
            "-c:a", "aac",
            "-b:a", f"{audio_bitrate}k",
            "-ac", "2",
            "-movflags", "+faststart",
            "-progress", "pipe:1", "-nostats",
            str(destination),
        ],
        duration=duration,
        on_progress=on_progress,
        config=config,
        error_hint="Не удалось сжать видео.",
    )

    outcome = MediaOutcome(
        files=[destination],
        input_bytes=source.stat().st_size,
        output_bytes=destination.stat().st_size,
        duration_seconds=duration,
        details={"action": "compress", "crf": crf, "preset": preset, "audio_bitrate_kbps": audio_bitrate},
    )
    return outcome


def inspect(path: Path, *, config: Settings | None = None) -> dict:
    """Быстрая проверка медиафайла: наличие FFmpeg и длительность."""
    if not path.is_file() or path.stat().st_size == 0:
        raise UnprocessableFileError("Файл пуст или отсутствует.", filename=path.name)
    if not find_ffmpeg(config):
        raise DependencyUnavailableError(
            INSTALL_HINT, details={"dependency": "ffmpeg", "install_hint": INSTALL_HINT}
        )
    return {
        "filename": path.name,
        "size": path.stat().st_size,
        "duration_seconds": _durations(path, config),
    }


def install_environment_report(config: Settings | None = None) -> dict:
    cfg = config or default_settings
    report = availability(cfg)
    report["python"] = sys.version.split()[0]
    report["platform"] = sys.platform
    report["vendor_dir"] = str(cfg.ffmpeg_vendor_dir)
    return report
