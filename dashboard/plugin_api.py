"""Backend routes for hermes-imagegen, mounted at /api/plugins/hermes-imagegen/.

The desktop half (``desktop/plugin.js``) talks to this router through
``ctx.rest('/…')``. Nothing here is reachable from the agent's tools, and no
handler returns an API key: every settings response is the masked view.

Endpoint map:

    GET    /bootstrap                 settings + version + recent jobs
    GET    /settings                  masked channels + defaults
    POST   /settings/channels         create/update a channel
    DELETE /settings/channels/<id>    remove a channel
    POST   /settings/defaults         set the default channel/model
    POST   /channels/detect           probe <base>/models and ?kind=image
    POST   /jobs                      submit a generation job
    GET    /jobs                      in-memory job list
    GET    /jobs/<id>                 one job (status + images)
    POST   /jobs/<id>/cancel          cancel a running job
    GET    /history                   paginated generation history
    GET    /history/<id>              one generation
    DELETE /history/<id>              delete a generation and its files
    GET    /files/<image_id>          serve one generated image
"""

from __future__ import annotations

import base64
import logging
import re
import sys
import threading
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

# The backend package sits beside this file; the gateway imports the api module
# by path, so the dashboard folder is not automatically on sys.path.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from imagegen_backend import engine as image_engine  # noqa: E402
from imagegen_backend.jobs import JobError, JobRunner  # noqa: E402
from imagegen_backend.settings import Settings, SettingsError  # noqa: E402
from imagegen_backend.store import HistoryStore  # noqa: E402

log = logging.getLogger(__name__)

router = APIRouter()

_PLUGIN_YAML = _HERE.parent / "plugin.yaml"

_lock = threading.Lock()
_settings: Optional[Settings] = None
_store: Optional[HistoryStore] = None
_runner: Optional[JobRunner] = None


def _plugin_version() -> str:
    try:
        text = _PLUGIN_YAML.read_text(encoding="utf-8")
    except OSError:
        return "0.0.0"
    match = re.search(r"^version:\s*\"?([^\"\n]+)\"?\s*$", text, re.M)
    return match.group(1).strip() if match else "0.0.0"


def _get_settings() -> Settings:
    global _settings
    with _lock:
        if _settings is None:
            _settings = Settings()
        return _settings


def _get_store() -> HistoryStore:
    global _store
    with _lock:
        if _store is None:
            _store = HistoryStore()
        return _store


def _get_runner() -> JobRunner:
    # Note: never call another _get_* from inside a ``with _lock`` block — the
    # lock is not reentrant, and the resulting self-deadlock only shows up as a
    # request that never returns.
    global _runner
    if _runner is not None:
        return _runner
    settings = _get_settings()
    store = _get_store()
    with _lock:
        if _runner is None:
            _runner = JobRunner(settings, store)
        return _runner


def _bad(error: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(error))


# --- bootstrap / health --------------------------------------------------


@router.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "version": _plugin_version()}


@router.get("/bootstrap")
async def bootstrap() -> dict[str, Any]:
    settings = _get_settings()
    return {
        "version": _plugin_version(),
        "settings": settings.public(),
        "jobs": _get_runner().jobs(),
    }


@router.get("/settings")
async def read_settings() -> dict[str, Any]:
    return _get_settings().public()


@router.post("/settings/channels")
async def upsert_channel(body: dict[str, Any]) -> dict[str, Any]:
    try:
        return {"channel": _get_settings().upsert_channel(body)}
    except SettingsError as error:
        raise _bad(error) from error


@router.delete("/settings/channels/{channel_id}")
async def delete_channel(channel_id: str) -> dict[str, Any]:
    try:
        _get_settings().delete_channel(channel_id)
    except SettingsError as error:
        raise _bad(error) from error
    return {"ok": True, "settings": _get_settings().public()}


@router.post("/settings/defaults")
async def set_defaults(body: dict[str, Any]) -> dict[str, Any]:
    try:
        _get_settings().set_defaults(
            default_channel_id=body.get("default_channel_id"),
            default_model=body.get("default_model"),
        )
    except SettingsError as error:
        raise _bad(error) from error
    return {"settings": _get_settings().public()}


# --- model discovery -----------------------------------------------------


@router.post("/channels/detect")
async def detect_channel(body: dict[str, Any]) -> dict[str, Any]:
    """Probe a stored channel, or an unsaved form's URL+key before saving."""
    settings = _get_settings()
    channel_id = str(body.get("channel_id") or "").strip()
    if channel_id:
        channel = settings.channel(channel_id)
        if channel is None:
            raise HTTPException(status_code=404, detail="渠道不存在")
    else:
        channel = {
            "api_url": str(body.get("api_url") or ""),
            "api_url_full": bool(body.get("api_url_full", False)),
            "protocol": str(body.get("protocol") or "auto"),
            "api_key": str(body.get("api_key") or ""),
            "api_key_env": str(body.get("api_key_env") or ""),
        }
        if not channel["api_url"]:
            raise HTTPException(status_code=400, detail="api_url 不能为空")
    try:
        result = await image_engine.detect_models(channel)
    except image_engine.ImageGenError as error:
        raise HTTPException(status_code=400, detail={"message": str(error), "code": error.code}) from error
    if channel_id and body.get("save") is True:
        result["channel"] = settings.add_models(channel_id, [m["id"] for m in result["models"]])
    return result


# --- jobs ----------------------------------------------------------------


@router.post("/jobs")
async def submit_job(body: dict[str, Any], request: Request) -> dict[str, Any]:
    try:
        return {"job": _get_runner().submit(body)}
    except (JobError, SettingsError) as error:
        raise _bad(error) from error
    except image_engine.ImageGenError as error:
        raise HTTPException(status_code=400, detail={"message": str(error), "code": error.code}) from error


@router.get("/jobs")
async def list_jobs() -> dict[str, Any]:
    return {"jobs": _get_runner().jobs()}


@router.get("/jobs/{job_id}")
async def read_job(job_id: str) -> dict[str, Any]:
    job = _get_runner().get(job_id)
    if job is None:
        return {"job": {"id": job_id, "status": "lost", "error": "该任务状态已失效（网关重启过）", "error_code": "job-lost", "images": []}}
    return {"job": job}


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict[str, Any]:
    job = _get_runner().cancel(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"job": job}


# --- history -------------------------------------------------------------


@router.get("/history")
async def history(
    limit: int = Query(30, ge=1, le=200),
    offset: int = Query(0, ge=0),
    favorite: bool = Query(False),
) -> dict[str, Any]:
    return _get_store().list(limit=limit, offset=offset, favorite_only=favorite)


@router.post("/history/{generation_id}/favorite")
async def favorite_history_item(generation_id: str, body: dict[str, Any]) -> dict[str, Any]:
    record = _get_store().set_favorite(generation_id, bool(body.get("value", True)))
    if record is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    return {"generation": record}


@router.get("/history/{generation_id}")
async def history_item(generation_id: str) -> dict[str, Any]:
    record = _get_store().get(generation_id)
    if record is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    return {"generation": record}


@router.delete("/history/{generation_id}")
async def delete_history_item(generation_id: str) -> dict[str, Any]:
    if not _get_store().delete_generation(generation_id):
        raise HTTPException(status_code=404, detail="记录不存在")
    return {"ok": True}


# --- image files ---------------------------------------------------------


@router.get("/files/{image_id}")
async def image_file(image_id: str) -> FileResponse:
    found = _get_store().image_path(image_id)
    if found is None:
        raise HTTPException(status_code=404, detail="图片不存在")
    path, mime = found
    if not path.exists():
        raise HTTPException(status_code=410, detail="图片文件已被删除")
    return FileResponse(path, media_type=mime, headers={"cache-control": "private, max-age=31536000, immutable"})


@router.get("/files/{image_id}/inline")
async def image_inline(image_id: str) -> dict[str, Any]:
    """The image as a data URL.

    The desktop renderer cannot put the gateway credential on an ``<img src>``
    (native auth rides a header the app's own REST door adds), so the plugin
    UI fetches bytes through ``ctx.rest`` and renders a data URL. The plugin UI
    caches these per image id, so this is one read per picture, not per render.
    """
    found = _get_store().image_path(image_id)
    if found is None:
        raise HTTPException(status_code=404, detail="图片不存在")
    path, mime = found
    if not path.exists():
        raise HTTPException(status_code=410, detail="图片文件已被删除")
    data = path.read_bytes()
    return {
        "id": image_id,
        "mime": mime,
        "data_url": f"data:{mime};base64,{base64.b64encode(data).decode()}",
    }
