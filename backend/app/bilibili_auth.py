from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from . import config, runtime_security
from .adapters import biliup


@dataclass
class LoginSession:
    id: str
    qr_url: str
    status: str = "pending"
    error_message: str | None = None


_sessions: dict[str, LoginSession] = {}
_lock = threading.Lock()

_ZONE_CACHE_TTL_SECONDS = 3600
_zone_cache: dict[str, Any] = {"fetched_at": 0.0, "zones": []}


def _login(session_id: str, payload: str) -> None:
    try:
        credentials = biliup.complete_qrcode(payload)
    except Exception as exc:  # noqa: BLE001
        with _lock:
            session = _sessions.get(session_id)
            if session:
                session.status = "failed"
                session.error_message = str(exc).strip() or type(exc).__name__
        return
    with _lock:
        session = _sessions.get(session_id)
        if not session or session.status != "pending":
            return
        try:
            runtime_security.atomic_write_private_text(
                config.BILIBILI_COOKIE_PATH,
                json.dumps(credentials, ensure_ascii=False, indent=2),
            )
        except Exception as exc:  # noqa: BLE001
            session.status = "failed"
            session.error_message = str(exc).strip() or type(exc).__name__
        else:
            session.status = "succeeded"


def start_login() -> dict:
    payload, qr_url = biliup.create_qrcode()
    session = LoginSession(id=str(uuid.uuid4()), qr_url=qr_url)
    with _lock:
        for existing in _sessions.values():
            if existing.status == "pending":
                existing.status = "cancelled"
        _sessions[session.id] = session
        if len(_sessions) > 20:
            removable = [key for key, value in _sessions.items() if value.status != "pending"]
            for key in removable[: len(_sessions) - 20]:
                _sessions.pop(key, None)
    threading.Thread(target=_login, args=(session.id, payload), daemon=True).start()
    return session_dict(session)


def get_login(session_id: str) -> dict | None:
    with _lock:
        session = _sessions.get(session_id)
        return session_dict(session) if session else None


def session_dict(session: LoginSession) -> dict:
    return {
        "id": session.id,
        "qr_url": session.qr_url,
        "status": session.status,
        "error_message": session.error_message,
    }


def credential_status() -> dict:
    return {"connected": biliup.validate_cookie_file(config.BILIBILI_COOKIE_PATH)}


def zones() -> list[dict[str, Any]]:
    """返回 Bilibili 分区列表，带内存缓存；拉取失败时回退缓存，仍失败则返回空。"""
    now = time.monotonic()
    with _lock:
        if now - _zone_cache["fetched_at"] < _ZONE_CACHE_TTL_SECONDS and _zone_cache["zones"]:
            return list(_zone_cache["zones"])
    fetched = biliup.list_zones(config.BILIBILI_COOKIE_PATH)
    with _lock:
        if fetched:
            _zone_cache["zones"] = fetched
            _zone_cache["fetched_at"] = now
        return list(_zone_cache["zones"])



def disconnect() -> None:
    config.ensure_runtime_dirs()
    with _lock:
        for session in _sessions.values():
            if session.status == "pending":
                session.status = "cancelled"
    runtime_path = config.BILIBILI_COOKIE_PATH
    if runtime_path.exists():
        runtime_security.remove_private_file(runtime_path, missing_ok=True)
