from __future__ import annotations

import logging
import os
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, SecretStr

from . import auth, channel_jobs, database, runtime_security, worker
from .adapters.local_subtitles import parse_srt, uploaded_subtitle_dir
from .adapters.local_video import remove_upload, uploaded_video_dir
from .adapters.openai_client import validate_openai_base_url
from .adapters.openai_translate import list_models as list_openai_models
from .config import WORKFOLDER, YOUTUBE_COOKIE_PATH, ensure_runtime_dirs
from .pipeline import run_task
from .runtime_checks import validate_runtime_device
from .sanitize import sanitize_text
from .sources import detect_source
from .stage_reset import remove_stage_artifacts
from .stages import STAGE_NAMES
from .youtube import LOCAL_UPLOAD_DIRECTIONS, is_local_upload_url, validate_video_url

ALLOWED_VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".flv", ".wmv"}
ALLOWED_SUBTITLE_SUFFIXES = {".srt"}
LOCAL_UPLOAD_CHUNK_SIZE = 1024 * 1024
MAX_LOCAL_UPLOAD_BYTES = int(os.getenv("LOCAL_UPLOAD_MAX_BYTES", str(4 * 1024 * 1024 * 1024)))
MAX_LOCAL_SUBTITLE_BYTES = int(os.getenv("LOCAL_SUBTITLE_MAX_BYTES", str(20 * 1024 * 1024)))

logger = logging.getLogger(__name__)

TaskListStatus = Literal["all", "queued", "running", "paused", "succeeded", "failed"]
TaskListExecutionMode = Literal["all", "auto", "manual"]
TaskListSort = Literal[
    "created_desc",
    "created_asc",
    "started_desc",
    "started_asc",
    "completed_desc",
    "completed_asc",
    "status_asc",
    "status_desc",
    "title_asc",
    "title_desc",
]


def mask_secret(value: str) -> str:
    if not value:
        return ""
    return "********"


class TaskCreate(BaseModel):
    url: str
    execution_mode: str = "auto"


class ContinueTaskRequest(BaseModel):
    execution_mode: str | None = None


class YouTubeCookieUpdate(BaseModel):
    content: str


class OpenAISettingsUpdate(BaseModel):
    base_url: str
    api_key: str = ""
    clear_api_key: bool = False
    model: str
    translate_concurrency: str = ""


class OpenAIModelsRequest(BaseModel):
    base_url: str = ""
    api_key: str = ""


class YtdlpSettingsUpdate(BaseModel):
    proxy_port: str = ""


class LoginRequest(BaseModel):
    password: SecretStr


def normalize_proxy_port(value: str) -> str:
    proxy_port = value.strip()
    if not proxy_port:
        return ""
    if not proxy_port.isdigit():
        raise HTTPException(status_code=422, detail="Proxy port must be numeric.")
    port = int(proxy_port)
    if port < 1 or port > 65535:
        raise HTTPException(status_code=422, detail="Proxy port must be between 1 and 65535.")
    return str(port)


def normalize_translate_concurrency(value: str) -> str:
    concurrency = value.strip()
    if not concurrency:
        return ""
    if not all("0" <= char <= "9" for char in concurrency):
        raise HTTPException(status_code=422, detail="Translate concurrency must be numeric.")
    workers = int(concurrency)
    if workers < 1 or workers > 200:
        raise HTTPException(
            status_code=422, detail="Translate concurrency must be between 1 and 200."
        )
    return concurrency


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_runtime_dirs()
    auth.validate_auth_configuration()
    database.init_db()
    database.delete_expired_auth_sessions(database.now_iso())
    database.backfill_titles_from_metadata()
    database.fail_stale_active_tasks()
    worker.start(run_task)
    channel_jobs.start()
    yield


app = FastAPI(
    title="YouDub API",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.exception_handler(RequestValidationError)
async def redact_login_validation_error(
    request: Request, exc: RequestValidationError
) -> Response:
    if request.url.path == "/api/auth/login":
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid credentials."},
            headers={"Cache-Control": "no-store"},
        )
    return await request_validation_exception_handler(request, exc)


DEFAULT_CORS_ORIGIN_REGEX = (
    r"^https?://("
    r"localhost|"
    r"127\.0\.0\.1|"
    r"\[::1\]"
    r"):3000$"
)


def cors_origins() -> list[str]:
    defaults = ["http://localhost:3000", "http://127.0.0.1:3000"]
    configured = os.getenv("CORS_ALLOW_ORIGINS", "")
    extra = [origin.strip() for origin in configured.split(",") if origin.strip()]
    if "*" in extra:
        raise RuntimeError("CORS_ALLOW_ORIGINS cannot contain '*' when credentials are enabled.")
    return [*defaults, *extra]


def cors_origin_regex() -> str:
    configured = os.getenv("CORS_ALLOW_ORIGIN_REGEX", "").strip()
    return configured or DEFAULT_CORS_ORIGIN_REGEX


_cors_origins = cors_origins()
_cors_origin_regex = cors_origin_regex()

app.add_middleware(
    auth.AuthMiddleware,
    allowed_origins=_cors_origins,
    allowed_origin_regex=_cors_origin_regex,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_origin_regex=_cors_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _clear_replaced_login_cookie(
    response: Response,
    old_token: str,
    settings: auth.AuthSettings | None = None,
) -> None:
    if not old_token:
        return
    if settings is not None:
        auth.clear_session_cookie(response, settings)
        return
    response.delete_cookie(
        key=auth.SESSION_COOKIE_NAME,
        path=auth.SESSION_COOKIE_PATH,
        httponly=True,
    )


@app.post("/api/auth/login")
def login(payload: LoginRequest, request: Request) -> JSONResponse:
    old_token = request.cookies.get(auth.SESSION_COOKIE_NAME, "")
    auth.revoke_session_token(old_token)
    client_host = request.client.host if request.client else "unknown"

    try:
        settings = auth.validate_auth_configuration()
    except auth.AuthConfigurationError:
        response = JSONResponse(
            status_code=503,
            content={"detail": "Authentication is not configured."},
            headers={"Cache-Control": "no-store"},
        )
        _clear_replaced_login_cookie(response, old_token)
        return response

    rate_limit = auth.reserve_login_attempt(client_host)
    if not rate_limit.allowed:
        response = JSONResponse(
            status_code=429,
            content={"detail": "Too many login attempts."},
            headers={
                "Cache-Control": "no-store",
                "Retry-After": str(rate_limit.retry_after_seconds),
            },
        )
        _clear_replaced_login_cookie(response, old_token, settings)
        return response

    try:
        password_matches = auth.verify_password(
            payload.password.get_secret_value(), settings
        )
    except auth.AuthConfigurationError:
        auth.clear_login_attempts(client_host)
        response = JSONResponse(
            status_code=503,
            content={"detail": "Authentication is not configured."},
            headers={"Cache-Control": "no-store"},
        )
        _clear_replaced_login_cookie(response, old_token, settings)
        return response

    if not password_matches:
        response = JSONResponse(
            status_code=401,
            content={"detail": "Invalid credentials."},
            headers={"Cache-Control": "no-store"},
        )
        _clear_replaced_login_cookie(response, old_token, settings)
        return response

    auth.clear_login_attempts(client_host)
    token, session = auth.create_session(settings)
    response = JSONResponse(
        content={
            "authenticated": True,
            "csrf_token": session.csrf_token,
            "expires_at": session.expires_at,
        },
        headers={"Cache-Control": "no-store"},
    )
    auth.set_session_cookie(response, token, settings, session.expires_at)
    return response


@app.get("/api/auth/session")
def auth_session(request: Request) -> JSONResponse:
    session: auth.AuthenticatedSession = request.state.auth_session
    return JSONResponse(
        content={
            "authenticated": True,
            "csrf_token": session.csrf_token,
            "expires_at": session.expires_at,
        },
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/auth/logout", status_code=204)
def logout(request: Request) -> Response:
    session: auth.AuthenticatedSession = request.state.auth_session
    settings: auth.AuthSettings = request.state.auth_settings
    database.delete_auth_session(session.token_hash)
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    auth.clear_session_cookie(response, settings)
    return response


def _ensure_runtime_ready() -> None:
    try:
        validate_runtime_device()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def normalize_execution_mode(value: str) -> str:
    try:
        return database.normalize_execution_mode(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/tasks", status_code=201)
def create_task(payload: TaskCreate) -> dict:
    try:
        validated_url = validate_video_url(payload.url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    existing_id = database.find_task_by_video_id(validated_url.video_id)
    if existing_id:
        return database.get_task(existing_id)

    _ensure_runtime_ready()
    task_id = database.create_task(
        validated_url.url,
        task_id=validated_url.video_id,
        execution_mode=normalize_execution_mode(payload.execution_mode),
    )
    worker.enqueue(task_id)
    return database.get_task(task_id)


def _clean_upload_filename(filename: str | None) -> str:
    original = Path(filename or "").name.strip()
    if not original:
        raise HTTPException(status_code=422, detail="Video filename is required.")
    suffix = Path(original).suffix.lower()
    if suffix not in ALLOWED_VIDEO_SUFFIXES:
        raise HTTPException(status_code=422, detail="Unsupported video file type.")
    safe_stem = sanitize_text(Path(original).stem) or "video"
    return f"{safe_stem}{suffix}"


def _clean_subtitle_filename(filename: str | None) -> str:
    original = Path(filename or "").name.strip()
    if not original:
        raise HTTPException(status_code=422, detail="Subtitle filename is required.")
    suffix = Path(original).suffix.lower()
    if suffix not in ALLOWED_SUBTITLE_SUFFIXES:
        raise HTTPException(status_code=422, detail="Only .srt subtitle files are supported.")
    safe_stem = sanitize_text(Path(original).stem) or "subtitles"
    return f"{safe_stem}{suffix}"


def _save_uploaded_file(file: UploadFile, destination: Path, *, max_bytes: int, too_large_detail: str) -> int:
    total = 0
    created = False
    try:
        with runtime_security.open_private_binary_exclusive(destination) as handle:
            created = True
            while True:
                chunk = file.file.read(LOCAL_UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(status_code=413, detail=too_large_detail)
                handle.write(chunk)
    except Exception:
        if created:
            runtime_security.remove_private_file(destination, missing_ok=True)
        raise
    if total == 0:
        runtime_security.remove_private_file(destination, missing_ok=True)
        raise HTTPException(status_code=422, detail="Uploaded file is empty.")
    return total


def _validate_uploaded_srt(path: Path) -> None:
    try:
        parse_srt(path.read_text(encoding="utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid SRT subtitle file encoding.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid SRT subtitle file: {exc}") from exc


def _rollback_local_upload(task_id: str) -> None:
    try:
        remove_upload(WORKFOLDER, task_id)
    except Exception:
        logger.exception("Failed to remove local upload files for task %s", task_id)
    try:
        database.delete_task(task_id)
    except Exception:
        logger.exception("Failed to remove local upload database record for task %s", task_id)


@app.post("/api/tasks/upload", status_code=201)
def upload_local_video(
    direction: str = Form("en-zh"),
    file: UploadFile = File(...),
    subtitle_file: UploadFile | None = File(None),
    execution_mode: str = Form("auto"),
) -> dict:
    if direction not in LOCAL_UPLOAD_DIRECTIONS:
        raise HTTPException(status_code=422, detail="Unsupported local video direction.")

    original_name = Path(file.filename or "").name.strip()
    stored_name = _clean_upload_filename(original_name)
    stored_subtitle_name = None
    if subtitle_file is not None:
        stored_subtitle_name = _clean_subtitle_filename(subtitle_file.filename)
    normalized_execution_mode = normalize_execution_mode(execution_mode)
    _ensure_runtime_ready()

    task_id = str(uuid.uuid4())
    try:
        _save_uploaded_file(
            file,
            uploaded_video_dir(WORKFOLDER, task_id) / stored_name,
            max_bytes=MAX_LOCAL_UPLOAD_BYTES,
            too_large_detail="Uploaded video is too large.",
        )
        if subtitle_file is not None and stored_subtitle_name is not None:
            subtitle_path = uploaded_subtitle_dir(WORKFOLDER, task_id) / stored_subtitle_name
            _save_uploaded_file(
                subtitle_file,
                subtitle_path,
                max_bytes=MAX_LOCAL_SUBTITLE_BYTES,
                too_large_detail="Uploaded subtitle is too large.",
            )
            _validate_uploaded_srt(subtitle_path)

        url = f"local://upload/{task_id}?direction={direction}&filename={quote(original_name)}"
        database.create_task(
            url,
            task_id=task_id,
            execution_mode=normalized_execution_mode,
        )
        database.update_task(task_id, title=Path(original_name).stem)
        task = database.get_task(task_id)
        if task is None:
            raise RuntimeError(f"Local upload task {task_id} was not persisted.")
        worker.enqueue(task_id)
        return task
    except Exception:
        _rollback_local_upload(task_id)
        raise


@app.get("/api/tasks/current")
def current_task() -> dict | None:
    return database.get_current_task()


@app.get("/api/tasks")
def list_tasks(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    q: str = Query("", max_length=200),
    status: TaskListStatus = "all",
    execution_mode: TaskListExecutionMode = "all",
    sort: TaskListSort = "created_desc",
) -> dict:
    return database.list_tasks_page(
        page=page,
        page_size=page_size,
        query=q,
        status=status,
        execution_mode=execution_mode,
        sort=sort,
    )


@app.get("/api/tasks/{task_id}")
def task_detail(task_id: str) -> dict:
    task = database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    return task


def _is_inside_workfolder(path: Path) -> bool:
    workfolder = WORKFOLDER.resolve()
    try:
        path.resolve().relative_to(workfolder)
    except ValueError:
        return False
    return True


def _reveal_in_file_manager(directory: Path) -> None:
    """在系统文件管理器中打开目录（仅限本机场景）。"""
    import platform
    import subprocess

    system = platform.system()
    if system == "Darwin":
        subprocess.run(["open", str(directory)], check=True)
    elif system == "Windows":
        subprocess.run(["explorer", str(directory)], check=True)
    else:
        subprocess.run(["xdg-open", str(directory)], check=True)


def _purge_task(task: dict) -> None:
    session_path = task.get("session_path")
    if session_path:
        session_dir = Path(session_path)
        if session_dir.exists() and _is_inside_workfolder(session_dir):
            shutil.rmtree(session_dir)
    log_file = database.log_path(task["id"])
    if log_file.exists():
        log_file.unlink()
    database.delete_task(task["id"])


@app.delete("/api/tasks/{task_id}", status_code=204)
def delete_task(task_id: str) -> Response:
    task = database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    if task["status"] == "running":
        raise HTTPException(status_code=409, detail="Cannot delete a running task.")
    _purge_task(task)
    if is_local_upload_url(task["url"]):
        remove_upload(WORKFOLDER, task["id"])
    return Response(status_code=204)


@app.post("/api/tasks/{task_id}/rerun")
def rerun_task(task_id: str) -> dict:
    task = database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    if task["status"] == "running":
        raise HTTPException(status_code=409, detail="Cannot rerun a running task.")

    _ensure_runtime_ready()
    url = task["url"]
    execution_mode = task.get("execution_mode") or database.DEFAULT_EXECUTION_MODE
    _purge_task(task)
    new_id = database.create_task(url, task_id=task_id, execution_mode=execution_mode)
    worker.enqueue(new_id)
    return database.get_task(new_id)


@app.post("/api/tasks/{task_id}/stages/{stage_name}/redo")
def redo_stage(task_id: str, stage_name: str) -> dict:
    if stage_name not in STAGE_NAMES:
        raise HTTPException(status_code=404, detail="Stage not found.")
    task = database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    if (task.get("execution_mode") or database.DEFAULT_EXECUTION_MODE) != "manual":
        raise HTTPException(status_code=409, detail="Only manual tasks support per-stage redo.")
    if task["status"] in {"running", "queued"}:
        raise HTTPException(status_code=409, detail="Task is already running or queued.")
    stage = next((item for item in task["stages"] if item["name"] == stage_name), None)
    if not stage:
        raise HTTPException(status_code=404, detail="Stage not found.")
    if stage["status"] not in {"succeeded", "failed"}:
        raise HTTPException(status_code=409, detail="Only completed or failed stages can be redone.")
    _ensure_runtime_ready()
    session_path = task.get("session_path")
    if session_path:
        remove_stage_artifacts(Path(session_path), stage_name, detect_source(task["url"]))
    database.reset_stages_from(task_id, stage_name)
    worker.enqueue(task_id)
    return database.get_task(task_id)


@app.post("/api/tasks/{task_id}/continue")
def continue_task(task_id: str, payload: ContinueTaskRequest | None = None) -> dict:
    task = database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    if task["status"] != "paused":
        raise HTTPException(status_code=409, detail="Only paused tasks can be continued.")
    if (task.get("execution_mode") or database.DEFAULT_EXECUTION_MODE) != "manual":
        raise HTTPException(status_code=409, detail="Only manual tasks can be continued step by step.")
    if payload and payload.execution_mode is not None:
        database.update_task(task_id, execution_mode=normalize_execution_mode(payload.execution_mode))
    _ensure_runtime_ready()
    database.queue_task_for_continue(task_id)
    worker.enqueue(task_id)
    return database.get_task(task_id)


@app.post("/api/tasks/{task_id}/resume")
def resume_task(task_id: str) -> dict:
    task = database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    if task["status"] != "failed":
        raise HTTPException(status_code=409, detail="Only failed tasks can be resumed.")
    _ensure_runtime_ready()
    database.reset_failed_for_resume(task_id)
    worker.enqueue(task_id)
    return database.get_task(task_id)


@app.get("/api/tasks/{task_id}/log", response_class=PlainTextResponse)
def task_log(task_id: str) -> str:
    task = database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    path = database.log_path(task_id)
    return path.read_text(encoding="utf-8") if path.exists() else ""


@app.get("/api/tasks/{task_id}/artifact/final-video")
def final_video(task_id: str, download: bool = False) -> FileResponse:
    task = database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    final_path = task.get("final_video_path")
    if not final_path or not Path(final_path).exists():
        raise HTTPException(status_code=404, detail="Final video is not available.")
    name = Path(final_path).name
    if download:
        return FileResponse(final_path, media_type="video/mp4", filename=name)
    headers = {"Content-Disposition": f'inline; filename="{name}"'}
    return FileResponse(final_path, media_type="video/mp4", headers=headers)


@app.get("/api/tasks/{task_id}/artifact/original-video")
def original_video(task_id: str, download: bool = False) -> FileResponse:
    task = database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    session_path = task.get("session_path")
    if not session_path:
        raise HTTPException(status_code=404, detail="Original video is not available.")
    source_path = Path(session_path) / "media" / "video_source.mp4"
    if not source_path.is_file():
        raise HTTPException(status_code=404, detail="Original video is not available.")
    name = source_path.name
    if download:
        return FileResponse(source_path, media_type="video/mp4", filename=name)
    headers = {"Content-Disposition": f'inline; filename="{name}"'}
    return FileResponse(source_path, media_type="video/mp4", headers=headers)


@app.post("/api/tasks/{task_id}/open-folder", status_code=200)
def open_task_folder(task_id: str) -> dict:
    task = database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    session_path = task.get("session_path")
    if not session_path:
        raise HTTPException(status_code=404, detail="Task folder is not available.")
    session_dir = Path(session_path)
    if not session_dir.is_dir() or not _is_inside_workfolder(session_dir):
        raise HTTPException(status_code=404, detail="Task folder is not available.")
    try:
        _reveal_in_file_manager(session_dir)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Unable to open folder: {exc}") from exc
    return {"opened": True, "path": str(session_dir)}


@app.get("/api/cookies/youtube")
def get_youtube_cookie() -> dict:
    metadata = runtime_security.private_file_stat(YOUTUBE_COOKIE_PATH)
    exists = metadata is not None
    size = metadata.st_size if metadata else 0
    updated_at = metadata.st_mtime if metadata else None
    return {"exists": exists, "size": size, "updated_at": updated_at, "content": ""}


@app.post("/api/cookies/youtube")
def save_youtube_cookie(payload: YouTubeCookieUpdate) -> dict:
    content = payload.content.strip()
    if content:
        runtime_security.atomic_write_private_text(YOUTUBE_COOKIE_PATH, content + "\n")
    else:
        runtime_security.remove_private_file(YOUTUBE_COOKIE_PATH, missing_ok=True)
    return get_youtube_cookie()


@app.get("/api/settings/openai")
def get_openai_settings() -> dict:
    settings = database.get_openai_settings()
    return {
        "base_url": settings["base_url"],
        "api_key": mask_secret(settings["api_key"]),
        "has_api_key": bool(settings["api_key"]),
        "model": settings["model"],
        "translate_concurrency": settings["translate_concurrency"],
    }


@app.post("/api/settings/openai")
def save_openai_settings(payload: OpenAISettingsUpdate) -> dict:
    try:
        database.save_openai_settings(
            payload.base_url,
            payload.api_key,
            payload.model,
            normalize_translate_concurrency(payload.translate_concurrency),
            clear_api_key=payload.clear_api_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return get_openai_settings()


@app.post("/api/settings/openai/models")
def get_openai_models(payload: OpenAIModelsRequest) -> dict:
    settings = database.get_openai_settings()
    try:
        saved_base_url = validate_openai_base_url(settings["base_url"])
        requested_base_url = payload.base_url.strip()
        base_url = (
            validate_openai_base_url(requested_base_url)
            if requested_base_url
            else saved_base_url
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    requested_api_key = payload.api_key.strip()
    if requested_base_url and base_url != saved_base_url and not requested_api_key:
        raise HTTPException(
            status_code=422,
            detail="An API key is required when testing a different OpenAI base URL.",
        )
    api_key = requested_api_key or settings["api_key"]
    if not api_key:
        raise HTTPException(status_code=400, detail="OpenAI API key is not configured.")
    try:
        models = list_openai_models(base_url=base_url, api_key=api_key)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail="Failed to fetch models from the OpenAI-compatible API.",
        ) from exc
    return {"models": models}


@app.get("/api/settings/ytdlp")
def get_ytdlp_settings() -> dict:
    return database.get_ytdlp_settings()


@app.post("/api/settings/ytdlp")
def save_ytdlp_settings(payload: YtdlpSettingsUpdate) -> dict:
    database.save_ytdlp_settings(normalize_proxy_port(payload.proxy_port))
    return get_ytdlp_settings()


class ChannelFetchRequest(BaseModel):
    channels: list[str]
    limit: int = 2


class ChannelSubscribeRequest(BaseModel):
    url: str
    limit: int = 2
    group_name: str = ""


CHANNEL_LIMIT_MIN = 1
CHANNEL_LIMIT_MAX = 50
CHANNELS_MAX_COUNT = 100


@app.post("/api/channels/fetch", status_code=201)
def fetch_channels(payload: ChannelFetchRequest) -> dict:
    channels = [c.strip() for c in payload.channels if c.strip()]
    if not channels:
        raise HTTPException(status_code=422, detail="No channel URLs provided.")
    if len(channels) > CHANNELS_MAX_COUNT:
        raise HTTPException(
            status_code=422,
            detail=f"Too many channels (max {CHANNELS_MAX_COUNT}).",
        )
    limit = max(CHANNEL_LIMIT_MIN, min(payload.limit, CHANNEL_LIMIT_MAX))
    job_id = database.create_channel_job(channels, limit)
    channel_jobs.enqueue(job_id)
    return database.get_channel_job(job_id)


@app.get("/api/channels/fetch/{job_id}")
def get_channel_job(job_id: str) -> dict:
    job = database.get_channel_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Channel job not found.")
    return job


@app.get("/api/channels/subscriptions")
def list_channel_subscriptions() -> dict:
    subscriptions = database.list_channel_subscriptions()
    return {"subscriptions": subscriptions}


@app.delete("/api/channels/subscriptions/{subscription_id}", status_code=204)
def delete_channel_subscription(subscription_id: str) -> Response:
    if not database.delete_channel_subscription(subscription_id):
        raise HTTPException(status_code=404, detail="Channel subscription not found.")
    return Response(status_code=204)


@app.post("/api/channels/subscribe", status_code=201)
def subscribe_channel(payload: ChannelSubscribeRequest) -> dict:
    from .adapters.channels import resolve_channel_url

    try:
        channel_url, channel_name = resolve_channel_url(payload.url)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"Unable to resolve channel: {exc}") from exc
    subscription = database.upsert_channel_subscription(
        channel_url,
        channel_name=channel_name,
        group_name=payload.group_name.strip(),
    )
    # Kick off a background refresh to cache the channel's video list.
    refresh_id = database.create_channel_job([channel_url], 0, kind="refresh")
    channel_jobs.enqueue(refresh_id)
    return database.get_channel_subscription_by_url(channel_url)


@app.get("/api/channels/{subscription_id}/videos")
def channel_videos(
    subscription_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict:
    subscription = database.get_channel_subscription(subscription_id)
    if not subscription:
        raise HTTPException(status_code=404, detail="Channel subscription not found.")

    cached = database.list_channel_videos(subscription_id)
    enriched: list[dict] = []
    for video in cached:
        task: dict | None = None
        try:
            task_id = database.find_task_by_video_id(video["video_id"])
            if task_id:
                task = database.get_task_meta(task_id)
        except Exception:  # noqa: BLE001
            task = None
        original_exists = False
        final_exists = False
        session_path = None
        final_video_path = None
        if task:
            session_path = task.get("session_path")
            final_video_path = task.get("final_video_path")
            if session_path:
                try:
                    original_exists = Path(session_path, "media", "video_source.mp4").is_file()
                except Exception:  # noqa: BLE001
                    original_exists = False
            if final_video_path:
                try:
                    final_exists = Path(final_video_path).is_file()
                except Exception:  # noqa: BLE001
                    final_exists = False
        enriched.append(
            {
                "id": video["video_id"],
                "title": video["video_title"],
                "url": video["video_url"],
                "downloaded": task is not None,
                "task_id": task["id"] if task else None,
                "task_status": task["status"] if task else None,
                "session_path": session_path,
                "final_video_path": final_video_path,
                "has_original": original_exists,
                "has_final": final_exists,
            }
        )

    total = len(enriched)
    total_pages = max(1, (total + page_size - 1) // page_size)
    start = (page - 1) * page_size
    page_items = enriched[start : start + page_size]

    return {
        "subscription": subscription,
        "videos": page_items,
        "count": len(page_items),
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }


@app.post("/api/channels/{subscription_id}/refresh", status_code=202)
def refresh_channel(subscription_id: str) -> dict:
    subscription = database.get_channel_subscription(subscription_id)
    if not subscription:
        raise HTTPException(status_code=404, detail="Channel subscription not found.")
    refresh_id = database.create_channel_job(
        [subscription["channel_url"]], 0, kind="refresh"
    )
    channel_jobs.enqueue(refresh_id)
    return {"job_id": refresh_id, "status": "queued"}


@app.post("/api/channels/refresh-all", status_code=202)
def refresh_all_subscriptions() -> dict:
    subscriptions = database.list_channel_subscriptions()
    channels = [s["channel_url"] for s in subscriptions]
    if not channels:
        raise HTTPException(status_code=422, detail="No subscribed channels.")
    refresh_id = database.create_channel_job(channels, 0, kind="refresh")
    channel_jobs.enqueue(refresh_id)
    return {"job_id": refresh_id, "status": "queued", "total": len(channels)}


@app.post("/api/channels/process", status_code=201)
def process_all_subscriptions(payload: ChannelFetchRequest) -> dict:
    subscriptions = database.list_channel_subscriptions()
    channels = [s["channel_url"] for s in subscriptions]
    if not channels:
        raise HTTPException(status_code=422, detail="No subscribed channels.")
    limit = max(CHANNEL_LIMIT_MIN, min(payload.limit, CHANNEL_LIMIT_MAX))
    job_id = database.create_channel_job(channels, limit)
    channel_jobs.enqueue(job_id)
    return database.get_channel_job(job_id)
