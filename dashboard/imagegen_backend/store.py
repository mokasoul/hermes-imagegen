"""Generation history and image files.

One SQLite file per profile plus a date-partitioned image tree:

    <data>/history.sqlite3
    <data>/files/20261117/<image-id>.png

Images are written once and never rewritten; deleting a generation removes its
rows and its files together so the library cannot leak orphans.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import paths

_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}

MAX_TAGS = 8
MAX_TAG_LEN = 24


def normalise_tags(tags: Iterable[str]) -> list[str]:
    """Trim, drop empties, dedupe preserving order, cap count and length."""
    out: list[str] = []
    for raw in tags:
        tag = str(raw).strip()[:MAX_TAG_LEN]
        if tag and tag not in out:
            out.append(tag)
        if len(out) >= MAX_TAGS:
            break
    return out


def _parse_json_columns(record: dict[str, Any]) -> None:
    """Turn the TEXT columns back into structures, in place."""
    try:
        params = json.loads(record.get("params") or "{}")
    except json.JSONDecodeError:
        params = {}
    record["params"] = params if isinstance(params, dict) else {}
    try:
        tags = json.loads(record.get("tags") or "[]")
    except json.JSONDecodeError:
        tags = []
    record["tags"] = [str(tag) for tag in tags] if isinstance(tags, list) else []
    record["favorite"] = bool(record.get("favorite"))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS generations (
  id TEXT PRIMARY KEY,
  created_at REAL NOT NULL,
  status TEXT NOT NULL,
  mode TEXT NOT NULL,
  channel_id TEXT,
  channel_name TEXT,
  model TEXT,
  prompt TEXT,
  params TEXT,
  duration_ms INTEGER,
  error TEXT,
  error_code TEXT,
  favorite INTEGER NOT NULL DEFAULT 0,
  tags TEXT
);
CREATE TABLE IF NOT EXISTS images (
  id TEXT PRIMARY KEY,
  generation_id TEXT NOT NULL,
  position INTEGER NOT NULL,
  path TEXT NOT NULL,
  mime TEXT NOT NULL,
  width INTEGER,
  height INTEGER,
  size_bytes INTEGER,
  revised_prompt TEXT
);
CREATE INDEX IF NOT EXISTS generations_created ON generations(created_at DESC);
CREATE INDEX IF NOT EXISTS images_generation ON images(generation_id);
"""


class HistoryStore:
    def __init__(self, db_path: Path | None = None, files_dir: Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else paths.history_db()
        self.files_dir = Path(files_dir) if files_dir is not None else paths.files_root()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # --- plumbing --------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            # Additive migration: a database created before favourites existed
            # keeps its rows — ALTER TABLE only adds the missing column.
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(generations)")}
            if "favorite" not in columns:
                conn.execute("ALTER TABLE generations ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0")
            if "tags" not in columns:
                conn.execute("ALTER TABLE generations ADD COLUMN tags TEXT")

    # --- writes ----------------------------------------------------------

    def create_generation(self, payload: Mapping[str, Any]) -> str:
        generation_id = uuid.uuid4().hex
        params = payload.get("params") or {}
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO generations (id, created_at, status, mode, channel_id, channel_name, model, prompt, params)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    generation_id,
                    time.time(),
                    "running",
                    str(payload.get("mode") or "generate"),
                    str(payload.get("channel_id") or ""),
                    str(payload.get("channel_name") or ""),
                    str(payload.get("model") or ""),
                    str(payload.get("prompt") or ""),
                    json.dumps(params, ensure_ascii=False),
                ),
            )
        return generation_id

    def finish_generation(
        self,
        generation_id: str,
        *,
        status: str,
        duration_ms: int,
        images: Iterable[tuple[bytes, str, str | None]] = (),
        error: str | None = None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        stored: list[dict[str, Any]] = []
        for data, mime, revised in images:
            image_id = uuid.uuid4().hex
            path = self._write_file(image_id, mime, data)
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO images (id, generation_id, position, path, mime, size_bytes, revised_prompt)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (image_id, generation_id, len(stored), str(path), mime, len(data), revised),
                )
            stored.append({"id": image_id, "mime": mime, "size_bytes": len(data), "revised_prompt": revised, "url": f"/files/{image_id}"})
        with self._connect() as conn:
            conn.execute(
                "UPDATE generations SET status = ?, duration_ms = ?, error = ?, error_code = ? WHERE id = ?",
                (status, int(duration_ms), error, error_code, generation_id),
            )
        record = self.get(generation_id)
        return record or {"id": generation_id, "status": status, "images": stored}

    def _write_file(self, image_id: str, mime: str, data: bytes) -> Path:
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        folder = self.files_dir / day
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{image_id}.{_EXT.get(mime, 'png')}"
        path.write_bytes(data)
        return path

    def delete_generation(self, generation_id: str) -> bool:
        with self._connect() as conn:
            rows = conn.execute("SELECT path FROM images WHERE generation_id = ?", (generation_id,)).fetchall()
            cursor = conn.execute("DELETE FROM generations WHERE id = ?", (generation_id,))
            conn.execute("DELETE FROM images WHERE generation_id = ?", (generation_id,))
        if cursor.rowcount == 0:
            return False
        for row in rows:
            path = Path(row["path"])
            day = path.parent
            try:
                path.unlink(missing_ok=True)
                if day.is_dir() and not any(day.iterdir()):
                    day.rmdir()
            except OSError:
                pass
        return True

    # --- reads -----------------------------------------------------------

    def get(self, generation_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM generations WHERE id = ?", (generation_id,)).fetchone()
            if row is None:
                return None
            images = conn.execute(
                "SELECT * FROM images WHERE generation_id = ? ORDER BY position", (generation_id,)
            ).fetchall()
        record = dict(row)
        _parse_json_columns(record)
        record["images"] = [self._image_public(dict(image)) for image in images]
        return record

    def list(
        self, limit: int = 30, offset: int = 0, favorite_only: bool = False, tag: str = ""
    ) -> dict[str, Any]:
        limit = max(1, min(200, int(limit)))
        offset = max(0, int(offset))
        clauses: list[str] = []
        values: list[Any] = []
        if favorite_only:
            clauses.append("favorite = 1")
        if tag.strip():
            clauses.append("tags LIKE ?")
            values.append(f'%"{tag.strip()}"%')
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) AS n FROM generations{where}", values).fetchone()["n"]
            rows = conn.execute(
                f"SELECT * FROM generations{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (*values, limit, offset),
            ).fetchall()
            records: list[dict[str, Any]] = []
            for row in rows:
                record = dict(row)
                _parse_json_columns(record)
                images = conn.execute(
                    "SELECT * FROM images WHERE generation_id = ? ORDER BY position", (record["id"],)
                ).fetchall()
                record["images"] = [self._image_public(dict(image)) for image in images]
                records.append(record)
        return {"items": records, "total": total, "limit": limit, "offset": offset}

    def tags(self) -> list[str]:
        """Every tag in use, most frequent first — feeds the gallery's filter row."""
        counts: dict[str, int] = {}
        with self._connect() as conn:
            rows = conn.execute("SELECT tags FROM generations WHERE tags IS NOT NULL").fetchall()
        for row in rows:
            try:
                parsed = json.loads(row["tags"] or "[]")
            except json.JSONDecodeError:
                continue
            for tag in parsed if isinstance(parsed, list) else []:
                key = str(tag)
                counts[key] = counts.get(key, 0) + 1
        return [tag for tag, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]

    def set_favorite(self, generation_id: str, value: bool) -> dict[str, Any] | None:
        """Star/unstar one generation; returns the record, or None when absent."""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE generations SET favorite = ? WHERE id = ?", (1 if value else 0, generation_id)
            )
        if cursor.rowcount == 0:
            return None
        return self.get(generation_id)

    def set_tags(self, generation_id: str, tags: Iterable[str]) -> dict[str, Any] | None:
        """Replace the tag list; ``[]`` clears it. None when the record is gone."""
        clean = normalise_tags(tags)
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE generations SET tags = ? WHERE id = ?",
                (json.dumps(clean, ensure_ascii=False) if clean else None, generation_id),
            )
        if cursor.rowcount == 0:
            return None
        return self.get(generation_id)

    def bulk(self, ids: Iterable[str], action: str) -> dict[str, Any]:
        """Apply one action to many generations. Unknown ids are reported, not fatal."""
        wanted = [str(entry) for entry in ids if str(entry).strip()][:500]
        done: list[str] = []
        missing: list[str] = []
        for generation_id in wanted:
            if action == "delete":
                ok = self.delete_generation(generation_id)
            elif action in ("favorite", "unfavorite"):
                ok = self.set_favorite(generation_id, action == "favorite") is not None
            else:
                raise ValueError(f"未知批量操作：{action}")
            (done if ok else missing).append(generation_id)
        return {"action": action, "done": done, "missing": missing}

    def image_path(self, image_id: str) -> tuple[Path, str] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT path, mime FROM images WHERE id = ?", (image_id,)).fetchone()
        if row is None:
            return None
        return Path(row["path"]), str(row["mime"])

    @staticmethod
    def _image_public(image: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": image.get("id", ""),
            "mime": image.get("mime", "image/png"),
            "size_bytes": image.get("size_bytes", 0),
            "revised_prompt": image.get("revised_prompt"),
            "url": f"/files/{image.get('id', '')}",
        }
