"""Конвейер обработки: связывает REST-параметры с сервисами трансформации.

Здесь же выполняется «предполётная» проверка типов (инспектор содержимого на
сервере): проверяются magic bytes и соответствие заявленному типу – подделанные
расширения отсекаются ещё до запуска конвертации.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import Settings, settings as default_settings
from .errors import UnprocessableFileError, ValidationError
from .services import image as image_service
from .services import media as media_service
from .services import pdf as pdf_service
from .zipper import bundle

logger = logging.getLogger("mediahub.pipeline")

ProgressCallback = Callable[[float, str], None]

KINDS = ("image", "pdf", "video")

IMAGE_ACTIONS = {"convert", "to_jpg", "to_png", "to_webp"}
PDF_ACTIONS = {"merge", "compress", "to_images"}
VIDEO_ACTIONS = {"mp3", "gif", "compress"}


@dataclass
class PreparedResult:
    """Итог конвейера, готовый к отдаче клиенту."""

    files: list[Path] = field(default_factory=list)
    bundle_path: Path | None = None
    download_name: str = "result"
    content_type: str = "application/octet-stream"
    #: Требуется ли упаковать результат в ZIP (переопределяет правило «больше одного файла»).
    wants_bundle: bool = False
    input_bytes: int = 0
    output_bytes: int = 0
    file_count: int = 0
    warnings: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)

    @property
    def savings_percent(self) -> float:
        if self.input_bytes <= 0:
            return 0.0
        return round((1 - self.output_bytes / self.input_bytes) * 100, 1)

    def as_dict(self) -> dict:
        return {
            "download_name": self.download_name,
            "content_type": self.content_type,
            "input_bytes": self.input_bytes,
            "output_bytes": self.output_bytes,
            "savings_percent": self.savings_percent,
            "file_count": self.file_count,
            "warnings": self.warnings,
            "details": self.details,
        }


@dataclass
class RequestSpec:
    """Нормализованный запрос: вид, действие и параметры."""

    kind: str
    action: str
    inputs: list[Path]
    options: dict
    #: Каталог `out/` сессии, куда складываются результаты.
    out_dir: Path | None = None


def _norm_kind(raw: str) -> str:
    kind = (raw or "").strip().lower()
    aliases = {"image": "image", "images": "image", "img": "image", "photo": "image",
               "pdf": "pdf", "document": "pdf", "doc": "pdf",
               "video": "video", "audio": "video", "media": "video", "vid": "video"}
    if kind not in aliases:
        raise ValidationError(
            f"Неизвестный тип данных «{raw}». Допустимо: image, pdf, video.",
            details={"allowed": list(KINDS)},
        )
    return aliases[kind]


def _norm_action(kind: str, raw: str, file_count: int) -> str:
    action = (raw or "").strip().lower()
    if not action:
        # Осмысленные значения по умолчанию.
        if kind == "image":
            return "convert"
        if kind == "pdf":
            return "merge" if file_count > 1 else "compress"
        return "compress"
    if kind == "image":
        if action in IMAGE_ACTIONS:
            return action
    elif kind == "pdf":
        if action in PDF_ACTIONS:
            return action
    elif kind == "video":
        if action in VIDEO_ACTIONS:
            return action
    raise ValidationError(
        f"Действие «{raw}» не поддерживается для типа «{kind}».",
        details={
            "kind": kind,
            "allowed": sorted(
                IMAGE_ACTIONS if kind == "image" else PDF_ACTIONS if kind == "pdf" else VIDEO_ACTIONS
            ),
        },
    )


def _opt_int(options: dict, key: str, default: int) -> int:
    value = options.get(key)
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"Параметр «{key}» должен быть целым числом.", details={key: value}) from None


def _opt_float(options: dict, key: str, default: float) -> float:
    value = options.get(key)
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValidationError(f"Параметр «{key}» должен быть числом.", details={key: value}) from None


def _opt_bool(options: dict, key: str, default: bool) -> bool:
    value = options.get(key)
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "да"}


def _out_dir(spec: RequestSpec) -> Path:
    """Каталог `out/` сессии; при отсутствии – рядом с первым входным файлом."""
    if spec.out_dir is not None:
        spec.out_dir.mkdir(parents=True, exist_ok=True)
        return spec.out_dir
    fallback = spec.inputs[0].parent / "out"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


#: Расширение -> MIME-тип результата.
_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
    ".pdf": "application/pdf",
    ".zip": "application/zip",
}


def _guess_content_type(path: Path) -> str:
    return _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


def validate_options(spec: RequestSpec, config: Settings | None = None) -> None:
    """Проверяет параметры запроса до постановки задачи в очередь.

    Именно здесь отсекаются некорректные значения (качество вне диапазона, DPI
    не из списка, слишком много файлов для склейки): клиент получает 400 сразу,
    а не через статус уже запущенной задачи.
    """
    cfg = config or default_settings
    count = len(spec.inputs)
    if count > cfg.max_files_per_request:
        raise ValidationError(
            f"Слишком много файлов: {count}. Лимит – {cfg.max_files_per_request}.",
            details={"count": count, "limit": cfg.max_files_per_request},
        )

    if spec.kind == "image":
        target = str(spec.options.get("format") or spec.options.get("target") or "jpg")
        if spec.action in {"to_jpg", "to_png", "to_webp"}:
            target = spec.action.replace("to_", "")
        if target.strip().lower().lstrip(".") not in image_service.TARGET_FORMATS:
            raise ValidationError(
                f"Неподдерживаемый целевой формат «{target}». Допустимо: JPG, PNG, WEBP.",
                details={"allowed": ["jpg", "png", "webp"]},
            )
        quality = _opt_int(spec.options, "quality", cfg.default_image_quality)
        if not 1 <= quality <= 100:
            raise ValidationError(
                "Качество изображения должно быть в диапазоне 1–100.", details={"quality": quality}
            )
        return

    if spec.kind == "pdf":
        if spec.action == "merge":
            if count < 2:
                raise ValidationError("Для склейки нужно минимум 2 PDF-файла.", details={"count": count})
            if count > cfg.max_pdf_merge_files:
                raise ValidationError(
                    f"За один раз можно склеить не более {cfg.max_pdf_merge_files} PDF-файлов.",
                    details={"count": count, "limit": cfg.max_pdf_merge_files},
                )
        elif count != 1:
            raise ValidationError(
                "Этот режим PDF поддерживает ровно один файл.", details={"count": count}
            )
        if spec.action == "compress":
            quality = _opt_int(spec.options, "quality", 65)
            if not 10 <= quality <= 95:
                raise ValidationError(
                    "Качество сжатия PDF должно быть в диапазоне 10–95.",
                    details={"quality": quality},
                )
        elif spec.action == "to_images":
            dpi = _opt_int(spec.options, "dpi", cfg.default_pdf_dpi)
            if dpi not in pdf_service.ALLOWED_RENDER_DPI:
                raise ValidationError(
                    f"DPI должен быть одним из {list(pdf_service.ALLOWED_RENDER_DPI)}.",
                    details={"dpi": dpi, "allowed": list(pdf_service.ALLOWED_RENDER_DPI)},
                )
            fmt = str(spec.options.get("format") or "png").strip().lower().lstrip(".")
            if fmt not in {"png", "jpg", "jpeg", "webp"}:
                raise ValidationError(
                    "Формат рендера: png, jpg или webp.", details={"format": fmt}
                )
        return

    # Видео и аудио.
    if count != 1:
        raise ValidationError(
            "Обработка видео поддерживает ровно один файл за раз.", details={"count": count}
        )
    if spec.action == "mp3":
        bitrate = _opt_int(spec.options, "bitrate", cfg.default_mp3_bitrate)
        if bitrate not in media_service.MP3_BITRATES:
            raise ValidationError(
                f"Битрейт MP3 должен быть одним из {sorted(media_service.MP3_BITRATES)} kbps.",
                details={"bitrate": bitrate, "allowed": sorted(media_service.MP3_BITRATES)},
            )
    elif spec.action == "gif":
        seconds = _opt_int(spec.options, "seconds", cfg.default_gif_seconds)
        fps = _opt_int(spec.options, "fps", cfg.default_gif_fps)
        width = _opt_int(spec.options, "width", cfg.default_gif_width)
        if not 1 <= seconds <= 60:
            raise ValidationError(
                "Длительность GIF должна быть от 1 до 60 секунд.", details={"seconds": seconds}
            )
        if not 5 <= fps <= 30:
            raise ValidationError(
                "Частота кадров GIF должна быть от 5 до 30.", details={"fps": fps}
            )
        if not 120 <= width <= 1280:
            raise ValidationError(
                "Ширина GIF должна быть от 120 до 1280 пикселей.", details={"width": width}
            )
    elif spec.action == "compress":
        crf = _opt_int(spec.options, "crf", cfg.default_video_crf)
        if not 18 <= crf <= 35:
            raise ValidationError("CRF должен быть в диапазоне 18–35.", details={"crf": crf})


def sniff(spec: RequestSpec, config: Settings | None = None) -> list[dict]:
    """Проверяет параметры запроса и содержимое файлов.

    Сначала отсекаются некорректные параметры (400), затем – подделанные или
    неподдерживаемые файлы по magic bytes (422).
    """
    cfg = config or default_settings
    if not spec.inputs:
        raise ValidationError("Не передано ни одного файла.")
    validate_options(spec, cfg)

    reports: list[dict] = []
    for path in spec.inputs:
        if spec.kind == "image":
            reports.append(image_service.inspect(path))
        elif spec.kind == "pdf":
            reports.append(pdf_service.inspect(path))
        else:
            reports.append(media_service.inspect(path, config=cfg))
    return reports


def _prepare_image(spec: RequestSpec, prepared: PreparedResult, cfg: Settings) -> None:
    target = str(spec.options.get("format") or spec.options.get("target") or "jpg")
    if spec.action in {"to_jpg", "to_png", "to_webp"}:
        target = spec.action.replace("to_", "")
    quality = _opt_int(spec.options, "quality", cfg.default_image_quality)
    if not 1 <= quality <= 100:
        raise ValidationError("Качество изображения должно быть в диапазоне 1–100.", details={"quality": quality})

    max_width = _opt_int(spec.options, "width", 0)
    if max_width and not 16 <= max_width <= 10000:
        raise ValidationError(
            "Ширина должна быть от 16 до 10 000 пикселей.",
            details={"width": max_width},
        )

    target_kb = _opt_int(spec.options, "target_size_kb", 0)
    if target_kb and not 10 <= target_kb <= 20000:
        raise ValidationError(
            "Целевой размер должен быть от 10 КБ до 20 МБ.",
            details={"target_size_kb": target_kb},
        )

    outcome = image_service.convert(
        spec.inputs,
        _out_dir(spec),
        target=target,
        quality=quality,
        strip_metadata=_opt_bool(spec.options, "strip_metadata", True),
        max_bytes=target_kb * 1024 if target_kb else None,
        max_width=max_width or None,
    )
    prepared.files = outcome.files
    prepared.input_bytes = outcome.input_bytes
    prepared.output_bytes = outcome.output_bytes
    prepared.warnings.extend(outcome.warnings)
    prepared.details.update({"action": "convert", "target_format": target, "quality": quality, "target_size_kb": target_kb or None, "max_width": max_width or None,
                             "per_file": outcome.details})
    prepared.download_name = outcome.files[0].name if len(outcome.files) == 1 else "images.zip"


def _prepare_pdf(spec: RequestSpec, prepared: PreparedResult, cfg: Settings) -> None:
    out_dir = _out_dir(spec)
    if spec.action == "merge":
        if len(spec.inputs) > cfg.max_pdf_merge_files:
            raise ValidationError(
                f"За один раз можно склеить не более {cfg.max_pdf_merge_files} PDF-файлов.",
                details={"count": len(spec.inputs), "limit": cfg.max_pdf_merge_files},
            )
        outcome = pdf_service.merge(spec.inputs, out_dir)
        prepared.download_name = "merged.pdf"
    elif spec.action == "compress":
        if len(spec.inputs) != 1:
            raise ValidationError("Сжатие PDF поддерживает ровно один файл.")
        quality = _opt_int(spec.options, "quality", 65)
        outcome = pdf_service.compress(spec.inputs[0], out_dir, quality=quality)
        prepared.download_name = outcome.files[0].name
    elif spec.action == "to_images":
        if len(spec.inputs) != 1:
            raise ValidationError("Экспорт в изображения поддерживает ровно один файл.")
        dpi = _opt_int(spec.options, "dpi", cfg.default_pdf_dpi)
        fmt = str(spec.options.get("format") or "png")
        outcome = pdf_service.render_pages(spec.inputs[0], out_dir, dpi=dpi, fmt=fmt)
        prepared.download_name = f"{spec.inputs[0].stem}_pages.zip"
        prepared.wants_bundle = True
    else:  # pragma: no cover – действие уже нормализовано
        raise ValidationError(f"Действие «{spec.action}» не поддерживается для PDF.")

    prepared.files = outcome.files
    prepared.input_bytes = outcome.input_bytes
    prepared.output_bytes = outcome.output_bytes
    prepared.warnings.extend(outcome.warnings)
    prepared.details.update({"action": spec.action, **outcome.details})


def _prepare_video(
    spec: RequestSpec,
    prepared: PreparedResult,
    cfg: Settings,
    on_progress: ProgressCallback | None,
) -> None:
    out_dir = _out_dir(spec)
    if len(spec.inputs) != 1:
        raise ValidationError("Обработка видео поддерживает ровно один файл за раз.")
    source = spec.inputs[0]

    def media_progress(value: float) -> None:
        if on_progress:
            percent = 5 + value * 90
            on_progress(percent, "Выполняется конвертация (это может занять время для больших видео)...")

    if spec.action == "mp3":
        outcome = media_service.extract_audio(
            source, out_dir,
            bitrate=_opt_int(spec.options, "bitrate", cfg.default_mp3_bitrate),
            config=cfg,
            on_progress=media_progress,
        )
    elif spec.action == "gif":
        outcome = media_service.to_gif(
            source, out_dir,
            start=_opt_float(spec.options, "start", 0.0),
            seconds=_opt_int(spec.options, "seconds", cfg.default_gif_seconds),
            fps=_opt_int(spec.options, "fps", cfg.default_gif_fps),
            width=_opt_int(spec.options, "width", cfg.default_gif_width),
            config=cfg,
            on_progress=media_progress,
        )
    elif spec.action == "compress":
        outcome = media_service.compress_video(
            source, out_dir,
            crf=_opt_int(spec.options, "crf", cfg.default_video_crf),
            audio_bitrate=_opt_int(spec.options, "audio_bitrate", 128),
            config=cfg,
            on_progress=media_progress,
        )
    else:  # pragma: no cover
        raise ValidationError(f"Действие «{spec.action}» не поддерживается для видео.")

    prepared.files = outcome.files
    prepared.input_bytes = outcome.input_bytes
    prepared.output_bytes = outcome.output_bytes
    prepared.warnings.extend(outcome.warnings)
    prepared.details.update({"duration_seconds": outcome.duration_seconds, **outcome.details})
    prepared.download_name = outcome.files[0].name


def build_spec(
    kind: str,
    action: str,
    inputs: list[Path],
    options: dict | None = None,
    out_dir: Path | None = None,
) -> RequestSpec:
    norm_kind = _norm_kind(kind)
    return RequestSpec(
        kind=norm_kind,
        action=_norm_action(norm_kind, action, len(inputs)),
        inputs=list(inputs),
        options=dict(options or {}),
        out_dir=out_dir,
    )


def execute(
    spec: RequestSpec,
    out_dir: Path,
    *,
    bundle_output: bool | None = None,
    config: Settings | None = None,
    on_progress: ProgressCallback | None = None,
) -> PreparedResult:
    """Выполняет запрос и возвращает результат, упакованный при необходимости.

    По умолчанию ZIP создаётся, когда результат содержит больше одного файла,
    либо когда этого явно требует режим (например, «PDF -> картинки»).
    """
    cfg = config or default_settings
    prepared = PreparedResult()
    if spec.out_dir is None:
        spec.out_dir = out_dir

    if on_progress:
        on_progress(5.0, "Проверка файлов...")
    sniff(spec, cfg)

    if on_progress:
        on_progress(15.0, "Выполняется конвертация...")

    if spec.kind == "image":
        _prepare_image(spec, prepared, cfg)
    elif spec.kind == "pdf":
        _prepare_pdf(spec, prepared, cfg)
    else:
        _prepare_video(spec, prepared, cfg, on_progress)

    prepared.file_count = len(prepared.files)
    if not prepared.files:
        raise UnprocessableFileError("Обработка завершилась без результата.")

    should_bundle = (
        prepared.wants_bundle
        if bundle_output is None
        else bundle_output
    ) or len(prepared.files) > 1
    if spec.options.get("bundle") is not None:
        should_bundle = _opt_bool(spec.options, "bundle", should_bundle)

    if should_bundle:
        if on_progress:
            on_progress(95.0, "Упаковка результата...")
        zip_name = prepared.download_name
        if not zip_name.lower().endswith(".zip"):
            zip_name = f"{Path(zip_name).stem}.zip"
        archive, _ = bundle(prepared.files, out_dir / zip_name)
        prepared.bundle_path = archive
        prepared.download_name = zip_name
        prepared.content_type = "application/zip"
    else:
        # Один файл: отдаём его напрямую, без промежуточного архива.
        prepared.bundle_path = None
        single = prepared.files[0]
        prepared.download_name = single.name
        prepared.content_type = _guess_content_type(single)

    if on_progress:
        on_progress(100.0, "Готово")
    return prepared
