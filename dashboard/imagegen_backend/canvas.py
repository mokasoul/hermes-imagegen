"""Infinite canvas: documents, nodes, connections and their assets.

Port of the upstream ``src/canvas-store.ts``, trimmed to the parts that stand on
their own in Hermes: page CRUD with optimistic concurrency, image/text/config
nodes, connections between them, and a content-addressed asset pool.

Deliberately not ported (each was a separate, host-coupled feature upstream):
sketch boards, poster recipes, vision layer splitting, masks, file nodes with
office previews, and canvas skills. Those arrive with their own phase or not at
all — nothing here fakes them.
"""

from __future__ import annotations

import base64
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import paths

MAX_CANVAS_FILE_BYTES = 50 * 1024 * 1024
NODE_TYPES = {"image", "text", "config"}
BACKGROUNDS = {"dots", "lines", "blank"}

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")


class CanvasError(ValueError):
    """Rejected canvas request (bad id, unknown page, bad payload)."""


class CanvasConflict(CanvasError):
    """The caller's revision is stale — somebody saved first."""


def _canvas_root() -> Path:
    root = paths.data_root() / "canvas"
    (root / "pages").mkdir(parents=True, exist_ok=True)
    (root / "assets").mkdir(parents=True, exist_ok=True)
    return root


def _page_path(canvas_id: str) -> Path:
    if not _ID_RE.match(canvas_id):
        raise CanvasError("画布 id 非法")
    return _canvas_root() / "pages" / f"{canvas_id}.json"


def _asset_path(asset_id: str, ext: str = "") -> Path:
    if not _ID_RE.match(asset_id):
        raise CanvasError("素材 id 非法")
    return _canvas_root() / "assets" / f"{asset_id}{ext}"


def _new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:20]}"


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_json(path: Path, value: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def default_document(title: str = "未命名画布") -> dict[str, Any]:
    now = int(time.time() * 1000)
    return {
        "version": 2,
        "id": _new_id(),
        "title": title.strip() or "未命名画布",
        "revision": 1,
        "viewport": {"x": 0, "y": 0, "k": 1},
        "background": "dots",
        "nodes": [],
        "connections": [],
        "createdAt": now,
        "updatedAt": now,
        "favorite": False,
    }


def _number(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _normalise_metadata(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return {}
    keep = (
        "asset", "status", "error", "prompt", "model", "channelId", "size",
        "quality", "count", "mode", "taskId", "sourceNodeId", "text",
    )
    meta: dict[str, Any] = {}
    for key in keep:
        if key in raw:
            meta[key] = raw[key]
    asset = meta.get("asset")
    if isinstance(asset, Mapping):
        meta["asset"] = {
            "id": str(asset.get("id") or ""),
            "mime": str(asset.get("mime") or "image/png"),
            "name": str(asset.get("name") or ""),
        }
    return meta


def _normalise_node(raw: Mapping[str, Any]) -> dict[str, Any]:
    node_type = str(raw.get("type") or "")
    if node_type not in NODE_TYPES:
        raise CanvasError(f"不支持的节点类型：{node_type or '(空)'}")
    node_id = str(raw.get("id") or "") or _new_id("n")
    return {
        "id": node_id,
        "type": node_type,
        "title": str(raw.get("title") or ""),
        "x": _number(raw.get("x")),
        "y": _number(raw.get("y")),
        "width": max(80.0, _number(raw.get("width"), 240.0)),
        "height": max(60.0, _number(raw.get("height"), 160.0)),
        "metadata": _normalise_metadata(raw.get("metadata")),
    }


def _normalise_connections(raw: Any, node_ids: set[str]) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    seen: set[tuple[str, str]] = set()
    edges: list[dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        source = str(entry.get("fromNodeId") or "")
        target = str(entry.get("toNodeId") or "")
        if source not in node_ids or target not in node_ids or source == target:
            continue
        if (source, target) in seen:
            continue
        seen.add((source, target))
        edges.append({"id": str(entry.get("id") or f"e_{source}_{target}"), "fromNodeId": source, "toNodeId": target})
    return edges


def normalise_document(raw: Mapping[str, Any], canvas_id: str) -> dict[str, Any]:
    """Validate a client document; unknown/unsupported shapes are refused, not dropped silently."""
    nodes = [_normalise_node(node) for node in raw.get("nodes") or [] if isinstance(node, Mapping)]
    node_ids = {node["id"] for node in nodes}
    if len(node_ids) != len(nodes):
        raise CanvasError("节点 id 重复")
    viewport = raw.get("viewport") if isinstance(raw.get("viewport"), Mapping) else {}
    background = str(raw.get("background") or "dots")
    if background not in BACKGROUNDS:
        background = "dots"
    now = int(time.time() * 1000)
    return {
        "version": 2,
        "id": canvas_id,
        "title": str(raw.get("title") or "未命名画布"),
        "revision": int(_number(raw.get("revision"), 1)),
        "viewport": {
            "x": _number(viewport.get("x")),
            "y": _number(viewport.get("y")),
            "k": min(3.0, max(0.2, _number(viewport.get("k"), 1.0))),
        },
        "background": background,
        "nodes": nodes,
        "connections": _normalise_connections(raw.get("connections"), node_ids),
        "createdAt": int(_number(raw.get("createdAt"), now)),
        "updatedAt": now,
        "favorite": bool(raw.get("favorite")),
    }


def list_pages() -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for path in sorted(_canvas_root().glob("pages/*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        document = _read_json(path)
        if not isinstance(document, Mapping):
            continue
        summaries.append(
            {
                "id": document.get("id") or path.stem,
                "title": document.get("title") or "未命名画布",
                "revision": int(_number(document.get("revision"), 1)),
                "nodeCount": len(document.get("nodes") or []),
                "createdAt": int(_number(document.get("createdAt"))),
                "updatedAt": int(_number(document.get("updatedAt"))),
                "favorite": bool(document.get("favorite")),
            }
        )
    summaries.sort(key=lambda entry: entry["updatedAt"], reverse=True)
    return summaries


def create_page(title: str = "未命名画布") -> dict[str, Any]:
    document = default_document(title)
    _write_json(_page_path(document["id"]), document)
    return document


def read_page(canvas_id: str) -> dict[str, Any]:
    document = _read_json(_page_path(canvas_id))
    if not isinstance(document, Mapping):
        raise CanvasError("画布不存在或已损坏")
    return dict(document)


def save_page(canvas_id: str, document: Mapping[str, Any], expected_revision: int | None = None) -> dict[str, Any]:
    path = _page_path(canvas_id)
    current = _read_json(path)
    if not isinstance(current, Mapping):
        raise CanvasError("画布不存在")
    current_revision = int(_number(current.get("revision"), 1))
    if expected_revision is not None and int(expected_revision) != current_revision:
        raise CanvasConflict(f"画布已被其他窗口修改（服务端 r{current_revision}，你保存的是 r{expected_revision}）")
    merged = normalise_document(document, canvas_id)
    merged["revision"] = current_revision + 1
    merged["createdAt"] = int(_number(current.get("createdAt"), merged["createdAt"]))
    merged["favorite"] = bool(current.get("favorite"))
    _write_json(path, merged)
    return merged


def remove_page(canvas_id: str) -> None:
    path = _page_path(canvas_id)
    if not path.exists():
        raise CanvasError("画布不存在")
    path.rename(path.with_suffix(".json.removed"))


def set_favorite(canvas_id: str, favorite: bool) -> dict[str, Any]:
    document = read_page(canvas_id)
    document["favorite"] = bool(favorite)
    _write_json(_page_path(canvas_id), document)
    return {"id": canvas_id, "favorite": document["favorite"]}


_EXT_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "text/plain": ".txt",
    "application/json": ".json",
}


def save_asset(data: bytes, mime: str, name: str = "") -> dict[str, Any]:
    if not data:
        raise CanvasError("素材为空")
    if len(data) > MAX_CANVAS_FILE_BYTES:
        raise CanvasError("素材超过 50MB 上限")
    asset_id = uuid.uuid4().hex
    path = _asset_path(asset_id, _EXT_BY_MIME.get(mime, ""))
    path.write_bytes(data)
    return {"id": asset_id, "mime": mime, "name": name}


def save_asset_data_url(data_url: str, name: str = "") -> dict[str, Any]:
    match = re.match(r"^data:([^;,]+)?(;base64)?,(.*)$", data_url.strip(), re.S)
    if match is None or match.group(2) is None:
        raise CanvasError("素材必须是 base64 data URL")
    mime = match.group(1) or "application/octet-stream"
    try:
        data = base64.b64decode(match.group(3))
    except (ValueError, base64.binascii.Error) as error:  # type: ignore[attr-defined]
        raise CanvasError("素材 base64 无法解码") from error
    return save_asset(data, mime, name)


def read_asset(asset_id: str) -> tuple[bytes, str]:
    for mime, ext in _EXT_BY_MIME.items():
        path = _asset_path(asset_id, ext)
        if path.exists():
            return path.read_bytes(), mime
    raise CanvasError("素材不存在")


def asset_pool_size() -> int:
    return len(list(_canvas_root().glob("assets/*")))


def node_ids(document: Mapping[str, Any]) -> Iterable[str]:
    return [str(node.get("id")) for node in document.get("nodes") or []]
