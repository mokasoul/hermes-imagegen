from __future__ import annotations

from imagegen_backend.store import HistoryStore


def test_history_roundtrip(tmp_path):
    store = HistoryStore(tmp_path / "history.sqlite3", tmp_path / "files")
    gid = store.create_generation(
        {
            "mode": "generate",
            "channel_id": "c1",
            "channel_name": "本机",
            "model": "gpt-image-2",
            "prompt": "a cat",
            "params": {"size": "1:1", "quality": "2k", "n": 1},
        }
    )
    record = store.finish_generation(gid, status="succeeded", duration_ms=1234, images=[(b"%PNGDATA%", "image/png", None)])
    assert record["status"] == "succeeded"
    assert record["duration_ms"] == 1234
    assert len(record["images"]) == 1
    image = record["images"][0]
    assert image["url"] == f"/files/{image['id']}"
    path, mime = store.image_path(image["id"])
    assert path.read_bytes() == b"%PNGDATA%"
    assert mime == "image/png"

    listing = store.list(limit=10, offset=0)
    assert listing["total"] == 1
    assert listing["items"][0]["id"] == gid
    assert listing["items"][0]["params"]["quality"] == "2k"


def test_delete_removes_rows_and_files(tmp_path):
    store = HistoryStore(tmp_path / "history.sqlite3", tmp_path / "files")
    gid = store.create_generation({"mode": "generate", "model": "m", "prompt": "p", "params": {}})
    record = store.finish_generation(gid, status="succeeded", duration_ms=1, images=[(b"%PNG%", "image/png", None)])
    path, _ = store.image_path(record["images"][0]["id"])
    assert path.exists()
    assert store.delete_generation(gid) is True
    assert not path.exists()
    assert store.get(gid) is None
    assert store.list()["total"] == 0
    assert store.delete_generation(gid) is False


def test_failed_generation_is_stored_with_error(tmp_path):
    store = HistoryStore(tmp_path / "history.sqlite3", tmp_path / "files")
    gid = store.create_generation({"mode": "generate", "model": "m", "prompt": "p", "params": {}})
    record = store.finish_generation(gid, status="failed", duration_ms=9, error="boom", error_code="upstream-rejected")
    assert record["status"] == "failed"
    assert record["error_code"] == "upstream-rejected"
    assert record["images"] == []


def test_pagination_is_newest_first(tmp_path):
    store = HistoryStore(tmp_path / "history.sqlite3", tmp_path / "files")
    ids = [store.create_generation({"mode": "generate", "model": "m", "prompt": f"p{i}", "params": {}}) for i in range(3)]
    listing = store.list(limit=2, offset=0)
    assert listing["total"] == 3
    assert len(listing["items"]) == 2
    assert listing["items"][0]["id"] == ids[-1]
