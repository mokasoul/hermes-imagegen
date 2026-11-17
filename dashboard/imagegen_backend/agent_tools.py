"""Agent-facing tools: the same engine the workbench uses, callable from chat.

Ported from the upstream plugin's ``src/agent-image-tools.ts``. The whole point
is that a tool call and a UI click go through *one* code path: both submit a job
to ``runtime.runner()``, so a generation started in chat shows up in the
workbench history and vice versa.

Images come back as ``MEDIA:`` lines (Hermes renders them inline) plus their
stable ids, so a follow-up ``edit_image`` can reference them without re-uploading.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

import httpx

from . import engine, models, runtime

WAIT_TIMEOUT_S = 300.0
MAX_SOURCE_BYTES = 10 * 1024 * 1024


def _str(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


def _schema(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        params["required"] = required
    return {"name": name, "description": description, "parameters": params}


_CHANNEL_PROP = _str("渠道名或 id。省略则用设置的默认渠道。")
_MODEL_PROP = _str("模型 id（须在该渠道的模型列表里）。省略则用渠道默认模型。")
_SIZE_PROP = _str("宽高比，例如 1:1、16:9、9:16，或 auto。")
_QUALITY_PROP = _str("清晰度：auto / 1k / 2k / 4k。")
_COUNT_PROP = {"type": "integer", "description": "张数 1–4，默认 1。"}
_DETAIL_PROP = _str("上游 detail 取值（如 standard、high），可选。")
_WAIT_PROP = {"type": "boolean", "description": "是否等图出来再返回，默认 true；false 则只返回 task_id。"}

GENERATE_IMAGE_SCHEMA = _schema(
    "generate_image",
    "用已配置的生图渠道生成图片（文生图）。默认等到出图：图片会直接在对话里展示，并返回 image_id / generation_id，"
    "可直接传给 edit_image。设 wait_for_completion=false 则后台跑，用 get_image_generation_task 查。"
    "只允许使用本插件已配置渠道里的模型。",
    {
        "prompt": _str("详细的中文或英文画面描述。"),
        "channel": _CHANNEL_PROP,
        "model": _MODEL_PROP,
        "size": _SIZE_PROP,
        "quality": _QUALITY_PROP,
        "count": _COUNT_PROP,
        "detail": _DETAIL_PROP,
        "wait_for_completion": _WAIT_PROP,
    },
    required=["prompt"],
)

EDIT_IMAGE_SCHEMA = _schema(
    "edit_image",
    "对一张已有图片做图生图改图。source_image 接受：本插件返回的 image_id、本地文件绝对路径、"
    "http(s) 图片地址，或 data:image/... data URL。默认等到出图并返回新的 image_id。",
    {
        "prompt": _str("要怎么改这张图。"),
        "source_image": _str("源图：image_id / 本地路径 / http(s) URL / data URL。"),
        "channel": _CHANNEL_PROP,
        "model": _MODEL_PROP,
        "size": _SIZE_PROP,
        "quality": _QUALITY_PROP,
        "count": _COUNT_PROP,
        "detail": _DETAIL_PROP,
        "wait_for_completion": _WAIT_PROP,
    },
    required=["prompt", "source_image"],
)

GET_TASK_SCHEMA = _schema(
    "get_image_generation_task",
    "查询生图任务状态。完成时返回图片与 image_id（可直接用于 edit_image）。",
    {"task_id": _str("generate_image / edit_image 返回的 task_id。")},
    required=["task_id"],
)

CANCEL_TASK_SCHEMA = _schema(
    "cancel_image_generation_task",
    "取消排队中或运行中的生图任务。",
    {"task_id": _str("要取消的 task_id。")},
    required=["task_id"],
)

SCHEMAS = (GENERATE_IMAGE_SCHEMA, EDIT_IMAGE_SCHEMA, GET_TASK_SCHEMA, CANCEL_TASK_SCHEMA)


# ── resolution helpers ────────────────────────────────────────────────────


def _channel_for(name_or_id: Any) -> dict[str, Any]:
    settings = runtime.settings()
    channels = settings.channels
    if not channels:
        raise ValueError("还没有配置任何生图渠道：请先在 Hermes Desktop 的「生图 → 渠道」里添加渠道与密钥")
    wanted = str(name_or_id or "").strip()
    if not wanted:
        default_id = settings.public().get("default_channel_id", "")
        for channel in channels:
            if channel.get("id") == default_id:
                return channel
        return channels[0]
    for channel in channels:
        if channel.get("id") == wanted or str(channel.get("name") or "") == wanted:
            return channel
    names = "、".join(str(c.get("name") or c.get("id")) for c in channels)
    raise ValueError(f"没有名为 {wanted!r} 的渠道。已配置：{names}")


def _model_for(channel: dict[str, Any], requested: Any) -> str:
    wanted = str(requested or "").strip()
    available = [str(m) for m in (channel.get("models") or [])]
    if wanted:
        if available and wanted not in available:
            raise ValueError(f"渠道 {channel.get('name')} 没有模型 {wanted}。可选：{'、'.join(available)}")
        return wanted
    default = str(channel.get("default_model") or "").strip()
    if default:
        return default
    if available:
        return available[0]
    raise ValueError(f"渠道 {channel.get('name')} 还没有模型：请在「生图 → 渠道」里点「检测模型」")


async def _source_image_reference(value: Any) -> engine.ReferenceImage:
    """image_id | local path | http(s) URL | data URL → bytes + mime."""
    text = str(value or "").strip()
    if not text:
        raise ValueError("source_image 不能为空")
    if text.startswith("data:"):
        data, mime = engine.parse_data_url(text)
        return engine.ReferenceImage(data=data, mime=mime)
    if text.startswith(("http://", "https://")):
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=True) as client:
            response = await client.get(text)
        if response.status_code >= 400:
            raise ValueError(f"下载源图失败（HTTP {response.status_code}）")
        data = response.content
        if len(data) > MAX_SOURCE_BYTES:
            raise ValueError("源图超过 10MB 上限")
        return engine.ReferenceImage(data=data, mime=engine.sniff_image_mime(data) or "image/png")
    found = runtime.store().image_path(text)
    if found is not None and found[0].exists():
        data = found[0].read_bytes()
        return engine.ReferenceImage(data=data, mime=engine.sniff_image_mime(data) or found[1])
    path = Path(text).expanduser()
    if not path.is_file():
        raise ValueError(f"找不到源图：{text}（既不是 image_id，也不是存在的文件）")
    data = path.read_bytes()
    if len(data) > MAX_SOURCE_BYTES:
        raise ValueError("源图超过 10MB 上限")
    mime = engine.sniff_image_mime(data)
    if mime is None:
        raise ValueError("源图不是可识别的图片格式（PNG/JPEG/WEBP/GIF）")
    return engine.ReferenceImage(data=data, mime=mime)


def _image_media_lines(job: dict[str, Any]) -> list[str]:
    store = runtime.store()
    lines: list[str] = []
    for image in job.get("images") or []:
        found = store.image_path(str(image.get("id") or ""))
        if found is not None and found[0].exists():
            lines.append(f"MEDIA:{found[0]}")
    return lines


def _describe(job: dict[str, Any]) -> str:
    images = job.get("images") or []
    header = [f"状态：{job.get('status')}"]
    if job.get("model"):
        header.append(f"模型：{job['model']}")
    if isinstance(job.get("duration_ms"), int):
        header.append(f"耗时：{job['duration_ms'] / 1000:.1f} 秒")
    if job.get("channel_name"):
        header.append(f"渠道：{job['channel_name']}")
    lines = [" · ".join(header)]
    if job.get("id"):
        lines.append(f"task_id：{job['id']}")
    if job.get("error"):
        lines.append(f"错误：{job['error']}（{job.get('error_code') or 'unknown'}）")
    if images:
        ids = "、".join(str(image.get("id")) for image in images)
        lines.append(f"图片 id：{ids}")
        if job.get("generation_id"):
            lines.append(f"记录 id：{job['generation_id']}（可在「生图 → 历史」里找到）")
        lines.extend(_image_media_lines(job))
    return "\n".join(lines)


async def _wait_for(job_id: str) -> dict[str, Any]:
    runner = runtime.runner()
    waited = 0.0
    while waited < WAIT_TIMEOUT_S:
        job = runner.get(job_id)
        if job is None:
            return {"id": job_id, "status": "lost", "images": [], "error": "任务状态已失效（网关重启过）"}
        if job["status"] in ("succeeded", "failed", "cancelled", "lost"):
            return job
        await asyncio.sleep(1.0)
        waited += 1.0
    return {**(runner.get(job_id) or {"id": job_id}), "status": "timeout", "images": []}


def _submit(
    *,
    prompt: str,
    channel: dict[str, Any],
    model: str,
    mode: str,
    size: str,
    quality: str,
    count: int,
    detail: str,
    references: list[engine.ReferenceImage],
    wait: bool = True,  # accepted for symmetry with _common_args; the caller waits
) -> dict[str, Any]:
    payload = {
        "channel_id": channel["id"],
        "model": model,
        "mode": mode,
        "prompt": prompt,
        "size": size or "1:1",
        "quality": quality or "auto",
        "detail": detail,
        "n": models.clamp_count(count),
        "references": [engine._data_url_of(reference) for reference in references]
        if mode == "edit"
        else [],
    }
    return runtime.runner().submit(payload)


def _common_args(args: dict[str, Any]) -> dict[str, Any]:
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt 不能为空")
    channel = _channel_for(args.get("channel"))
    return {
        "prompt": prompt,
        "channel": channel,
        "model": _model_for(channel, args.get("model")),
        "size": str(args.get("size") or ""),
        "quality": str(args.get("quality") or ""),
        "count": args.get("count", 1),
        "detail": str(args.get("detail") or ""),
        "wait": bool(args.get("wait_for_completion", True)),
    }


# ── handlers ──────────────────────────────────────────────────────────────


async def handle_generate_image(args: dict[str, Any], **_kwargs: Any) -> str:
    common = _common_args(args)
    job = _submit(mode="generate", references=[], **common)
    if not common["wait"]:
        return f"已提交生图任务\ntask_id：{job['id']}\n用 get_image_generation_task 查询进度。"
    return _describe(await _wait_for(job["id"]))


async def handle_edit_image(args: dict[str, Any], **_kwargs: Any) -> str:
    common = _common_args(args)
    reference = await _source_image_reference(args.get("source_image"))
    job = _submit(mode="edit", references=[reference], **common)
    if not common["wait"]:
        return f"已提交改图任务\ntask_id：{job['id']}\n用 get_image_generation_task 查询进度。"
    return _describe(await _wait_for(job["id"]))


async def handle_get_image_generation_task(args: dict[str, Any], **_kwargs: Any) -> str:
    task_id = str(args.get("task_id") or "").strip()
    if not task_id:
        raise ValueError("task_id 不能为空")
    job = runtime.runner().get(task_id)
    if job is None:
        return f"找不到任务 {task_id}：任务状态只保存在网关进程内，重启后失效。历史记录请查「生图 → 历史」。"
    if job["status"] in ("queued", "running"):
        return f"任务 {task_id} 仍在 {job['status']}，稍后再查。"
    return _describe(job)


async def handle_cancel_image_generation_task(args: dict[str, Any], **_kwargs: Any) -> str:
    task_id = str(args.get("task_id") or "").strip()
    if not task_id:
        raise ValueError("task_id 不能为空")
    job = runtime.runner().cancel(task_id)
    if job is None:
        raise ValueError(f"找不到任务 {task_id}")
    return f"已请求取消任务 {task_id}（当前状态：{job['status']}）"


HANDLERS = {
    "generate_image": handle_generate_image,
    "edit_image": handle_edit_image,
    "get_image_generation_task": handle_get_image_generation_task,
    "cancel_image_generation_task": handle_cancel_image_generation_task,
}


def decode_reference(value: str) -> engine.ReferenceImage:  # pragma: no cover - helper for tests
    data, mime = engine.parse_data_url(value)
    return engine.ReferenceImage(data=data, mime=mime)


def as_data_url(data: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"
