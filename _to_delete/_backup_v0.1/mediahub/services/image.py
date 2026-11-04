"""Этап 2a – сервис обработки растровых изображений (Pillow).

Реализует ТЗ 3.1:
  * поддержка JPEG, PNG, WEBP, HEIC/HEIF, BMP, TIFF;
  * принудительное приведение палитровых изображений (mode P) к RGBA;
  * корректное сведение альфа-канала на белый холст при экспорте в JPEG;
  * физический разворот пикселей по EXIF-тегу ориентации;
  * управление качеством для JPEG/WEBP, максимальная оптимизация метода WEBP.

Модуль не знает ничего о HTTP – это чистый слой трансформации.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageFile as PillowImageFile, ImageOps, UnidentifiedImageError

from ..errors import UnprocessableFileError, ValidationError

logger = logging.getLogger("mediahub.services.image")

#: Разрешаем догрузку усечённых JPEG/PNG – иначе часть валидных фото падает.
PillowImageFile.LOAD_TRUNCATED_IMAGES = True

#: Формат Pillow -> целевой ключ API.
PILLOW_TO_TARGET = {
    "JPEG": "jpeg",
    "MPO": "jpeg",
    "PNG": "png",
    "WEBP": "webp",
    "BMP": "bmp",
    "TIFF": "tiff",
    "HEIF": "heic",
    "AVIF": "avif",
    "GIF": "gif",
}

#: Целевой формат -> (расширение, формат Pillow).
TARGET_FORMATS = {
    "jpg": ("jpg", "JPEG"),
    "jpeg": ("jpg", "JPEG"),
    "png": ("png", "PNG"),
    "webp": ("webp", "WEBP"),
}

#: Расширения и MIME, которые принимает модуль.
SUPPORTED_SUFFIXES = {
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".webp", ".heic", ".heif",
    ".bmp", ".tif", ".tiff", ".avif",
}
SUPPORTED_MIME_PREFIX = "image/"

#: Форматы, у которых есть альфа-канал и которые его не поддерживают.
_ALPHA_MODES = {"RGBA", "LA", "PA", "RGBa", "La"}
_OPAQUE_MODES = {"RGB", "L", "1", "CMYK", "YCbCr", "I", "F"}

WHITE = (255, 255, 255)


@dataclass
class ConversionOutcome:
    """Результат конвертации пачки изображений."""

    files: list[Path] = field(default_factory=list)
    input_bytes: int = 0
    output_bytes: int = 0
    warnings: list[str] = field(default_factory=list)
    details: list[dict] = field(default_factory=list)

    @property
    def savings_percent(self) -> float:
        if self.input_bytes <= 0:
            return 0.0
        return round((1 - self.output_bytes / self.input_bytes) * 100, 1)


def _configure_heif() -> str | None:
    """Регистрирует opener для HEIC/HEIF, если доступен pillow-heif."""
    try:
        import pillow_heif  # type: ignore
    except ImportError:
        return None
    try:
        pillow_heif.register_heif_opener()
        return "pillow-heif"
    except Exception:  # noqa: BLE001 – несовместимая версия не должна ломать сервис
        logger.warning("pillow-heif установлен, но не зарегистрирован", exc_info=True)
        return None


HEIF_BACKEND = _configure_heif()


def heif_available() -> bool:
    return HEIF_BACKEND is not None


def _validate_pillow_format(image: Image.Image, path: Path) -> str:
    """Проверяет, что Pillow действительно распознал растр (защита от подделки магии)."""
    fmt = (image.format or "").upper()
    if fmt not in PILLOW_TO_TARGET:
        raise UnprocessableFileError(
            f"Файл не является поддерживаемым изображением (определён формат: {fmt or 'неизвестно'}).",
            filename=path.name,
        )
    if fmt == "HEIF" and HEIF_BACKEND is None:
        raise UnprocessableFileError(
            "Для чтения HEIC/HEIF требуется библиотека pillow-heif.",
            filename=path.name,
        )
    return fmt


def inspect(path: Path) -> dict:
    """Лёгкая валидация: открывает файл и читает заголовок без декодирования пикселей."""
    if not path.is_file() or path.stat().st_size == 0:
        raise UnprocessableFileError("Файл пуст или отсутствует.", filename=path.name)
    try:
        with Image.open(path) as image:
            fmt = _validate_pillow_format(image, path)
            width, height = image.size
            return {
                "filename": path.name,
                "format": PILLOW_TO_TARGET.get(fmt, fmt.lower()),
                "mode": image.mode,
                "width": width,
                "height": height,
                "size": path.stat().st_size,
            }
    except UnidentifiedImageError as exc:  # подделанное расширение / битый файл
        raise UnprocessableFileError(
            "Содержимое файла не является изображением (возможно, расширение подделано).",
            filename=path.name,
        ) from exc
    except UnprocessableFileError:
        raise
    except OSError as exc:
        raise UnprocessableFileError(
            f"Не удалось прочитать изображение: {exc}", filename=path.name
        ) from exc


def _to_rgba(image: Image.Image) -> Image.Image:
    """Приводит любую картинку к полноценному RGBA (ТЗ 3.1: mode P -> RGBA)."""
    if image.mode == "P":
        # Палитровые изображения могут нести прозрачность в info["transparency"].
        return image.convert("RGBA")
    if image.mode in _ALPHA_MODES:
        return image.convert("RGBA")
    return image.convert("RGBA")


def _flatten_on_white(image: Image.Image) -> Image.Image:
    """Сводит изображение с альфа-каналом на непрозрачный белый холст #FFFFFF."""
    rgba = _to_rgba(image)
    canvas = Image.new("RGB", rgba.size, WHITE)
    canvas.paste(rgba, mask=rgba.split()[-1])
    return canvas


def _prepare(image: Image.Image, target_format: str) -> Image.Image:
    """Готовит пиксельные данные под целевой формат."""
    # 1. Палитра -> RGBA (до любых манипуляций).
    work = _to_rgba(image)

    # 2. Целевой формат без альфа-канала: плоский белый холст.
    if target_format == "JPEG":
        if work.mode in _ALPHA_MODES or "A" in work.getbands():
            return _flatten_on_white(work)
        return work.convert("RGB")

    # 3. Целевые форматы с поддержкой прозрачности сохраняют RGBA.
    if target_format == "PNG":
        return work
    if target_format == "WEBP":
        return work
    return work


def _save_kwargs(target_format: str, quality: int, *, has_alpha: bool) -> dict:
    if target_format == "JPEG":
        return {
            "format": "JPEG",
            "quality": quality,
            "optimize": True,
            "progressive": True,
            "subsampling": 0 if quality >= 90 else 2,
        }
    if target_format == "WEBP":
        # ТЗ 3.1: максимальный уровень оптимизации метода сжатия.
        return {
            "format": "WEBP",
            "quality": quality,
            "method": 6,
            "exact": has_alpha,
        }
    if target_format == "PNG":
        return {"format": "PNG", "optimize": True, "compress_level": 9}
    return {"format": target_format}


def _output_name(stem: str, suffix: str, index: int | None) -> str:
    safe_stem = stem or "image"
    if index is None:
        return f"{safe_stem}.{suffix}"
    return f"{safe_stem}_{index:03d}.{suffix}"


def convert(
    inputs: Iterable[Path],
    out_dir: Path,
    *,
    target: str = "jpg",
    quality: int = 85,
    strip_metadata: bool = True,
    max_bytes: int | None = None,
    max_width: int | None = None,
) -> ConversionOutcome:
    """Конвертирует изображения в целевой формат.

    :param inputs: пути к исходникам в порядке, заданном пользователем.
    :param out_dir: каталог `out/` сессии.
    :param target: jpg | png | webp.
    :param quality: 1–100, применяется к JPEG и WEBP.
    """
    paths = list(inputs)
    if not paths:
        raise ValidationError("Не передано ни одного изображения.")

    target_key = target.strip().lower().lstrip(".")
    if target_key not in TARGET_FORMATS:
        raise ValidationError(
            f"Неподдерживаемый целевой формат «{target}». Допустимо: JPG, PNG, WEBP.",
            details={"allowed": sorted({v[0] for v in TARGET_FORMATS.values()})},
        )
    if not 1 <= quality <= 100:
        raise ValidationError("Качество должно быть в диапазоне 1–100.", details={"quality": quality})

    out_suffix, pillow_format = TARGET_FORMATS[target_key]
    supports_alpha = pillow_format in {"PNG", "WEBP"}
    multiple = len(paths) > 1
    out_dir.mkdir(parents=True, exist_ok=True)

    outcome = ConversionOutcome()
    used_names: set[str] = set()

    for index, source in enumerate(paths, start=1):
        if not source.is_file():
            raise UnprocessableFileError("Файл не найден в сессии.", filename=source.name)
        outcome.input_bytes += source.stat().st_size

        try:
            with Image.open(source) as raw:
                fmt = _validate_pillow_format(raw, source)
                raw.load()
                # EXIF Orientation -> физический разворот пикселей.
                oriented = ImageOps.exif_transpose(raw)
                if oriented is None:
                    oriented = raw.copy()
                exif_payload = None
                if not strip_metadata:
                    # Ориентацию сбрасываем: пиксели уже развёрнуты.
                    try:
                        exif_payload = oriented.getexif()
                        if exif_payload:
                            exif_payload[0x0112] = 1
                            exif_payload = exif_payload.tobytes()
                    except Exception:  # noqa: BLE001 – метаданные не критичны
                        exif_payload = None

                prepared = _prepare(oriented, pillow_format)
                if max_width and prepared.width > max_width:
                    # Вписываем в заданную ширину, сохраняя пропорции.
                    height = max(1, round(prepared.height * max_width / prepared.width))
                    prepared = prepared.resize((max_width, height), Image.LANCZOS)
                has_alpha = "A" in prepared.getbands()
                name = _output_name(source.stem, out_suffix, index if multiple else None)
                if name in used_names:
                    name = _output_name(source.stem, out_suffix, index)
                used_names.add(name)
                destination = out_dir / name

                if max_bytes and pillow_format in {"JPEG", "WEBP"}:
                    # Подбираем качество под цель по размеру, от высокого к низкому.
                    chosen = quality
                    for candidate in (92, 84, 76, 68, 60, 52, 44, 36, 28, 20):
                        attempt = _save_kwargs(pillow_format, candidate, has_alpha=has_alpha)
                        if exif_payload:
                            attempt["exif"] = exif_payload
                        prepared.save(destination, **attempt)
                        chosen = candidate
                        if destination.stat().st_size <= max_bytes:
                            break
                    quality_used = chosen
                else:
                    kwargs = _save_kwargs(pillow_format, quality, has_alpha=has_alpha)
                    if exif_payload:
                        kwargs["exif"] = exif_payload
                    prepared.save(destination, **kwargs)
                    quality_used = quality
                if max_bytes:
                    outcome.details.append(
                        {"name": name, "quality": quality_used,
                         "bytes": destination.stat().st_size}
                    )
                if prepared is not raw:
                    prepared.close()
        except UnprocessableFileError:
            raise
        except (OSError, ValueError) as exc:
            raise UnprocessableFileError(
                f"Не удалось преобразовать изображение: {exc}", filename=source.name
            ) from exc

        size = destination.stat().st_size
        outcome.files.append(destination)
        outcome.output_bytes += size
        outcome.details.append(
            {
                "source": source.name,
                "output": destination.name,
                "source_format": PILLOW_TO_TARGET.get(fmt, fmt.lower()),
                "target_format": target_key,
                "source_bytes": source.stat().st_size,
                "output_bytes": size,
            }
        )

    if not outcome.files:
        raise UnprocessableFileError("Ни один файл не был сконвертирован.")
    return outcome
