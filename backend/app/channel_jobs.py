"""Single-thread FIFO worker for channel-fetch jobs.

Runs independently from the video pipeline worker so that slow TTS jobs
never block bulk channel ingestion.

Two job kinds:
- ``refresh``: fetch a channel's full video list and cache it in
  ``channel_videos`` (used on subscribe and manual refresh).
- ``process``: read from the cache and create pipeline tasks for the
  newest ``limit`` videos that have not been downloaded yet.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Callable

from . import database, worker
from .youtube import validate_video_url

_queue: "queue.Queue[str]" = queue.Queue()
_thread: threading.Thread | None = None
_lock = threading.Lock()
logger = logging.getLogger(__name__)


def enqueue(job_id: str) -> None:
    _queue.put(job_id)


def _create_task_for_video(video_url: str) -> str:
    validated = validate_video_url(video_url)
    existing_id = database.find_task_by_video_id(validated.video_id)
    if existing_id:
        return "skipped"
    task_id = database.create_task(validated.url, task_id=validated.video_id)
    worker.enqueue(task_id)
    return "created"


def _resolve_subscription_id(channel_url: str) -> str | None:
    subscription = database.get_channel_subscription_by_url(channel_url)
    return subscription["id"] if subscription else None


def _refresh_channel_cache(channel_url: str) -> tuple[int, str]:
    """抓取频道全量视频并缓存，返回 (视频数, 频道名)。"""
    from .adapters.channels import fetch_channel_info, fetch_channel_videos

    channel_info = fetch_channel_info(channel_url)
    channel_name = channel_info.get("channel_name", "")
    channel_id = channel_info.get("channel_id", "")
    subscription_id = database.upsert_channel_subscription(
        channel_url,
        channel_name=channel_name,
        channel_id=channel_id,
    )
    videos = fetch_channel_videos(channel_url, limit=None)
    database.replace_channel_videos(subscription_id, videos)
    return len(videos), channel_name


def _run_refresh_job(job_id: str) -> None:
    job = database.get_channel_job(job_id)
    if not job:
        return
    channels = job["channels"]
    total = len(channels)
    database.update_channel_job(
        job_id,
        status="running",
        total=total,
        started_at=database.now_iso(),
        error_message=None,
    )
    for index, channel_url in enumerate(channels, start=1):
        try:
            count, _name = _refresh_channel_cache(channel_url)
            logger.info("channel %s refreshed: %d videos", channel_url, count)
        except Exception as exc:  # noqa: BLE001
            logger.warning("channel %s refresh failed: %s", channel_url, exc)
        database.update_channel_job(job_id, processed=index)
    database.update_channel_job(
        job_id,
        status="succeeded",
        completed_at=database.now_iso(),
    )


def _run_process_job(job_id: str) -> None:
    job = database.get_channel_job(job_id)
    if not job:
        logger.warning("channel job %s not found", job_id)
        return
    if job["status"] != "queued":
        return

    channels = job["channels"]
    limit = job["video_limit"]
    total = len(channels)
    database.update_channel_job(
        job_id,
        status="running",
        total=total,
        started_at=database.now_iso(),
        error_message=None,
    )
    created = 0
    skipped = 0
    try:
        for index, channel_url in enumerate(channels, start=1):
            subscription_id = _resolve_subscription_id(channel_url)
            if subscription_id is None:
                logger.warning("channel %s not subscribed; skipping", channel_url)
                database.update_channel_job(job_id, processed=index)
                continue

            cached = database.list_channel_videos(subscription_id)
            if not cached:
                # Cache is empty; fall back to a one-time refresh.
                try:
                    _refresh_channel_cache(channel_url)
                    cached = database.list_channel_videos(subscription_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("channel %s cache refresh failed: %s", channel_url, exc)
                    cached = []

            for video in cached[:limit]:
                video_url = video.get("video_url") or ""
                if not video_url:
                    continue
                try:
                    outcome = _create_task_for_video(video_url)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("video %s create failed: %s", video_url, exc)
                    continue
                if outcome == "created":
                    created += 1
                else:
                    skipped += 1
            database.update_channel_job(
                job_id,
                processed=index,
                created_count=created,
                skipped_count=skipped,
            )
        database.update_channel_job(
            job_id,
            status="succeeded",
            completed_at=database.now_iso(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("channel job %s failed", job_id)
        database.update_channel_job(
            job_id,
            status="failed",
            error_message=str(exc).strip() or type(exc).__name__,
            completed_at=database.now_iso(),
        )


def _run_job(job_id: str) -> None:
    job = database.get_channel_job(job_id)
    if not job:
        logger.warning("channel job %s not found", job_id)
        return
    if job["status"] != "queued":
        return
    kind = job.get("kind") or "process"
    if kind == "refresh":
        _run_refresh_job(job_id)
    else:
        _run_process_job(job_id)


def _loop(runner: Callable[[str], None]) -> None:
    while True:
        job_id = _queue.get()
        try:
            runner(job_id)
        except Exception:  # noqa: BLE001
            logger.exception("Unhandled channel job runner exception for %s", job_id)
            try:
                database.update_channel_job(
                    job_id,
                    status="failed",
                    error_message="Unhandled runner exception",
                    completed_at=database.now_iso(),
                )
            except Exception:  # noqa: BLE001
                logger.exception("Failed to mark channel job %s as failed", job_id)
        finally:
            _queue.task_done()


def start() -> None:
    global _thread
    with _lock:
        if _thread is not None:
            return
        _thread = threading.Thread(target=_loop, args=(_run_job,), daemon=True)
        _thread.start()
