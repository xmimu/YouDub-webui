"""Independent single-thread worker for Bilibili submissions."""

from __future__ import annotations

import logging
import queue
import threading
from pathlib import Path

from . import config, database
from .adapters import biliup
from .adapters.summary import read_bilibili_publish_metadata
from .youtube import is_local_upload_url


_queue: "queue.Queue[str]" = queue.Queue()
_thread: threading.Thread | None = None
_lock = threading.Lock()
logger = logging.getLogger(__name__)


def _inside_workfolder(path: Path) -> bool:
    try:
        path.resolve().relative_to(config.WORKFOLDER.resolve())
    except ValueError:
        return False
    return True


def _default_tid() -> int:
    raw = database.get_bilibili_settings()["default_tid"].strip()
    if not raw.isdigit() or not 1 <= int(raw) <= 65535:
        raise ValueError("Configure a valid default Bilibili category ID before uploading.")
    return int(raw)


def validate_ready() -> None:
    if not biliup.validate_cookie_file(config.BILIBILI_COOKIE_PATH):
        raise ValueError("Log in to Bilibili before enabling automatic upload.")
    _default_tid()


def _validated_cover_path(value: str | None) -> Path | None:
    if not value or not str(value).strip():
        return None
    cover = Path(str(value).strip())
    if not cover.is_file() or not _inside_workfolder(cover):
        raise ValueError("The generated Bilibili cover is not available in the configured workfolder.")
    return cover


def create_for_task(task_id: str) -> dict:
    task = database.get_task(task_id)
    if not task:
        raise ValueError("Task not found.")
    if is_local_upload_url(task["url"]):
        raise ValueError("Local video tasks cannot be uploaded to Bilibili.")
    if task["status"] != "succeeded":
        raise ValueError("Only succeeded tasks can be uploaded to Bilibili.")
    final_path = Path(task.get("final_video_path") or "")
    session_path = Path(task.get("session_path") or "")
    if not final_path.is_file() or not _inside_workfolder(final_path):
        raise ValueError("The final video is not available in the configured workfolder.")
    if not session_path.is_dir() or not _inside_workfolder(session_path):
        raise ValueError("The task metadata is not available in the configured workfolder.")
    validate_ready()
    metadata = read_bilibili_publish_metadata(session_path)
    if not metadata:
        raise ValueError("Bilibili publishing metadata is not available.")
    title = str(metadata.get("title") or "").strip()
    description = str(metadata.get("description") or "").strip()
    tags = [str(tag).strip() for tag in metadata.get("tags") or [] if str(tag).strip()]
    if not title:
        raise ValueError("Bilibili publishing title is empty.")
    cover_path = _validated_cover_path(metadata.get("cover_path"))
    job_id = database.create_bilibili_upload_job(
        task_id,
        title=title[:80],
        description=description[:2000],
        tags=tags[:10],
        tid=_default_tid(),
        source_url=task["url"],
        cover_path=str(cover_path) if cover_path else None,
    )
    _queue.put(job_id)
    job = database.get_bilibili_upload_job(job_id)
    if not job:
        raise RuntimeError("Bilibili upload job was not persisted.")
    return job


def enqueue_auto_for_task(task_id: str) -> None:
    task = database.get_task(task_id)
    if not task or task["status"] != "succeeded" or not task.get("auto_upload_bilibili"):
        return
    try:
        create_for_task(task_id)
    except Exception:  # noqa: BLE001
        logger.exception("Unable to enqueue automatic Bilibili upload for task %s", task_id)


def _run_job(job_id: str) -> None:
    job = database.get_bilibili_upload_job(job_id)
    if not job or job["status"] != "queued":
        return
    task = database.get_task(job["task_id"])
    if not task:
        database.update_bilibili_upload_job(
            job_id,
            status="failed",
            error_message="Task not found.",
            completed_at=database.now_iso(),
        )
        return
    try:
        final_path = Path(task.get("final_video_path") or "")
        if not final_path.is_file() or not _inside_workfolder(final_path):
            raise ValueError("The final video is not available in the configured workfolder.")
        if not biliup.validate_cookie_file(config.BILIBILI_COOKIE_PATH):
            raise ValueError("Bilibili login credentials are missing or invalid.")
        cover_path = _validated_cover_path(job.get("cover_path"))
    except Exception as exc:  # noqa: BLE001
        database.update_bilibili_upload_job(
            job_id,
            status="failed",
            error_message=str(exc).strip() or type(exc).__name__,
            completed_at=database.now_iso(),
        )
        return
    database.update_bilibili_upload_job(
        job_id,
        status="running",
        started_at=database.now_iso(),
        error_message="",
    )
    try:
        biliup.upload_video(
            cookie_path=config.BILIBILI_COOKIE_PATH,
            video_path=final_path,
            title=job["title"],
            description=job["description"],
            tags=job["tags"],
            tid=int(job["tid"]),
            source_url=job["source_url"],
            cover_path=cover_path,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Bilibili upload job %s failed", job_id)
        database.update_bilibili_upload_job(
            job_id,
            status="unknown",
            error_message=(
                f"Upload outcome is unknown; verify Bilibili before retrying. "
                f"{str(exc).strip() or type(exc).__name__}"
            ),
            completed_at=database.now_iso(),
        )
        return
    database.update_bilibili_upload_job(
        job_id,
        status="succeeded",
        result_message="Submitted to Bilibili.",
        completed_at=database.now_iso(),
    )


def _loop() -> None:
    while True:
        job_id = _queue.get()
        try:
            _run_job(job_id)
        except Exception:  # noqa: BLE001
            logger.exception("Unhandled Bilibili upload worker error for %s", job_id)
        finally:
            _queue.task_done()


def start() -> None:
    global _thread
    with _lock:
        if _thread is not None:
            return
        database.fail_stale_bilibili_upload_jobs()
        _thread = threading.Thread(target=_loop, daemon=True)
        _thread.start()
