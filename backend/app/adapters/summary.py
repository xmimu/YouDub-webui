from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .. import config, runtime_security

logger = logging.getLogger(__name__)

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# Bilibili's web cover upload endpoint uses a 16:10 canvas.
COVER_WIDTH = 1600
COVER_HEIGHT = 1000


def _load_settings() -> dict[str, str]:
    env_file = _repo_root() / ".env"
    values: dict[str, str] = {}
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _llm_client():
    settings = _load_settings()
    api_key = os.getenv("OPENAI_API_KEY") or settings.get("OPENAI_API_KEY", "")
    base_url = os.getenv("OPENAI_BASE_URL") or settings.get(
        "OPENAI_BASE_URL", "https://api.deepseek.com/v1"
    )
    model = os.getenv("OPENAI_MODEL") or settings.get("OPENAI_MODEL", "deepseek-chat")
    if not api_key:
        return None, ""
    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=base_url.rstrip("/"))
    return client, model


def _chat(client, model: str, system: str, user: str) -> str:
    if client is None or not model:
        return ""
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.3,
        )
        return (response.choices[0].message.content or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _translate_title(title: str) -> str:
    client, model = _llm_client()
    if client is None:
        return title
    translated = _chat(
        client,
        model,
        (
            "你是视频标题翻译助手。将视频标题翻译成简体中文，"
            "保留专有名词与术语原文（如 C++、GPU、Transformer 等），"
            "只输出译文本身，不要解释、不要引号、不要加前缀。"
        ),
        title,
    )
    translated = re.sub(r"^[「『\"']+|[」』\"']+$", "", translated)
    if translated and _CJK_RE.search(translated):
        return translated
    return title


def _translate_tags(tags: list[str]) -> list[str]:
    if not tags:
        return []
    client, model = _llm_client()
    if client is None:
        return tags
    text = "\n".join(f"- {t}" for t in tags)
    translated = _chat(
        client,
        model,
        (
            "你是标签翻译助手。将下列英文视频标签逐条翻译成简体中文。\n"
            "规则：\n"
            "- 每行一个标签，输出为 `- 中文` 格式，与原顺序一致。\n"
            "- 技术术语、品牌、缩写、专有名词保留原文（如 C++、GPU、Transformer、Claude Code、YouTube）。\n"
            "- 不增加、不删除标签条数；只输出标签列表，不要其他文字。"
        ),
        text,
    )
    if not translated:
        return tags
    lines = [ln.strip().lstrip("-").strip() for ln in translated.splitlines()]
    result = [ln for ln in lines if ln]
    return result if len(result) == len(tags) else tags


def _content_description(
    title: str,
    uploader: str,
    description: str,
    tags: list[str],
) -> str:
    client, model = _llm_client()
    if client is None:
        return _strip_ads(description)
    prompt = (
        f"视频标题：{title}\n"
        f"作者：{uploader}\n"
        f"视频标签：{'、'.join(tags)}\n"
        f"原始简介：\n{description[:6000]}"
    )
    cleaned = _chat(
        client,
        model,
        (
            "你是视频简介整理助手。根据视频的标题、标签和原始简介，"
            "用简体中文写一段 2-4 句的视频内容简介，概括这个视频讲什么。\n"
            "规则：\n"
            "- 只描述视频本身的内容，不包含任何赞助、推广、社交账号、商品链接等信息。\n"
            "- 不要复述原始简介里的网址、邮箱、赞助方名字。\n"
            "- 直接输出简介正文，不要标题、不要列表。"
        ),
        prompt,
    )
    if cleaned and _CJK_RE.search(cleaned):
        return cleaned
    return _strip_ads(description)


def _strip_ads(description: str) -> str:
    lines = description.splitlines()
    kept: list[str] = []
    for line in lines:
        if re.search(
            r"https?://|www\.|@\w+\.|utm_|sponsor|Patreon|Discord|Instagram|Twitter",
            line,
            re.IGNORECASE,
        ):
            continue
        kept.append(line)
    text = "\n".join(kept).strip()
    return re.sub(r"\n{3,}", "\n\n", text)


def _safe_filename(text: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", text).strip()
    return cleaned[:120] or "untitled"


def _format_publish_date(value: str) -> str:
    """将 yt-dlp 的 upload_date（YYYYMMDD）格式化为 YYYY-MM-DD，无效时返回空字符串。"""
    digits = value.strip()
    if len(digits) == 8 and digits.isdigit():
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    return ""


def _publish_metadata(info: dict[str, Any]) -> dict[str, Any]:
    original_title = str(info.get("title") or info.get("fulltitle") or "未命名视频")
    uploader = str(info.get("uploader") or info.get("channel") or "未知作者")
    desc = str(info.get("description") or "").strip()
    tags = [str(tag).strip() for tag in (info.get("tags") or info.get("categories") or []) if str(tag).strip()]
    url = str(info.get("webpage_url") or info.get("original_url") or "")
    translated_title = _translate_title(original_title)
    title = f"【AI译制】{translated_title}"
    if len(title) > 80:
        title = title[:77] + "..."
    translated_tags = _translate_tags(tags)
    publish_tags = list(dict.fromkeys([*translated_tags, "AI译制", "中文配音"]))[:10]
    content_description = _content_description(original_title, uploader, desc, tags)
    published_at = _format_publish_date(str(info.get("upload_date") or ""))
    source_meta = f"原作者：{uploader}"
    if published_at:
        source_meta += f"\n原视频发布时间：{published_at}"
    source_meta += f"\n原视频：{url}"
    description = (
        f"本视频由 YouDub-webui 自动 AI 译制。\n\n{content_description}\n\n{source_meta}"
    ).strip()[:2000]
    return {
        "title": title,
        "description": description,
        "tags": publish_tags,
        "source_url": url,
        "original_title": original_title,
        "uploader": uploader,
    }


def _markdown(info: dict[str, Any], publish: dict[str, Any]) -> str:
    title = str(publish["title"]).removeprefix("【AI译制】")
    original_title = str(publish["original_title"])
    uploader = str(publish["uploader"])
    url = str(publish["source_url"])
    md_tags = list(publish["tags"])
    md_desc = str(publish["description"]).split("\n\n原作者：", 1)[0]
    dur = str(info.get("duration_string") or "")
    date = str(info.get("upload_date") or "")

    return f"""# 【AI译制】{title}

> 本视频由 YouDub-webui 自动 AI 译制（DeepSeek 翻译 + VoxCPM 配音）。

## 视频信息

- **标题**：{title}
- **原标题**：{original_title}
- **作者**：{uploader}
- **时长**：{dur}
- **发布日期**：{date}
- **原视频链接**：{url}

## 简介

{md_desc}

## 相关标签

{''.join(f'- {t}\n' for t in md_tags)}
"""


def generate_summary_md(session: Path, info: dict[str, Any]) -> Path | None:
    """基于视频元数据生成【AI译制】简介 md，使用安全写入。"""
    original_title = str(info.get("title") or info.get("fulltitle") or "").strip()
    if not original_title:
        return None
    publish = _publish_metadata(info)
    title = str(publish["title"]).removeprefix("【AI译制】")
    content = _markdown(info, publish)
    out = session / f"{_safe_filename(title)}.md"
    runtime_security.atomic_write_private_text(out, content)
    cover = generate_cover(session, info, publish)
    if cover:
        publish["cover_path"] = str(cover)
    metadata_path = session / "metadata" / "bilibili_publish.json"
    runtime_security.atomic_write_private_text(
        metadata_path,
        json.dumps(publish, ensure_ascii=False, indent=2),
    )
    return out


def _probe_duration(path: Path) -> float | None:
    command = [
        config.ffprobe_binary(),
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=nw=1:nk=1",
        str(path),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        return float(result.stdout.strip())
    except (ValueError, subprocess.SubprocessError, OSError):
        return None


def _extract_frame(session: Path, info: dict[str, Any]) -> Path | None:
    video_source = session / "media" / "video_source.mp4"
    if not video_source.is_file():
        return None
    duration = _probe_duration(video_source)
    timestamp = max(1.0, duration / 2) if duration and duration > 0 else 1.0
    frame_path = session / "metadata" / "cover_frame.jpg"
    command = [
        config.ffmpeg_binary(),
        "-y",
        "-ss",
        f"{timestamp:.2f}",
        "-i",
        str(video_source),
        "-frames:v",
        "1",
        "-q:v",
        "3",
        str(frame_path),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0 or not frame_path.is_file():
        return None
    return frame_path


def _crop_to_cover(img: Any) -> Any:
    from PIL import Image

    target_w, target_h = COVER_WIDTH, COVER_HEIGHT
    width, height = img.size
    scale = max(target_w / width, target_h / height)
    img = img.resize((round(width * scale), round(height * scale)), Image.LANCZOS)
    width, height = img.size
    left = (width - target_w) // 2
    top = (height - target_h) // 2
    return img.crop((left, top, left + target_w, top + target_h))


def _find_cjk_font() -> str | None:
    candidates = (
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    )
    for path in candidates:
        if Path(path).is_file():
            return path
    return None


def _wrap_title(text: str, font: Any, max_width: int, max_lines: int) -> list[str]:
    from PIL import Image, ImageDraw

    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    lines: list[str] = []
    current = ""
    for char in text:
        candidate = current + char
        box = draw.textbbox((0, 0), candidate, font=font)
        if box[2] - box[0] > max_width and current:
            lines.append(current)
            current = char
            if len(lines) >= max_lines:
                break
        else:
            current = candidate
    if current and len(lines) < max_lines:
        lines.append(current)
    return lines[:max_lines]


def _compose_cover(frame_path: Path, title: str, out_path: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont

    with Image.open(frame_path) as source:
        image = _crop_to_cover(source.convert("RGB"))
    overlay = Image.new("RGBA", (COVER_WIDTH, COVER_HEIGHT), (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    for row in range(COVER_HEIGHT):
        ratio = row / COVER_HEIGHT
        if ratio < 0.35:
            continue
        alpha = int(190 * ((ratio - 0.35) / 0.65))
        overlay_draw.line([(0, row), (COVER_WIDTH, row)], fill=(0, 0, 0, min(alpha, 190)))
    image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(image)

    font_path = _find_cjk_font()
    if font_path:
        max_width = COVER_WIDTH - 120
        max_lines = 3
        font_size = 92
        font = None
        lines: list[str] = []
        while font_size >= 48:
            font = ImageFont.truetype(font_path, font_size)
            lines = _wrap_title(title, font, max_width, max_lines)
            if len(lines) <= max_lines:
                break
            font_size -= 8
        line_height = font_size + 16
        block_height = len(lines) * line_height
        y_start = COVER_HEIGHT - 80 - block_height
        for index, line in enumerate(lines):
            box = draw.textbbox((0, 0), line, font=font)
            x = (COVER_WIDTH - (box[2] - box[0])) // 2
            draw.text((x, y_start + index * line_height), line, font=font, fill=(255, 255, 255, 255))

    image.save(out_path, "JPEG", quality=85)


def generate_cover(session: Path, info: dict[str, Any], publish: dict[str, Any]) -> Path | None:
    """生成投稿中文封面；任何一步失败都返回 None 以便回退到无封面。"""
    frame = _extract_frame(session, info)
    if frame is None:
        return None
    title = str(publish.get("title") or "").strip()
    if title.startswith("【AI译制】"):
        title = title[len("【AI译制】") :].strip()
    if not title:
        return None
    out_path = session / "metadata" / "cover.jpg"
    runtime_security.ensure_private_directory(out_path.parent)
    try:
        _compose_cover(frame, title, out_path)
    except Exception:  # noqa: BLE001
        logger.warning("Failed to generate Bilibili cover for %s", session, exc_info=True)
        return None
    if not out_path.is_file():
        return None
    return out_path


def read_bilibili_publish_metadata(session: Path) -> dict[str, Any] | None:
    path = session / "metadata" / "bilibili_publish.json"
    if not path.is_file():
        info = read_video_info(session)
        if not info or not generate_summary_md(session, info):
            return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def read_video_info(session: Path) -> dict[str, Any] | None:
    """读取视频元数据：优先 ytdlp_info.json，其次 local_info.json。"""
    candidates = (
        session / "metadata" / "ytdlp_info.json",
        session / "metadata" / "local_info.json",
    )
    for info_file in candidates:
        if info_file.exists():
            return json.loads(info_file.read_text(encoding="utf-8"))
    return None
