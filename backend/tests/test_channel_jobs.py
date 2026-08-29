from __future__ import annotations

import backend.app.adapters.channels as channels_adapter

from backend.app import channel_jobs, database, worker


def _run_job_sync(job_id: str) -> None:
    """直接同步执行 job 逻辑（不启动线程），便于断言。"""
    channel_jobs._run_job(job_id)


def _isolate_worker_enqueue(monkeypatch) -> None:
    """避免 channel 测试污染全局 worker 队列。"""
    monkeypatch.setattr(worker, "enqueue", lambda task_id: None)


def _fake_channel_info(channel_url):
    return {
        "channel_name": channel_url.split("@")[-1],
        "channel_id": f"UC-{channel_url.split('@')[-1]}",
    }


def test_channel_job_creates_tasks_and_skips_existing(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "channels.sqlite")
    database.init_db()
    _isolate_worker_enqueue(monkeypatch)
    monkeypatch.setattr(channels_adapter, "fetch_channel_info", _fake_channel_info)

    # Cache video lists for both channels.
    cached_videos = {
        "https://www.youtube.com/@ChanA": [
            {"id": "aaaaaaaaaaa", "title": "Video A", "url": "https://www.youtube.com/watch?v=aaaaaaaaaaa"},
            {"id": "bbbbbbbbbbb", "title": "Video B", "url": "https://www.youtube.com/watch?v=bbbbbbbbbbb"},
        ],
        "https://www.youtube.com/@ChanB": [
            {"id": "ccccccccccc", "title": "Video C", "url": "https://www.youtube.com/watch?v=ccccccccccc"},
        ],
    }
    for channel_url, videos in cached_videos.items():
        sub_id = database.upsert_channel_subscription(channel_url, channel_name="ChanA")
        database.replace_channel_videos(sub_id, videos)

    # Pre-create one existing task so it should be skipped.
    existing = database.create_task(
        "https://www.youtube.com/watch?v=aaaaaaaaaaa",
        task_id="aaaaaaaaaaa",
    )

    job_id = database.create_channel_job(list(cached_videos.keys()), 2)
    _run_job_sync(job_id)

    job = database.get_channel_job(job_id)
    assert job["status"] == "succeeded"
    assert job["total"] == 2
    assert job["processed"] == 2
    # 2 new (bbb, ccc) + 1 skipped (aaa existing)
    assert job["created_count"] == 2
    assert job["skipped_count"] == 1

    created_task = database.get_task("bbbbbbbbbbb")
    assert created_task is not None
    assert created_task["status"] == "queued"
    existing_after = database.get_task("aaaaaaaaaaa")
    assert existing_after["id"] == existing


def test_channel_job_process_falls_back_to_refresh_when_cache_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "channels-fb.sqlite")
    database.init_db()
    _isolate_worker_enqueue(monkeypatch)
    monkeypatch.setattr(channels_adapter, "fetch_channel_info", _fake_channel_info)

    channel_url = "https://www.youtube.com/@ChanA"
    sub_id = database.upsert_channel_subscription(channel_url, channel_name="ChanA")

    fetched: list[dict] = [
        {"id": "eeeeeeeeeee", "title": "Video E", "url": "https://www.youtube.com/watch?v=eeeeeeeeeee"},
    ]

    def fake_fetch_videos(url, limit=None):
        return fetched

    monkeypatch.setattr(channels_adapter, "fetch_channel_videos", fake_fetch_videos)

    # Cache is empty -> process job should refresh then create task.
    job_id = database.create_channel_job([channel_url], 5)
    _run_job_sync(job_id)

    job = database.get_channel_job(job_id)
    assert job["status"] == "succeeded"
    assert job["created_count"] == 1
    assert job["skipped_count"] == 0
    assert database.count_channel_videos(sub_id) == 1


def test_channel_job_refresh_caches_videos(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "channels-refresh.sqlite")
    database.init_db()
    _isolate_worker_enqueue(monkeypatch)
    monkeypatch.setattr(channels_adapter, "fetch_channel_info", _fake_channel_info)

    channel_url = "https://www.youtube.com/@ChanA"
    sub_id = database.upsert_channel_subscription(channel_url, channel_name="ChanA")

    fetched = [
        {"id": "fffffffffff", "title": "F1", "url": "https://www.youtube.com/watch?v=fffffffffff"},
        {"id": "ggggggggggg", "title": "F2", "url": "https://www.youtube.com/watch?v=ggggggggggg"},
    ]

    def fake_fetch_videos(url, limit=None):
        return fetched

    monkeypatch.setattr(channels_adapter, "fetch_channel_videos", fake_fetch_videos)

    job_id = database.create_channel_job([channel_url], 0, kind="refresh")
    _run_job_sync(job_id)

    job = database.get_channel_job(job_id)
    assert job["status"] == "succeeded"
    assert job["processed"] == 1
    cached = database.list_channel_videos(sub_id)
    assert len(cached) == 2
    assert cached[0]["video_id"] == "fffffffffff"


def test_channel_job_respects_existing_statuses(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "channels-status.sqlite")
    database.init_db()
    _isolate_worker_enqueue(monkeypatch)

    channel_url = "https://www.youtube.com/@ChanC"
    sub_id = database.upsert_channel_subscription(channel_url, channel_name="ChanC")
    database.replace_channel_videos(
        sub_id,
        [{"id": "ddddddddddd", "title": "D", "url": "https://www.youtube.com/watch?v=ddddddddddd"}],
    )

    # A failed/queued task with the same video id should still be skipped.
    database.create_task(
        "https://www.youtube.com/watch?v=ddddddddddd",
        task_id="ddddddddddd",
    )

    job_id = database.create_channel_job([channel_url], 1)
    _run_job_sync(job_id)

    job = database.get_channel_job(job_id)
    assert job["created_count"] == 0
    assert job["skipped_count"] == 1


def test_channel_subscription_delete(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "channels-del.sqlite")
    database.init_db()

    sub_id = database.upsert_channel_subscription(
        "https://www.youtube.com/@ChanD",
        channel_name="ChanD",
        channel_id="UC-ChanD",
    )
    assert database.delete_channel_subscription(sub_id) is True
    assert database.list_channel_subscriptions() == []
    assert database.delete_channel_subscription(sub_id) is False


def test_resolve_channel_url_from_video(monkeypatch):
    fake_info = {
        "channel_url": "https://www.youtube.com/@TheCherno",
        "channel": "The Cherno",
    }

    def fake_extract(self, url, download=False):
        assert url == "https://www.youtube.com/watch?v=s0oN6Tx5Xxo"
        return fake_info

    monkeypatch.setattr(channels_adapter.yt_dlp.YoutubeDL, "extract_info", fake_extract)

    channel_url, name = channels_adapter.resolve_channel_url(
        "https://www.youtube.com/watch?v=s0oN6Tx5Xxo"
    )
    assert channel_url == "https://www.youtube.com/@TheCherno"
    assert name == "The Cherno"


def test_resolve_channel_url_direct_channel(monkeypatch):
    fake_info = {
        "channel_url": "",
        "webpage_url": "https://www.youtube.com/@TheCherno",
        "channel": "The Cherno",
    }

    def fake_extract(self, url, download=False):
        assert "TheCherno" in url
        return fake_info

    monkeypatch.setattr(channels_adapter.yt_dlp.YoutubeDL, "extract_info", fake_extract)

    channel_url, name = channels_adapter.resolve_channel_url(
        "https://www.youtube.com/@TheCherno"
    )
    assert channel_url == "https://www.youtube.com/@TheCherno"
    assert name == "The Cherno"


def test_fetch_channel_videos_all_and_limited(monkeypatch):
    fake_entries = [
        {"_type": "url", "id": "aaaaaaaaaaa", "title": "Video A", "url": "https://www.youtube.com/watch?v=aaaaaaaaaaa"},
        {"_type": "url", "id": "bbbbbbbbbbb", "title": "Video B"},
        {"_type": "playlist", "id": "ignored"},
    ]

    def fake_extract(self, url, download=False):
        assert url.endswith("/videos")
        return {"entries": fake_entries}

    monkeypatch.setattr(channels_adapter.yt_dlp.YoutubeDL, "extract_info", fake_extract)

    all_videos = channels_adapter.fetch_channel_videos("https://www.youtube.com/@X", limit=None)
    assert len(all_videos) == 2
    assert all_videos[0]["url"].startswith("https://")
    # entry without url should fall back to watch URL
    assert all_videos[1]["url"] == "https://www.youtube.com/watch?v=bbbbbbbbbbb"

    limited = channels_adapter.fetch_channel_videos("https://www.youtube.com/@X", limit=5)
    assert len(limited) == 2


def test_fetch_channel_videos_extracts_view_count(monkeypatch):
    fake_entries = [
        {
            "_type": "url",
            "id": "viewvid01",
            "title": "With views",
            "url": "https://www.youtube.com/watch?v=viewvid01",
            "view_count": 12345,
        },
        {
            "_type": "url",
            "id": "viewvid02",
            "title": "Zero views",
            "url": "https://www.youtube.com/watch?v=viewvid02",
            "view_count": 0,
        },
        {
            "_type": "url",
            "id": "viewvid03",
            "title": "Missing metadata",
            "url": "https://www.youtube.com/watch?v=viewvid03",
        },
    ]

    def fake_extract(self, url, download=False):
        return {"entries": fake_entries}

    monkeypatch.setattr(channels_adapter.yt_dlp.YoutubeDL, "extract_info", fake_extract)

    videos = channels_adapter.fetch_channel_videos("https://www.youtube.com/@X")
    assert videos[0]["view_count"] == 12345
    assert videos[1]["view_count"] == 0
    assert videos[2]["view_count"] is None
    assert "published_at" not in videos[0]
