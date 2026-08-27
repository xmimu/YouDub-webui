from __future__ import annotations

import os
from typing import Any

import yt_dlp

from .ytdlp import DEFAULT_USER_AGENT


def _proxy_url() -> str:
    return os.getenv("HTTP_PROXY") or os.getenv("http_proxy") or ""


def _normalize_channel_url(channel_url: str) -> str:
    url = channel_url.strip()
    if not url:
        raise ValueError("Channel URL must not be empty.")
    if not url.startswith("http://") and not url.startswith("https://"):
        url = f"https://{url}"
    return url.rstrip("/") + "/videos"


def _ydl_opts(proxy: str) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "extract_flat": True,
        "playlistend": 1,
        "quiet": True,
        "no_warnings": True,
        "cachedir": False,
        "http_headers": {"User-Agent": DEFAULT_USER_AGENT},
    }
    if proxy:
        opts["proxy"] = proxy
    return opts


def resolve_channel_url(input_url: str) -> tuple[str, str]:
    """解析频道或视频链接为频道地址，返回 (频道URL, 频道名)。

    - 输入频道链接（@handle / channel/ID / c/xxx）时原样返回频道地址；
    - 输入视频链接时通过 yt-dlp 解析其所属频道。
    """
    url = input_url.strip()
    if not url:
        raise ValueError("URL must not be empty.")
    if not url.startswith("http://") and not url.startswith("https://"):
        url = f"https://{url}"

    proxy = _proxy_url()
    opts = _ydl_opts(proxy)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    channel_url = str(info.get("channel_url") or "").strip()
    channel_name = str(info.get("channel") or info.get("uploader") or "").strip()
    if channel_url:
        return channel_url, channel_name

    # 输入本身是频道链接：channel_url 为空时用输入地址
    for candidate in (info.get("webpage_url"), url):
        value = str(candidate or "").strip()
        if value.startswith("http"):
            return value, channel_name
    raise ValueError("Unable to resolve a channel from the given URL.")


def fetch_channel_info(channel_url: str) -> dict[str, str]:
    """抓取频道名称与 channel id（flat 模式，不发视频元数据请求）。"""
    url = _normalize_channel_url(channel_url)
    proxy = _proxy_url()
    opts = _ydl_opts(proxy)
    opts["playlistend"] = 1
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return {
        "channel_name": str(info.get("channel") or info.get("uploader") or "").strip(),
        "channel_id": str(info.get("channel_id") or info.get("uploader_id") or "").strip(),
    }


def fetch_channel_videos(channel_url: str, limit: int | None = None) -> list[dict[str, str]]:
    """抓取频道视频列表（flat 模式）。limit 为空时抓取全部。"""
    url = _normalize_channel_url(channel_url)
    proxy = _proxy_url()
    opts = _ydl_opts(proxy)
    if limit is not None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        opts["playlistend"] = int(limit)
    else:
        opts.pop("playlistend", None)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    videos: list[dict[str, str]] = []
    for entry in info.get("entries") or []:
        if not entry or entry.get("_type") not in (None, "url", "video"):
            continue
        video_url = entry.get("url") or ""
        if not video_url.startswith("http") and entry.get("id"):
            video_url = f"https://www.youtube.com/watch?v={entry['id']}"
        videos.append(
            {
                "id": str(entry.get("id") or ""),
                "title": str(entry.get("title") or ""),
                "url": video_url,
            }
        )
    return videos


def fetch_channel_video_urls(channel_url: str, limit: int) -> list[str]:
    """抓取频道最新视频的 watch URL 列表（flat 模式，按频道页默认最新在前）。"""
    videos = fetch_channel_videos(channel_url, limit)
    return [v["url"] for v in videos if v["url"]]
