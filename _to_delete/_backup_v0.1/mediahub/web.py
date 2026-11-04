"""Этап 3 – REST API поверх сервисов трансформации.

Слой отвечает за: приём и стриминг файлов в сессию, валидацию типов, постановку
фоновых задач, отдачу результатов и QR-мост для мобильных устройств.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import time
from pathlib import Path

from fastapi import FastAPI, File, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import __version__, pipeline
from .config import PROJECT_ROOT, Settings, settings as default_settings
from .errors import (
    ConflictError,
    MediaHubError,
    NotFoundError,
    PayloadTooLargeError,
    ValidationError,
)
from .jobs import STATUS_DONE, STATUS_ERROR, STATUS_PROCESSING, STATUS_QUEUED, JobManager
from .network import NetworkInfo
from .services import media as media_service
from .storage import GarbageCollector, TaskStore, sanitize_filename

logger = logging.getLogger("mediahub.web")

UPLOAD_CHUNK = 1024 * 1024  # 1 МиБ
#: Минимальная страница для телефона: выбрать файлы и отправить на компьютер.
SEND_PAGE = """<!DOCTYPE html>
<html lang="ru"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>MediaHub – отправить файлы</title>
<style>
body{margin:0;background:#fff;color:#000;font:17px/22px -apple-system,"Segoe UI",system-ui,sans-serif}
main{max-width:420px;margin:0 auto;padding:64px 24px}
h1{font-size:22px;margin:0 0 6px}
p{color:rgba(60,60,67,.7);margin:0 0 24px;font-size:15px}
label{display:block;border:1px solid #e3e3e8;background:#fbfbfd;border-radius:16px;padding:48px 20px;text-align:center;cursor:pointer}
label b{display:block;font-size:17px;color:#000;margin-bottom:4px}
label span{color:#007aff}
input{display:none}
button{margin-top:18px;width:100%;height:50px;border:0;border-radius:12px;background:#007aff;color:#fff;font:600 17px inherit}
#status{margin-top:16px;font-size:14px;color:rgba(60,60,67,.7);text-align:center}
@media (prefers-color-scheme: dark){body{background:#000;color:#fff}label{background:#101012;border-color:#2c2c2e}p,#status{color:rgba(235,235,245,.7)}label b{color:#fff}}
</style></head>
<body><main>
<h1>Отправить на компьютер</h1>
<p>Выберите фото или файлы – они появятся в окне MediaHub.</p>
<label><b>Выбрать файлы</b><span>или снимите на камеру</span>
<input id="files" type="file" multiple>
</label>
<button id="send">Отправить</button>
<p id="status"></p>
<script>
const input = document.getElementById('files');
const status = document.getElementById('status');
document.getElementById('send').addEventListener('click', async () => {
  if (!input.files.length) { status.textContent = 'Сначала выберите файлы.'; return; }
  const data = new FormData();
  for (const file of input.files) data.append('files', file, file.name);
  status.textContent = 'Отправка…';
  try {
    const response = await fetch('/send/__TASK__', { method: 'POST', body: data });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.message || 'Не получилось');
    status.textContent = 'Готово: ' + payload.count + ' файл(ов) на компьютере.';
    input.value = '';
  } catch (error) {
    status.textContent = error.message;
  }
});
</script>
</main></body></html>"""

#: Поля формы, не относящиеся к файлам: из них собирается словарь опций.
_RESERVED_FIELDS = {"kind", "action", "token"}

#: Опции, которые принимаются как есть.
_OPTION_FIELDS = {
    "format", "target", "quality", "dpi", "bitrate", "fps", "width", "seconds",
    "start", "crf", "bundle", "strip_metadata", "audio_bitrate", "target_size_kb",
}

#: Служебные поля запроса, не попадающие в опции конвертации.
_SERVICE_FIELDS = _RESERVED_FIELDS | {"max_bytes", "max_files"}



#: Ограничение частоты: не больше 20 запросов в минуту с одного адреса.
_RATE_LIMIT = 20
_RATE_WINDOW = 60.0
_rate_hits: dict[str, list[float]] = {}


def _too_many_requests(client: str) -> bool:
    """Скользящее окно на минуту. True – запрос надо отклонить (429)."""
    now = time.monotonic()
    bucket = [moment for moment in _rate_hits.get(client, []) if now - moment < _RATE_WINDOW]
    if len(bucket) >= _RATE_LIMIT:
        _rate_hits[client] = bucket
        return True
    bucket.append(now)
    _rate_hits[client] = bucket
    return False
def _client_is_local(request: Request) -> bool:
    """Разрешает запросы только из приватных/локальных сетей."""
    import ipaddress

    client = request.client.host if request.client else ""
    if client in {"testclient", "localhost", ""}:
        return True
    try:
        address = ipaddress.ip_address(client)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def _check_token(request: Request, settings: Settings) -> None:
    expected = os.environ.get("MEDIAHUB_TOKEN", "").strip()
    if not expected:
        return
    provided = (
        request.headers.get("x-mediahub-token")
        or request.query_params.get("token")
        or ""
    )
    # Сравнение в байтах: compare_digest не принимает не-ASCII строки.
    if not secrets.compare_digest(
        str(provided).encode("utf-8"), expected.encode("utf-8")
    ):
        raise ValidationError(
            "Неверный или отсутствующий токен доступа (заголовок X-MediaHub-Token)."
        )


def _form_int(raw, default: int) -> int:
    """Безопасно читает целое из поля формы; мусор отсекается как 400."""
    if raw in (None, ""):
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        raise ValidationError(
            "Некорректное числовое значение в запросе.", details={"value": str(raw)}
        ) from None


def _collect_options(form) -> dict:
    options: dict = {}
    for field in _OPTION_FIELDS:
        value = form.get(field)
        if value not in (None, ""):
            options[field] = value
    return options


def _resolve_download(task_root_in: Path, task_root_out: Path, requested: str | None,
                      bundle_path: Path | None, files: list[Path]) -> Path:
    """Находит файл для скачивания, не позволяя выйти за пределы сессии."""
    if requested:
        relative = requested.replace("\\", "/").lstrip("/")
        if ".." in relative.split("/"):
            raise ValidationError("Недопустимое имя файла.")
        for base in (task_root_out, task_root_in):
            candidate = (base / relative).resolve()
            base_resolved = base.resolve()
            if base_resolved in candidate.parents and candidate.is_file():
                return candidate
        raise NotFoundError("Файл не найден в сессии.")

    if bundle_path is not None and bundle_path.is_file():
        return bundle_path
    if files:
        return files[0]
    raise NotFoundError("Результат ещё не готов или уже удалён.")


def create_app(config: Settings | None = None) -> FastAPI:
    cfg = config or default_settings
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    store = TaskStore(cfg)
    jobs = JobManager(store, cfg)
    gc = GarbageCollector(store, cfg)
    network = NetworkInfo(cfg)

    def _on_task_removed(task_id: str) -> None:
        jobs.forget_task(task_id)

    gc.on_task_removed = _on_task_removed

    app = FastAPI(
        title="MediaHub – автономный медиа-хаб и конвертер",
        version="0.1.0",
        description=(
            "Локальный инструмент конвертации изображений, PDF и медиа. "
            "Обработка выполняется только на этой машине."
        ),
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.settings = cfg
    app.state.store = store
    app.state.jobs = jobs
    app.state.gc = gc
    app.state.network = network

    # --- обработчики ошибок ----------------------------------------------
    @app.exception_handler(MediaHubError)
    async def _mediahub_error_handler(request: Request, exc: MediaHubError) -> JSONResponse:
        if exc.status_code >= 500:
            logger.error("Ошибка обработки: %s", exc.message)
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    # --- middleware -------------------------------------------------------
    @app.middleware("http")
    async def _guard(request: Request, call_next):
        if request.url.path.startswith("/api") or request.url.path.startswith("/download") \
                or request.url.path.startswith("/qr"):
            if not cfg.allow_public_clients and not _client_is_local(request):
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": "forbidden",
                        "message": "Доступ разрешён только с локальных адресов.",
                    },
                )
        heavy = request.method == "POST" and (
            request.url.path == "/api/convert" or request.url.path.startswith("/send/")
        )
        if heavy and _too_many_requests(request.client.host if request.client else "unknown"):
            return JSONResponse(
                status_code=429,
                content={
                    "error": "too_many_requests",
                    "message": "Слишком много запросов. Подождите минуту и попробуйте снова.",
                },
            )

        try:
            return await call_next(request)
        except MediaHubError as exc:  # страховка для ошибок, поднятых в middleware
            return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    # --- жизненный цикл ---------------------------------------------------
    @app.on_event("startup")
    async def _startup() -> None:
        cfg.ensure_dirs()
        gc.start()
        jobs_cleanup_task = asyncio.create_task(_registry_cleanup_loop())
        app.state.registry_cleanup = jobs_cleanup_task
        report = media_service.availability(cfg)
        logger.info("Хранилище: %s", cfg.storage_dir)
        logger.info("Локальный адрес для QR-ссылок: %s", network.base_url)
        if report["available"]:
            logger.info("FFmpeg: %s", report["version"])
        else:
            logger.warning("FFmpeg не найден – медиа-функции вернут 503.")

    async def _registry_cleanup_loop() -> None:
        """Периодически чистит реестр задач, чтобы он не рос бесконечно."""
        while True:
            try:
                await asyncio.sleep(1800)
                removed = jobs.cleanup_registry(max_age_seconds=cfg.task_ttl_seconds * 2)
                if removed:
                    logger.info("Из реестра задач удалено записей: %d", removed)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 – служебный цикл не должен падать
                logger.exception("Ошибка очистки реестра задач")

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        cleanup_task = getattr(app.state, "registry_cleanup", None)
        if cleanup_task is not None:
            cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cleanup_task
        await gc.stop()
        jobs.shutdown()

    # --- служебные маршруты ----------------------------------------------
    @app.get("/api/health", tags=["service"])
    async def health() -> dict:
        report = media_service.availability(cfg)
        return {
            "status": "ok",
            "version": __version__,
            "storage": store.disk_usage(),
            "network": network.as_dict(),
            "ffmpeg": report,
            "limits": {
                "max_files": cfg.max_files_per_request,
                "max_request_bytes": cfg.max_request_bytes,
                "max_pdf_merge_files": cfg.max_pdf_merge_files,
            },
            "ttl": {
                "task_ttl_seconds": cfg.task_ttl_seconds,
                "gc_interval_seconds": cfg.gc_interval_seconds,
                "burn_after_download": cfg.burn_after_download,
                "burn_delay_seconds": cfg.burn_after_download_seconds,
            },
        }

    @app.get("/api/network", tags=["service"])
    async def network_info(refresh: bool = Query(False)) -> dict:
        if refresh:
            network.refresh()
        return network.as_dict()

    # --- основной сценарий ------------------------------------------------
    @app.post("/api/convert", tags=["conversion"], status_code=202)
    async def convert(request: Request) -> dict:
        _check_token(request, cfg)
        form = await request.form()
        uploads: list[UploadFile] = []
        for key, value in form.multi_items():
            if key in _SERVICE_FIELDS or key in _OPTION_FIELDS:
                continue
            if hasattr(value, "filename") and hasattr(value, "read"):
                uploads.append(value)  # type: ignore[arg-type]

        if not uploads:
            raise ValidationError(
                "Файлы не переданы. Прикрепите их в поле с любым именем (например, files)."
            )
        file_limit = _form_int(form.get("max_files"), cfg.max_files_per_request)
        file_limit = max(1, min(file_limit, cfg.max_files_per_request))
        if len(uploads) > file_limit:
            raise PayloadTooLargeError(
                f"Слишком много файлов: {len(uploads)}. Лимит – {file_limit}.",
                details={"count": len(uploads), "limit": file_limit},
            )
        total_limit = _form_int(form.get("max_bytes"), cfg.max_request_bytes)
        total_limit = max(1024, min(total_limit, cfg.max_request_bytes))

        kind_raw = str(form.get("kind") or "")
        action_raw = str(form.get("action") or "")
        options = _collect_options(form)

        task = store.create_task()
        saved: list[Path] = []
        total_bytes = 0
        used_names: set[str] = set()
        try:
            for index, upload in enumerate(uploads, start=1):
                original = upload.filename or f"file_{index}"
                safe = sanitize_filename(original, fallback_stem=f"file_{index}")
                # Индекс добавляется только при совпадении имён: он нужен лишь для
                # уникальности внутри сессии, а не для красоты.
                if safe in used_names:
                    safe = f"{index:03d}_{safe}"
                used_names.add(safe)
                destination = task.raw / safe
                with destination.open("wb") as handle:
                    while True:
                        chunk = await upload.read(UPLOAD_CHUNK)
                        if not chunk:
                            break
                        total_bytes += len(chunk)
                        if total_bytes > total_limit:
                            raise PayloadTooLargeError(
                                "Превышен допустимый вес запроса.",
                                details={
                                    "limit_bytes": total_limit,
                                    "received_bytes": total_bytes,
                                },
                            )
                        handle.write(chunk)
                if destination.stat().st_size == 0:
                    raise ValidationError(
                        f"Файл «{original}» пуст.", details={"filename": original}
                    )
                saved.append(destination)
                await upload.close()

            spec = pipeline.build_spec(kind_raw, action_raw, saved, options, out_dir=task.out)
            # Проверка содержимого до постановки в очередь: подделки отсекаются здесь.
            await run_in_threadpool(pipeline.sniff, spec, cfg)

            job = jobs.create(
                task,
                kind=spec.kind,
                action=spec.action,
                options=spec.options,
                filenames=[path.name for path in saved],
            )
            jobs.submit(job, spec)
        except MediaHubError:
            store.delete_task(task.task_id)
            raise
        except Exception:
            store.delete_task(task.task_id)
            raise

        store.touch(task.task_id)
        return {
            "job_id": job.job_id,
            "task_id": task.task_id,
            "status": job.status,
            "kind": job.kind,
            "action": job.action,
            "received_files": len(saved),
            "received_bytes": total_bytes,
            "status_url": f"/api/jobs/{job.job_id}",
            "download_url": f"/download/{task.task_id}",
            "qr_url": f"/qr/{task.task_id}",
        }

    @app.get("/api/jobs/{job_id}", tags=["conversion"])
    async def job_status(job_id: str) -> dict:
        job = jobs.get(job_id)
        payload = job.as_dict()
        payload["status_url"] = f"/api/jobs/{job.job_id}"
        payload["download_url"] = f"/download/{job.task_id}"
        payload["qr_url"] = f"/qr/{job.task_id}"
        payload["qr_target"] = network.download_url(job.task_id)
        if job.status == STATUS_DONE and job.result is not None:
            payload["download_url"] += f"?file={job.result.download_name}"
        return payload

    @app.delete("/api/jobs/{job_id}", tags=["conversion"])
    async def cancel_job(job_id: str) -> dict:
        job = jobs.cancel(job_id)
        return {"job_id": job.job_id, "status": job.status, "stage": job.stage}

    @app.delete("/api/tasks/{task_id}", tags=["conversion"])
    async def delete_task(task_id: str) -> dict:
        task = store.get_task(task_id)
        gc.cancel_burn(task_id)
        removed = store.delete_task(task.task_id)
        jobs.forget_task(task.task_id)
        return {"task_id": task.task_id, "deleted": removed}

    # --- выдача результата ------------------------------------------------
    def _file_response(target: Path, task, job, *, inline: bool) -> Response:
        """Готовит ответ с файлом и обновляет жизненный цикл сессии."""
        # Тип определяется по конкретному файлу; тип набора применяется только
        # когда отдаётся именно результат задачи (ZIP или единственный файл).
        content_type = pipeline._guess_content_type(target)  # noqa: SLF001 – общий справочник
        if target.suffix.lower() == ".zip":
            content_type = "application/zip"
        elif job and job.result and target.name == job.result.download_name:
            if job.result.content_type != "application/octet-stream":
                content_type = job.result.content_type
        response = FileResponse(
            target,
            media_type=content_type,
            filename=target.name,
            content_disposition_type="inline" if inline else "attachment",
            headers={"Cache-Control": "no-store"},
        )
        if cfg.burn_after_download:
            # ТЗ 2.2: после выдачи файла сессия уничтожается с задержкой.
            gc.schedule_burn(task.task_id)
        else:
            store.touch(task.task_id)
        return response

    @app.get("/download/{task_id}", tags=["download"])
    async def download(
        task_id: str,
        request: Request,
        inline: bool = Query(False),
    ) -> Response:
        """Отдаёт готовый результат: сам файл или ZIP-архив."""
        _check_token(request, cfg)
        task = store.get_task(task_id)

        job = jobs.find_by_task(task.task_id)
        if job is not None and job.status in {STATUS_QUEUED, STATUS_PROCESSING}:
            # Результат ещё не готов: это не «удалено», а «подождите».
            raise ConflictError(
                f"Результат ещё обрабатывается (готово {job.progress:.0f} %). "
                "Повторите запрос после завершения задачи.",
                details={
                    "job_id": job.job_id,
                    "status": job.status,
                    "progress": round(job.progress, 1),
                    "status_url": f"/api/jobs/{job.job_id}",
                },
            )
        if job is not None and job.status == STATUS_ERROR:
            raise ConflictError(
                "Обработка завершилась ошибкой, результат недоступен.",
                details={"job_id": job.job_id, "error": (job.error or {}).get("message")},
            )

        bundle_path = job.result.bundle_path if job and job.result else None
        files = job.result.files if job and job.result else sorted(
            path for path in task.out.rglob("*") if path.is_file()
        )
        target = _resolve_download(task.raw, task.out, None, bundle_path, files)
        return _file_response(target, task, job, inline=inline)

    @app.get("/download/{task_id}/{name:path}", tags=["download"])
    async def download_named(
        task_id: str,
        name: str,
        request: Request,
        inline: bool = Query(False),
    ) -> Response:
        """Отдаёт конкретный файл из сессии по имени (вложенные пути отклоняются)."""
        _check_token(request, cfg)
        task = store.get_task(task_id)

        job = jobs.find_by_task(task.task_id)
        if job is not None and job.status in {STATUS_QUEUED, STATUS_PROCESSING}:
            raise ConflictError(
                f"Результат ещё обрабатывается (готово {job.progress:.0f} %).",
                details={"job_id": job.job_id, "status": job.status,
                         "progress": round(job.progress, 1)},
            )

        files = job.result.files if job and job.result else sorted(
            path for path in task.out.rglob("*") if path.is_file()
        )
        target = _resolve_download(task.raw, task.out, name, None, files)
        return _file_response(target, task, job, inline=inline)

    @app.get("/qr/{task_id}", tags=["download"])
    async def qr_code(
        task_id: str,
        request: Request,
        file: str | None = Query(None),
        box_size: int = Query(12, ge=4, le=24),
    ) -> Response:
        task = store.get_task(task_id)
        _check_token(request, cfg)
        url = network.download_url(task.task_id, filename=file)
        png = await run_in_threadpool(network.qr_png, url, box_size=box_size)
        # Адрес ссылки клиент получает в /api/jobs/{id} (поле qr_target):
        # HTTP-заголовки ограничены ASCII, а имя файла может быть кириллическим.
        return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})

    # --- минимальная страница для ручной проверки -------------------------
    static_dir = PROJECT_ROOT / "web"
    index = static_dir / "index.html"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # --- мост «телефон → компьютер» ---------------------------------------
    @app.post("/api/send", tags=["bridge"])
    async def start_send() -> dict:
        """Создаёт сессию приёма и отдаёт ссылку для телефона."""
        paths = store.create_task()
        return {
            "task_id": paths.task_id,
            "url": f"{network.base_url}/send/{paths.task_id}",
        }

    @app.get("/send/{task_id}", include_in_schema=False)
    async def send_page(task_id: str) -> HTMLResponse:
        store.get_task(task_id)
        return HTMLResponse(SEND_PAGE.replace("__TASK__", task_id))

    @app.post("/send/{task_id}", tags=["bridge"])
    async def receive_files(task_id: str, files: list[UploadFile] = File(...)) -> dict:
        paths = store.get_task(task_id)
        saved: list[str] = []
        for upload in files:
            name = sanitize_filename(upload.filename or "file")
            data = await upload.read()
            if not data:
                continue
            (paths.raw / name).write_bytes(data)
            saved.append(name)
        if not saved:
            raise ValidationError("Файлы не получены.")
        logger.info("С телефона принято файлов: %s", len(saved))
        return {"count": len(saved), "names": saved}

    @app.get("/qr/send/{task_id}", tags=["bridge"])
    async def qr_send(task_id: str) -> Response:
        paths = store.get_task(task_id)
        url = f"{network.base_url}/send/{paths.task_id}"
        png = await run_in_threadpool(network.qr_png, url, box_size=8)
        return Response(png, media_type="image/png")

    @app.get("/api/inbox/{task_id}", tags=["bridge"])
    async def inbox(task_id: str) -> dict:
        paths = store.get_task(task_id)
        files = sorted(item.name for item in paths.raw.iterdir() if item.is_file())
        return {"task_id": task_id, "files": files}

    @app.get("/api/inbox/{task_id}/{name:path}", tags=["bridge"])
    async def inbox_file(task_id: str, name: str) -> FileResponse:
        paths = store.get_task(task_id)
        base = paths.raw.resolve()
        target = (paths.raw / name).resolve()
        if base not in target.parents or not target.is_file():
            raise NotFoundError("Файл не найден.")
        return FileResponse(target, filename=target.name)

    @app.get("/", include_in_schema=False)
    async def root() -> Response:
        if index.is_file():
            return HTMLResponse(index.read_text(encoding="utf-8"))
        return JSONResponse(
            {
                "service": "MediaHub",
                "message": "Интерфейс не найден. Используйте REST API.",
                "docs": "/api/docs",
            }
        )

    return app
