from __future__ import annotations

import json
import queue
import threading

import pytest
from PIL import Image

from backend.app import bilibili_auth, bilibili_uploads, config, database, runtime_security
from backend.app.adapters import biliup, summary


def test_biliup_qrcode_binding_receives_explicit_proxy_argument(monkeypatch):
    credentials = {
        "token_info": {"mid": 1},
        "cookie_info": {"cookies": []},
    }

    class FakeStreamGears:
        def get_qrcode(self, proxy):
            assert proxy is None
            return '{"data":{"url":"https://example.com/qr"}}'

        def login_by_qrcode(self, payload, proxy):
            assert payload
            assert proxy is None
            return json.dumps(credentials)

    monkeypatch.setattr(biliup, "_stream_gears", lambda: FakeStreamGears())

    payload, url = biliup.create_qrcode()

    assert url == "https://example.com/qr"
    assert biliup.complete_qrcode(payload) == credentials


def test_summary_writes_stable_bilibili_metadata(monkeypatch, tmp_path):
    session = tmp_path / "session"
    (session / "metadata").mkdir(parents=True)
    monkeypatch.setattr(summary, "_translate_title", lambda value: "翻译标题")
    monkeypatch.setattr(summary, "_translate_tags", lambda values: ["科技", "科技"])
    monkeypatch.setattr(summary, "_content_description", lambda *args: "视频内容简介")

    md_path = summary.generate_summary_md(
        session,
        {
            "title": "Original title",
            "uploader": "Creator",
            "tags": ["tech", "technology"],
            "webpage_url": "https://www.youtube.com/watch?v=abcdefghijk",
            "upload_date": "20230501",
        },
    )

    publish = json.loads(
        (session / "metadata" / "bilibili_publish.json").read_text(encoding="utf-8")
    )
    assert md_path == session / "翻译标题.md"
    assert publish["title"] == "【AI译制】翻译标题"
    assert publish["tags"] == ["科技", "AI译制", "中文配音"]
    assert publish["source_url"] == "https://www.youtube.com/watch?v=abcdefghijk"
    assert "原视频发布时间：2023-05-01" in publish["description"]


def test_qrcode_login_persists_credentials_without_returning_them(monkeypatch, tmp_path):
    cookie_path = tmp_path / "cookies" / "bilibili.json"
    finished = threading.Event()
    credentials = {
        "token_info": {"access_token": "secret-token"},
        "cookie_info": {"cookies": [{"name": "SESSDATA", "value": "secret-cookie"}]},
    }
    monkeypatch.setattr(config, "BILIBILI_COOKIE_PATH", cookie_path)
    monkeypatch.setattr(
        biliup,
        "create_qrcode",
        lambda: ('{"data":{"url":"https://example.com/qr"}}', "https://example.com/qr"),
    )

    def complete(_payload):
        finished.set()
        return credentials

    monkeypatch.setattr(biliup, "complete_qrcode", complete)
    monkeypatch.setattr(bilibili_auth, "_sessions", {})

    session = bilibili_auth.start_login()
    assert finished.wait(1)
    result = bilibili_auth.get_login(session["id"])

    assert result["status"] == "succeeded"
    assert "secret-token" not in str(result)
    assert "secret-cookie" not in str(result)
    assert biliup.validate_cookie_file(cookie_path)


def test_upload_job_is_deduplicated_and_can_retry_after_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.sqlite")
    workfolder = tmp_path / "workfolder"
    session = workfolder / "creator" / "video"
    final_video = session / "media" / "video_final.mp4"
    final_video.parent.mkdir(parents=True)
    final_video.write_bytes(b"video")
    metadata_dir = session / "metadata"
    metadata_dir.mkdir()
    (metadata_dir / "bilibili_publish.json").write_text(
        json.dumps(
            {
                "title": "【AI译制】标题",
                "description": "简介",
                "tags": ["AI译制"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    cookie_path = tmp_path / "cookies" / "bilibili.json"
    runtime_security.atomic_write_private_text(
        cookie_path,
        json.dumps({"token_info": {"mid": 1}, "cookie_info": {"cookies": []}}),
    )
    monkeypatch.setattr(config, "WORKFOLDER", workfolder)
    monkeypatch.setattr(config, "BILIBILI_COOKIE_PATH", cookie_path)
    monkeypatch.setattr(bilibili_uploads, "_queue", queue.Queue())
    database.init_db()
    database.save_bilibili_settings("171")
    task_id = database.create_task(
        "https://www.youtube.com/watch?v=abcdefghijk",
        task_id="abcdefghijk",
        auto_upload_bilibili=True,
    )
    database.update_task(
        task_id,
        status="succeeded",
        current_stage="done",
        session_path=str(session),
        final_video_path=str(final_video),
    )

    first = bilibili_uploads.create_for_task(task_id)
    with pytest.raises(ValueError, match="already queued"):
        bilibili_uploads.create_for_task(task_id)

    database.update_bilibili_upload_job(first["id"], status="failed")
    second = bilibili_uploads.create_for_task(task_id)

    assert second["id"] != first["id"]
    assert second["source_url"] == "https://www.youtube.com/watch?v=abcdefghijk"
    assert second["tid"] == 171


def test_upload_worker_records_success(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.sqlite")
    workfolder = tmp_path / "workfolder"
    session = workfolder / "session"
    final_video = session / "media" / "video_final.mp4"
    final_video.parent.mkdir(parents=True)
    final_video.write_bytes(b"video")
    cookie_path = tmp_path / "cookies" / "bilibili.json"
    runtime_security.atomic_write_private_text(
        cookie_path,
        json.dumps({"token_info": {"mid": 1}, "cookie_info": {"cookies": []}}),
    )
    monkeypatch.setattr(config, "WORKFOLDER", workfolder)
    monkeypatch.setattr(config, "BILIBILI_COOKIE_PATH", cookie_path)
    database.init_db()
    task_id = database.create_task(
        "https://www.youtube.com/watch?v=abcdefghijk", task_id="abcdefghijk"
    )
    database.update_task(
        task_id,
        status="succeeded",
        session_path=str(session),
        final_video_path=str(final_video),
    )
    cover = session / "media" / "cover.jpg"
    Image.new("RGB", (8, 8), (0, 0, 0)).save(cover, "JPEG")
    job_id = database.create_bilibili_upload_job(
        task_id,
        title="Title",
        description="Description",
        tags=["tag"],
        tid=171,
        source_url="https://www.youtube.com/watch?v=abcdefghijk",
        cover_path=str(cover),
    )
    calls = []
    monkeypatch.setattr(biliup, "upload_video", lambda **kwargs: calls.append(kwargs) or "ok")

    bilibili_uploads._run_job(job_id)

    job = database.get_bilibili_upload_job(job_id)
    assert job["status"] == "succeeded"
    assert job["result_message"] == "Submitted to Bilibili."
    assert calls[0]["video_path"] == final_video
    assert calls[0]["cover_path"] == cover


def test_restart_marks_running_upload_as_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.sqlite")
    database.init_db()
    task_id = database.create_task(
        "https://www.youtube.com/watch?v=abcdefghijk", task_id="abcdefghijk"
    )
    job_id = database.create_bilibili_upload_job(
        task_id,
        title="Title",
        description="Description",
        tags=["tag"],
        tid=171,
        source_url="https://www.youtube.com/watch?v=abcdefghijk",
    )
    database.update_bilibili_upload_job(job_id, status="running")

    database.fail_stale_bilibili_upload_jobs()

    job = database.get_bilibili_upload_job(job_id)
    assert job["status"] == "unknown"
    with pytest.raises(ValueError, match="already unknown"):
        database.create_bilibili_upload_job(
            task_id,
            title="Title",
            description="Description",
            tags=["tag"],
            tid=171,
            source_url="https://www.youtube.com/watch?v=abcdefghijk",
        )


def test_list_zones_flattens_tree_and_is_cached(monkeypatch, tmp_path):
    cookie_path = tmp_path / "cookies" / "bilibili.json"
    runtime_security.atomic_write_private_text(
        cookie_path,
        json.dumps(
            {
                "token_info": {"mid": 1},
                "cookie_info": {"cookies": [{"name": "SESSDATA", "value": "s"}]},
            }
        ),
    )
    calls = {"count": 0}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(
                {
                    "code": 0,
                    "data": {
                        "typelist": [
                            {
                                "id": 36,
                                "name": "知识",
                                "children": [{"id": 201, "name": "科学科普", "children": []}],
                            }
                        ]
                    },
                }
            ).encode("utf-8")

    def fake_urlopen(_request, timeout=20):
        calls["count"] += 1
        return FakeResponse()

    monkeypatch.setattr(biliup.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(config, "BILIBILI_COOKIE_PATH", cookie_path)
    monkeypatch.setattr(bilibili_auth, "_zone_cache", {"fetched_at": 0.0, "zones": []})

    first = bilibili_auth.zones()
    second = bilibili_auth.zones()

    assert first == [
        {"id": 36, "name": "知识", "parent": None},
        {"id": 201, "name": "科学科普", "parent": 36},
    ]
    assert second == first
    assert calls["count"] == 1


def test_cover_composition_returns_16x10_jpeg(monkeypatch, tmp_path):
    frame = tmp_path / "frame.jpg"
    Image.new("RGB", (1920, 1080), (30, 60, 90)).save(frame, "JPEG")
    out = tmp_path / "cover.jpg"
    monkeypatch.setattr(summary, "_find_cjk_font", lambda: None)

    summary._compose_cover(frame, "中文标题测试", out)

    with Image.open(out) as image:
        assert image.format == "JPEG"
        assert image.size == (summary.COVER_WIDTH, summary.COVER_HEIGHT)
        assert image.size[0] / image.size[1] == 16 / 10


def test_generate_summary_writes_cover_path(monkeypatch, tmp_path):
    session = tmp_path / "session"
    (session / "media").mkdir(parents=True)
    frame = session / "media" / "cover_frame.jpg"
    Image.new("RGB", (640, 480), (10, 20, 30)).save(frame, "JPEG")
    monkeypatch.setattr(summary, "_extract_frame", lambda *args: frame)
    monkeypatch.setattr(summary, "_find_cjk_font", lambda: None)
    monkeypatch.setattr(summary, "_translate_title", lambda value: "翻译标题")
    monkeypatch.setattr(summary, "_translate_tags", lambda values: [])
    monkeypatch.setattr(summary, "_content_description", lambda *args: "内容")

    summary.generate_summary_md(
        session,
        {
            "title": "Original",
            "uploader": "Creator",
            "tags": [],
            "webpage_url": "https://example.com/v",
        },
    )

    publish = json.loads(
        (session / "metadata" / "bilibili_publish.json").read_text(encoding="utf-8")
    )
    assert publish["cover_path"] == str(session / "metadata" / "cover.jpg")
    assert (session / "metadata" / "cover.jpg").is_file()


def test_create_for_task_rejects_cover_outside_workfolder(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.sqlite")
    workfolder = tmp_path / "workfolder"
    session = workfolder / "s"
    final_video = session / "media" / "video_final.mp4"
    final_video.parent.mkdir(parents=True)
    final_video.write_bytes(b"v")
    outside = tmp_path / "outside" / "cover.jpg"
    outside.parent.mkdir()
    Image.new("RGB", (10, 10), (0, 0, 0)).save(outside, "JPEG")
    (session / "metadata").mkdir()
    (session / "metadata" / "bilibili_publish.json").write_text(
        json.dumps(
            {
                "title": "T",
                "description": "d",
                "tags": [],
                "cover_path": str(outside),
            }
        ),
        encoding="utf-8",
    )
    cookie_path = tmp_path / "cookies" / "bilibili.json"
    runtime_security.atomic_write_private_text(
        cookie_path,
        json.dumps({"token_info": {"mid": 1}, "cookie_info": {"cookies": []}}),
    )
    monkeypatch.setattr(config, "WORKFOLDER", workfolder)
    monkeypatch.setattr(config, "BILIBILI_COOKIE_PATH", cookie_path)
    monkeypatch.setattr(bilibili_uploads, "_queue", queue.Queue())
    database.init_db()
    database.save_bilibili_settings("171")
    task_id = database.create_task(
        "https://www.youtube.com/watch?v=abcdefghijk", task_id="abcdefghijk"
    )
    database.update_task(
        task_id,
        status="succeeded",
        session_path=str(session),
        final_video_path=str(final_video),
    )

    with pytest.raises(ValueError, match="cover"):
        bilibili_uploads.create_for_task(task_id)
