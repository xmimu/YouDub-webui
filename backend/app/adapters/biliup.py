from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

from .. import runtime_security

logger = logging.getLogger(__name__)


def _stream_gears():
    try:
        import stream_gears
    except ImportError as exc:
        raise RuntimeError(
            "biliup is not installed. Install the runtime dependencies and restart YouDub."
        ) from exc
    return stream_gears


def create_qrcode() -> tuple[str, str]:
    raw = _stream_gears().get_qrcode(None)
    try:
        payload = json.loads(raw)
        url = payload["data"]["url"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("biliup returned an invalid QR code response.") from exc
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise RuntimeError("biliup returned an invalid QR code URL.")
    return raw, url


def complete_qrcode(payload: str) -> dict[str, Any]:
    credentials = _stream_gears().login_by_qrcode(payload, None)
    try:
        parsed = json.loads(credentials)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("biliup returned invalid login credentials.") from exc
    if not isinstance(parsed, dict) or not parsed.get("token_info") or not parsed.get("cookie_info"):
        raise RuntimeError("biliup returned incomplete login credentials.")
    return parsed


def list_zones(cookie_path: Path) -> list[dict[str, Any]]:
    """拉取 Bilibili 当前投稿分区列表，失败时返回空列表。"""
    if not validate_cookie_file(cookie_path):
        return []
    try:
        payload = json.loads(cookie_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    cookies = payload.get("cookie_info", {}).get("cookies", [])
    cookie_header = "; ".join(
        f"{item['name']}={item['value']}"
        for item in cookies
        if isinstance(item, dict) and item.get("name") and item.get("value")
    )
    if not cookie_header:
        return []
    request = urllib.request.Request(
        "https://member.bilibili.com/x/vupre/web/archive/pre",
        headers={
            "User-Agent": "Mozilla/5.0",
            "Cookie": cookie_header,
            "Referer": "https://member.bilibili.com/",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to fetch Bilibili zones: %s", exc)
        return []
    if raw.get("code") != 0:
        logger.warning("Bilibili zones API returned code %s", raw.get("code"))
        return []
    typelist = (raw.get("data") or {}).get("typelist") or []
    zones: list[dict[str, Any]] = []

    def walk(nodes: list[dict[str, Any]], parent: int | None = None) -> None:
        for node in nodes:
            tid = node.get("id")
            name = str(node.get("name") or "").strip()
            if tid and name:
                zones.append({"id": int(tid), "name": name, "parent": parent})
            walk(node.get("children") or [], parent=int(tid) if tid else parent)

    walk(typelist)
    return zones


def validate_cookie_file(cookie_path: Path) -> bool:
    if runtime_security.private_file_stat(cookie_path) is None:
        return False
    try:
        payload = json.loads(cookie_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and bool(payload.get("token_info")) and bool(
        payload.get("cookie_info")
    )


def upload_video(
    *,
    cookie_path: Path,
    video_path: Path,
    title: str,
    description: str,
    tags: list[str],
    tid: int,
    source_url: str,
    cover_path: Path | None = None,
) -> str:
    command = [
        sys.executable,
        "-m",
        "biliup",
        "--user-cookie",
        str(cookie_path),
        "upload",
        str(video_path),
        "--submit",
        "web",
        "--copyright",
        "2",
        "--source",
        source_url,
        "--tid",
        str(tid),
        "--title",
        title,
        "--desc",
        description,
        "--tag",
        ",".join(tags),
    ]
    if cover_path is not None:
        command.extend(["--cover", str(cover_path)])
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=max(60, int(os.getenv("BILIUP_UPLOAD_TIMEOUT_SECONDS", "86400"))),
    )
    output = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
    if completed.returncode != 0:
        detail = output[-4000:] if output else f"biliup exited with code {completed.returncode}"
        raise RuntimeError(detail)
    return output[-4000:]
