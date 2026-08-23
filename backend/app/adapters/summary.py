from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .. import runtime_security

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


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


def _markdown(info: dict[str, Any]) -> str:
    original_title = str(info.get("title") or info.get("fulltitle") or "未命名视频")
    uploader = str(info.get("uploader") or info.get("channel") or "未知作者")
    desc = str(info.get("description") or "").strip()
    tags = list(info.get("tags") or info.get("categories") or [])
    url = str(info.get("webpage_url") or info.get("original_url") or "")
    dur = str(info.get("duration_string") or "")
    date = str(info.get("upload_date") or "")

    title = _translate_title(original_title)
    md_tags = _translate_tags(tags)
    md_desc = _content_description(original_title, uploader, desc, tags)

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
    title = _translate_title(original_title)
    content = _markdown(info)
    out = session / f"{_safe_filename(title)}.md"
    runtime_security.atomic_write_private_text(out, content)
    return out


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
