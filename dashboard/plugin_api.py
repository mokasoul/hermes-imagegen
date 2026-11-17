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
from pathlib import Path
from typing import Any, Mapping

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse

# The backend package sits beside this file; the gateway imports the api module
# by path, so the dashboard folder is not automatically on sys.path.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from imagegen_backend import canvas as canvas_store  # noqa: E402
from imagegen_backend import ecommerce as ecommerce_kit  # noqa: E402
from imagegen_backend import engine as image_engine  # noqa: E402
from imagegen_backend import poster as poster_kit  # noqa: E402
from imagegen_backend import prompt_tools, runtime  # noqa: E402
from imagegen_backend import templates as template_library  # noqa: E402
from imagegen_backend.jobs import JobError, JobRunner  # noqa: E402
from imagegen_backend.settings import Settings, SettingsError  # noqa: E402
from imagegen_backend.store import HistoryStore  # noqa: E402

log = logging.getLogger(__name__)

router = APIRouter()

_PLUGIN_YAML = _HERE.parent / "plugin.yaml"


def _plugin_version() -> str:
    try:
        text = _PLUGIN_YAML.read_text(encoding="utf-8")
    except OSError:
        return "0.0.0"
    match = re.search(r"^version:\s*\"?([^\"\n]+)\"?\s*$", text, re.M)
    return match.group(1).strip() if match else "0.0.0"


# Settings / history / jobs live in `runtime` so the agent tools (loaded from the
# package __init__) and these routes share one instance of each.
def _get_settings() -> Settings:
    return runtime.settings()


def _get_store() -> HistoryStore:
    return runtime.store()


def _get_runner() -> JobRunner:
    return runtime.runner()


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


# --- e-commerce product sets --------------------------------------------


@router.get("/ecommerce/slots")
async def ecommerce_slots() -> dict[str, Any]:
    return {
        "slots": ecommerce_kit.slot_presets(),
        "roles": ecommerce_kit.ROLE_LABELS,
        "anchorPrefix": ecommerce_kit.ANCHOR_PREFIX,
        "defaultInfo": ecommerce_kit.DEFAULT_INFO,
    }


@router.post("/ecommerce/plan")
async def ecommerce_plan(body: dict[str, Any]) -> dict[str, Any]:
    draft = body.get("draft", body)
    if not isinstance(draft, dict):
        raise HTTPException(status_code=400, detail="缺少 draft")
    try:
        return ecommerce_kit.plan(draft)
    except ecommerce_kit.EcommerceError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


# --- infinite canvas -----------------------------------------------------


@router.get("/canvas/pages")
async def canvas_pages() -> dict[str, Any]:
    return {"pages": canvas_store.list_pages()}


@router.post("/canvas/pages")
async def canvas_create(body: dict[str, Any]) -> dict[str, Any]:
    return {"page": canvas_store.create_page(str(body.get("title") or "未命名画布"))}


@router.get("/canvas/pages/{canvas_id}")
async def canvas_read(canvas_id: str) -> dict[str, Any]:
    try:
        return {"page": canvas_store.read_page(canvas_id)}
    except canvas_store.CanvasError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.put("/canvas/pages/{canvas_id}")
async def canvas_save(canvas_id: str, body: dict[str, Any]) -> dict[str, Any]:
    document = body.get("document")
    if not isinstance(document, dict):
        raise HTTPException(status_code=400, detail="缺少 document")
    revision = body.get("revision")
    try:
        page = canvas_store.save_page(canvas_id, document, int(revision) if revision is not None else None)
    except canvas_store.CanvasConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except canvas_store.CanvasError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"page": page}


@router.delete("/canvas/pages/{canvas_id}")
async def canvas_remove(canvas_id: str) -> dict[str, Any]:
    try:
        canvas_store.remove_page(canvas_id)
    except canvas_store.CanvasError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return {"ok": True}


@router.post("/canvas/pages/{canvas_id}/favorite")
async def canvas_favorite(canvas_id: str, body: dict[str, Any]) -> dict[str, Any]:
    try:
        return canvas_store.set_favorite(canvas_id, bool(body.get("favorite", True)))
    except canvas_store.CanvasError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/canvas/assets")
async def canvas_asset_upload(body: dict[str, Any]) -> dict[str, Any]:
    data_url = str(body.get("data_url") or "")
    if not data_url:
        raise HTTPException(status_code=400, detail="缺少 data_url")
    try:
        return {"asset": canvas_store.save_asset_data_url(data_url, str(body.get("name") or ""))}
    except canvas_store.CanvasError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/canvas/assets/{asset_id}")
async def canvas_asset(asset_id: str) -> Response:
    try:
        data, mime = canvas_store.read_asset(asset_id)
    except canvas_store.CanvasError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return Response(content=data, media_type=mime, headers={"cache-control": "private, max-age=31536000, immutable"})


@router.get("/canvas/assets/{asset_id}/poster")
async def canvas_asset_poster(asset_id: str) -> dict[str, Any]:
    """A starting poster recipe for one asset (image = background, one title layer)."""
    try:
        data, mime = canvas_store.read_asset(asset_id)
        recipe = poster_kit.default_recipe({"id": asset_id, "mime": mime, "name": ""}, data)
    except canvas_store.CanvasError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except poster_kit.PosterError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"recipe": recipe}


@router.get("/canvas/assets/{asset_id}/inline")
async def canvas_asset_inline(asset_id: str) -> dict[str, Any]:
    try:
        data, mime = canvas_store.read_asset(asset_id)
    except canvas_store.CanvasError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return {"id": asset_id, "mime": mime, "data_url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}


# --- prompt helpers ------------------------------------------------------


def _prompt_channel(body: Mapping[str, Any]) -> dict[str, Any]:
    channel_id = str(body.get("channel_id") or "").strip()
    channel = _get_settings().channel(channel_id) if channel_id else None
    if channel is None:
        raise HTTPException(status_code=400, detail="请先选择一个渠道")
    return channel


@router.get("/prompt/models")
async def prompt_models(channel_id: str = Query("")) -> dict[str, Any]:
    channel = _get_settings().channel(channel_id) if channel_id else None
    if channel is None:
        raise HTTPException(status_code=400, detail="请先选择一个渠道")
    try:
        return {"models": await prompt_tools.list_models(channel), "channel_id": channel["id"]}
    except image_engine.ImageGenError as error:
        raise HTTPException(status_code=400, detail={"message": str(error), "code": error.code}) from error


@router.post("/prompt/enhance")
async def prompt_enhance(body: dict[str, Any]) -> dict[str, Any]:
    channel = _prompt_channel(body)
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="提示词不能为空")
    try:
        model = prompt_tools.chat_model_for(channel, body.get("model"))
        return {"prompt": await prompt_tools.enhance_prompt(channel, model, prompt), "model": model}
    except image_engine.ImageGenError as error:
        raise HTTPException(status_code=400, detail={"message": str(error), "code": error.code}) from error


@router.post("/prompt/polish")
async def prompt_polish(body: dict[str, Any]) -> dict[str, Any]:
    channel = _prompt_channel(body)
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="提示词不能为空")
    try:
        model = prompt_tools.chat_model_for(channel, body.get("model"))
        return {"prompt": await prompt_tools.polish_prompt(channel, model, prompt), "model": model}
    except image_engine.ImageGenError as error:
        raise HTTPException(status_code=400, detail={"message": str(error), "code": error.code}) from error


@router.post("/prompt/reverse")
async def prompt_reverse(body: dict[str, Any]) -> dict[str, Any]:
    channel = _prompt_channel(body)
    raw = [entry for entry in (body.get("references") or []) if isinstance(entry, str) and entry.strip()]
    if not raw:
        raise HTTPException(status_code=400, detail="反推至少需要一张图片")
    try:
        references = [
            image_engine.ReferenceImage(data=data, mime=mime)
            for data, mime in (image_engine.parse_data_url(entry) for entry in raw)
        ]
        model = prompt_tools.chat_model_for(channel, body.get("model"))
        text = await prompt_tools.reverse_prompt(
            channel,
            model,
            references,
            detail=str(body.get("detail") or "standard"),
            output=str(body.get("output") or "zh"),
            instruction=str(body.get("instruction") or ""),
        )
        return {"prompt": text, "model": model, "images": len(references)}
    except image_engine.ImageGenError as error:
        raise HTTPException(status_code=400, detail={"message": str(error), "code": error.code}) from error


# --- prompt-template library --------------------------------------------


@router.get("/templates/sources")
async def template_sources() -> dict[str, Any]:
    return {"sources": template_library.source_meta()}


@router.get("/templates/list")
async def template_list(
    source: str = Query(template_library.DEFAULT_SOURCE),
    q: str = Query(""),
    category: str = Query(""),
    offset: int = Query(0, ge=0),
    limit: int = Query(60, ge=1, le=120),
) -> dict[str, Any]:
    try:
        return template_library.library().list(source, query=q, category=category, offset=offset, limit=limit)
    except template_library.TemplateError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/templates/sample")
async def template_sample(
    source: str = Query(template_library.DEFAULT_SOURCE),
    id: str = Query(...),
) -> dict[str, Any]:
    try:
        return {"case": template_library.library().sample(source, id)}
    except template_library.TemplateError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/templates/image/{source_id}/{case_id}")
async def template_image(source_id: str, case_id: str) -> Response:
    """Serve one case's reference image, fetched once and cached on disk.

    The image URL is resolved from the loaded case, so a request can never make
    the gateway fetch an arbitrary address.
    """
    try:
        data, mime = await template_library.library().image(source_id, case_id)
    except template_library.TemplateError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return Response(
        content=data,
        media_type=mime,
        headers={"cache-control": "private, max-age=604800"},
    )


@router.get("/templates/image/{source_id}/{case_id}/inline")
async def template_image_inline(source_id: str, case_id: str) -> dict[str, Any]:
    """Same bytes as the file route, as a data URL — the renderer's only read path."""
    try:
        data, mime = await template_library.library().image(source_id, case_id)
    except template_library.TemplateError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return {
        "sourceId": source_id,
        "id": case_id,
        "mime": mime,
        "data_url": f"data:{mime};base64,{base64.b64encode(data).decode()}",
    }


@router.post("/templates/refresh")
async def template_refresh(body: dict[str, Any]) -> dict[str, Any]:
    try:
        return await template_library.library().refresh(str(body.get("source") or template_library.DEFAULT_SOURCE))
    except template_library.TemplateError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/templates/favorites")
async def template_favorites() -> dict[str, Any]:
    return {"favorites": template_library.library().favorites()}


@router.post("/templates/favorites")
async def template_favorite_add(body: dict[str, Any]) -> dict[str, Any]:
    source = str(body.get("source") or template_library.DEFAULT_SOURCE)
    case_id = str(body.get("id") or "")
    if not case_id:
        raise HTTPException(status_code=400, detail="id 不能为空")
    try:
        return template_library.library().add_favorite(source, case_id)
    except template_library.TemplateError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.delete("/templates/favorites/{source_id}/{case_id}")
async def template_favorite_remove(source_id: str, case_id: str) -> dict[str, Any]:
    return template_library.library().remove_favorite(source_id, case_id)
