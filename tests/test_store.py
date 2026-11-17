from __future__ import annotations

import pytest

from imagegen_backend.store import HistoryStore


@pytest.fixture()
def store(tmp_path):
    return HistoryStore(tmp_path / "history.sqlite3", tmp_path / "files")


def _record(store, prompt="x"):
    """Create a finished generation and return its id."""
    gid = store.create_generation({"mode": "generate", "model": "m", "prompt": prompt, "params": {}})
    store.finish_generation(gid, status="succeeded", duration_ms=10, images=[(b"%PNGDATA%", "image/png", None)])
    return gid


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


def test_favorite_toggle_and_filter(tmp_path):
    store = HistoryStore(tmp_path / "history.sqlite3", tmp_path / "files")
    starred = store.create_generation({"mode": "generate", "model": "m", "prompt": "keep", "params": {}})
    other = store.create_generation({"mode": "generate", "model": "m", "prompt": "plain", "params": {}})
    assert store.get(starred)["favorite"] is False
    record = store.set_favorite(starred, True)
    assert record["favorite"] is True
    assert store.list(favorite_only=True)["total"] == 1
    assert store.list(favorite_only=True)["items"][0]["id"] == starred
    assert store.list()["total"] == 2
    store.set_favorite(starred, False)
    assert store.list(favorite_only=True)["total"] == 0
    assert store.set_favorite("missing", True) is None
    assert other != starred


def test_migration_adds_favorite_column_to_an_old_database(tmp_path):
    import sqlite3

    db = tmp_path / "history.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE generations (
          id TEXT PRIMARY KEY, created_at REAL NOT NULL, status TEXT NOT NULL, mode TEXT NOT NULL,
          channel_id TEXT, channel_name TEXT, model TEXT, prompt TEXT, params TEXT,
          duration_ms INTEGER, error TEXT, error_code TEXT
        );
        CREATE TABLE images (
          id TEXT PRIMARY KEY, generation_id TEXT NOT NULL, position INTEGER NOT NULL, path TEXT NOT NULL,
          mime TEXT NOT NULL, width INTEGER, height INTEGER, size_bytes INTEGER, revised_prompt TEXT
        );
        INSERT INTO generations (id, created_at, status, mode, model, prompt, params)
          VALUES ('legacy', 1700000000, 'succeeded', 'generate', 'm', 'old row', '{}');
        """
    )
    conn.commit()
    conn.close()

    store = HistoryStore(db, tmp_path / "files")
    listing = store.list()
    assert listing["total"] == 1
    assert listing["items"][0]["favorite"] is False
    assert listing["items"][0]["tags"] == []
    assert store.set_favorite("legacy", True)["favorite"] is True
    assert store.set_tags("legacy", ["主图", " 熨斗 ", "主图", ""])["tags"] == ["主图", "熨斗"]


def test_tags_round_trip_and_filter(store):
    first = _record(store, prompt="one")
    second = _record(store, prompt="two")
    assert store.set_tags(first, ["猫咪", "头像"])["tags"] == ["猫咪", "头像"]
    assert store.set_tags(second, ["猫咪"])["tags"] == ["猫咪"]
    assert store.set_tags(first, [])["tags"] == []
    # the cleared row must drop out of the tag filter
    assert [item["id"] for item in store.list(tag="猫咪")["items"]] == [second]
    assert store.list(tag="头像")["total"] == 0
    assert store.tags() == ["猫咪"]
    assert store.set_tags("nope", ["x"]) is None


def test_tags_are_capped_and_trimmed(store):
    record_id = _record(store, prompt="many")
    tags = [f"t{index}" for index in range(12)] + ["x" * 40]
    stored = store.set_tags(record_id, tags)["tags"]
    assert stored == [f"t{index}" for index in range(8)]
    long_tag = store.set_tags(record_id, ["y" * 40])["tags"][0]
    assert len(long_tag) == 24


def test_bulk_actions_report_missing_ids(store):
    first = _record(store, prompt="a")
    second = _record(store, prompt="b")
    result = store.bulk([first, second, "ghost"], "favorite")
    assert result["done"] == [first, second]
    assert result["missing"] == ["ghost"]
    assert store.get(first)["favorite"] is True
    result = store.bulk([first], "delete")
    assert result["done"] == [first]
    assert store.get(first) is None
    with pytest.raises(ValueError):
        store.bulk([second], "explode")
