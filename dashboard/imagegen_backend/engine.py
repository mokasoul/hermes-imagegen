"""Upstream image generation — OpenAI-compatible images API + model discovery.

Python port of the response/request handling in the upstream DSH plugin's
``src/engine.ts`` (Apache-2.0), narrowed to what this port supports today:

- ``images/generations`` (text-to-image) and ``images/edits`` (image-to-image,
  with optional PNG mask) for the OpenAI-compatible families
- the family-specific body shaping for gpt-image / dall-e / grok / nanobanana /
  seedream / sensenova / zhipu (see ``models.effective_params``)
- async provider tasks: a ``task_id`` answer is polled on ``/tasks/{id}``
- result normalisation from ``b64_json`` or ``url`` (same-origin key rule)

Not ported yet (fails loudly with a stable code, never silently degrades):
the DashScope-native Qwen contract, MiniMax's native contract, subscription
channels, and the Chat-Completions transport.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

import httpx

from . import models

UPSTREAM_TIMEOUT_S = 240.0
IMAGE_FETCH_TIMEOUT_S = 60.0
ASYNC_POLL_MAX_S = 240.0
ASYNC_POLL_REQUEST_TIMEOUT_S = 30.0
MAX_EDIT_IMAGE_BYTES = 10 * 1024 * 1024
MAX_RESULT_BYTES = 40 * 1024 * 1024

ASYNC_PENDING = frozenset({"submitted", "pending", "processing", "running", "in_progress", "queued"})
ASYNC_DONE = frozenset({"completed", "succeeded", "succeed", "success", "done"})
ASYNC_FAILED = frozenset({"failed", "failure", "cancelled", "canceled", "error"})

_MIME_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}
_EXT_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}


class ImageGenError(Exception):
    """Upstream/config failure with a stable wire code for the UI."""

    def __init__(
        self,
        message: str,
        code: str = "generate-failed",
        status: int | None = None,
        *,
        no_retry: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        # HTTP status when the failure came from a response; drives retry decisions.
        self.status = status
        # Set once an async task has been accepted upstream: retrying the call
        # would resubmit (and re-bill) work the provider is already running.
        self.no_retry = no_retry


# Responses worth a transparent retry: the request never reached the model
# (gateway hiccup, origin overload, Cloudflare edge error) or the gateway itself
# flagged temporary trouble. Auth, validation and content-policy rejections are
# final — retrying them only burns quota. Mirrors the upstream engine table.
TRANSIENT_STATUSES = frozenset({408, 429, 500, 502, 503, 504, 520, 522, 524})
RETRY_MAX_ATTEMPTS = 3
RETRY_BASE_DELAY_S = 1.5


async def with_transient_retry(run: Any, cancel: Any = None) -> Any:
    """One initial attempt plus up to two retries while the failure looks transient.

    The caller's cancellation always wins: a set cancel event rethrows the
    in-flight failure instead of sleeping past it.
    """
    for attempt in range(1, RETRY_MAX_ATTEMPTS + 1):
        try:
            return await run()
        except ImageGenError as error:
            cancelled = cancel is not None and cancel.is_set()
            retryable = not cancelled and not error.no_retry and (
                error.code == "upstream-unreachable"
                or (error.status is not None and error.status in TRANSIENT_STATUSES)
            )
            if not retryable or attempt >= RETRY_MAX_ATTEMPTS:
                raise
            await asyncio.sleep(RETRY_BASE_DELAY_S * attempt)
    raise AssertionError("unreachable")  # pragma: no cover


def _fan_out(settled: list[Any], requested: int) -> tuple[list[GeneratedImage], str | None]:
    """Flatten the parallel single-image calls behind one request.

    A surviving image keeps its place (the images are independent variations of
    the same prompt, so a compacted position is still a valid result); the
    request only fails when nothing succeeded. A partial result stays a success
    and names what failed, so the board can offer a retry for those positions.
    """
    images: list[GeneratedImage] = []
    failures: list[BaseException] = []
    for outcome in settled:
        if isinstance(outcome, asyncio.CancelledError):
            raise outcome
        if isinstance(outcome, BaseException):
            failures.append(outcome)
        else:
            images.extend(outcome)
    if not images:
        first = failures[0] if failures else None
        if isinstance(first, ImageGenError):
            raise first
        if first is not None:
            raise ImageGenError(f"上游并行请求全部失败：{first}", "generate-failed") from first
        raise ImageGenError("上游并行请求全部失败", "generate-failed")
    if failures:
        detail = str(failures[0]) if failures else "未知错误"
        return images, f"{len(failures)}/{requested} 张图片生成失败：{detail}"
    return images, None


@dataclass
class ReferenceImage:
    data: bytes
    mime: str = "image/png"

    @property
    def filename(self) -> str:
        return f"reference.{_MIME_EXT.get(self.mime, 'png')}"


@dataclass
class GeneratedImage:
    data: bytes
    mime: str
    revised_prompt: str | None = None


# --- small helpers -------------------------------------------------------


def sniff_image_mime(data: bytes) -> str | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def mime_from_extension(path: str) -> str | None:
    match = re.search(r"\.([a-z0-9]+)$", path, re.I)
    return _EXT_MIME.get(match.group(1).lower()) if match else None


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Width/height for PNG, GIF, WEBP and JPEG — enough to validate a mask."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        if data[12:16] == b"VP8X" and len(data) >= 30:
            width = int.from_bytes(data[24:27], "little") + 1
            height = int.from_bytes(data[27:30], "little") + 1
            return width, height
        return None
    if data[:3] == b"\xff\xd8\xff":
        index = 2
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                index += 2
                continue
            length = int.from_bytes(data[index + 2 : index + 4], "big")
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                height = int.from_bytes(data[index + 5 : index + 7], "big")
                width = int.from_bytes(data[index + 7 : index + 9], "big")
                return width, height
            index += 2 + length
    return None


def parse_data_url(value: str) -> tuple[bytes, str]:
    """Decode ``data:<mime>;base64,<payload>``; raises on anything else."""
    match = re.match(r"^data:([^;,]+)?(;base64)?,(.*)$", (value or "").strip(), re.S)
    if match is None or match.group(2) is None:
        raise ImageGenError("参考图片格式无效（需要 base64 data URL）", "edit-image-invalid")
    mime = match.group(1) or "application/octet-stream"
    try:
        data = base64.b64decode(match.group(3), validate=False)
    except (binascii.Error, ValueError) as error:
        raise ImageGenError("参考图片数据无法解码", "edit-image-invalid") from error
    return data, mime


def display_url(url: str) -> str:
    """origin+path only — a signed query string must never reach a message."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}" if parts.scheme else url


def _same_origin(a: str, b: str) -> bool:
    try:
        pa, pb = urlsplit(a), urlsplit(b)
    except ValueError:
        return False
    return (pa.scheme, pa.netloc) == (pb.scheme, pb.netloc)


def is_model_scope(base_url: str) -> bool:
    try:
        return urlsplit(base_url).hostname == "api-inference.modelscope.cn"
    except ValueError:
        return False


def download_timeout_of(channel: Mapping[str, Any]) -> float:
    """Per-channel result-download budget (seconds); 10–600, default 60."""
    value = channel.get("download_timeout_s")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 10 <= float(value) <= 600:
        return float(value)
    return IMAGE_FETCH_TIMEOUT_S


def normalize_base_url(channel: Mapping[str, Any]) -> str:
    base = str(channel.get("api_url") or "").strip().rstrip("/")
    if not base:
        raise ImageGenError("渠道未配置 API 地址", "config-missing")
    if channel.get("api_url_full") is True or not is_model_scope(base):
        return base
    # ModelScope documents the service origin; the OpenAI API lives under /v1.
    parts = urlsplit(base)
    if parts.path in ("", "/"):
        return f"{parts.scheme}://{parts.netloc}/v1"
    return base


def endpoint_url(channel: Mapping[str, Any], base_url: str, path: str) -> str:
    return base_url if channel.get("api_url_full") is True else f"{base_url}{path}"


def _api_key(channel: Mapping[str, Any]) -> str:
    from .settings import resolve_api_key

    key = resolve_api_key(channel)
    if not key:
        raise ImageGenError("该渠道未配置 API 密钥", "config-missing")
    return key


def _check_channel_ready(channel: Mapping[str, Any]) -> None:
    """Refuse channels we cannot address at all — never guess a URL shape."""
    if not str(channel.get("api_url") or "").strip():
        raise ImageGenError("渠道未配置 API 地址", "config-missing")


def _image_items(value: Any, depth: int = 0) -> list[dict[str, Any]]:
    """Collect every dict that carries an image payload, at any nesting depth."""
    if depth > 4 or value is None:
        return []
    if isinstance(value, list):
        out: list[dict[str, Any]] = []
        for item in value:
            out.extend(_image_items(item, depth + 1))
        return out
    if not isinstance(value, dict):
        return []
    if any(isinstance(value.get(k), str) and value.get(k).strip() for k in ("b64_json", "url")):
        return [value]
    for key in ("data", "images", "output", "result", "content"):
        if key in value:
            found = _image_items(value[key], depth + 1)
            if found:
                return found
    return []


def _error_message(payload: Any, status: int) -> str:
    if isinstance(payload, dict):
        for candidate in (
            payload.get("error"),
            payload.get("message"),
            (payload.get("error") or {}).get("message") if isinstance(payload.get("error"), dict) else None,
        ):
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return f"上游接口拒绝了请求（HTTP {status}）"


async def _read_job_text(client: httpx.AsyncClient, url: str, headers: Mapping[str, str]) -> dict[str, Any]:
    try:
        response = await client.get(url, headers=dict(headers))
    except httpx.TimeoutException as error:
        raise ImageGenError("上游异步任务轮询超时", "upstream-timeout") from error
    except httpx.HTTPError as error:
        raise ImageGenError(f"无法轮询上游异步任务：{error}", "upstream-unreachable") from error
    try:
        payload = response.json()
    except ValueError as error:
        raise ImageGenError(f"上游任务接口返回了非 JSON 响应（HTTP {response.status_code}）", "upstream-invalid") from error
    if response.status_code >= 400 or not isinstance(payload, dict):
        raise ImageGenError(_error_message(payload, response.status_code), "upstream-rejected", response.status_code)
    return payload


async def _poll_task(
    client: httpx.AsyncClient,
    channel: Mapping[str, Any],
    base_url: str,
    task_id: str,
    api_key: str,
    model_scope: bool,
) -> list[dict[str, Any]]:
    headers = {"authorization": f"Bearer {api_key}"}
    if model_scope:
        headers["X-ModelScope-Task-Type"] = "image_generation"
    poll_url = f"{base_url}/tasks/{task_id}"
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ASYNC_POLL_MAX_S
    delay = 1.0
    last_status = ""
    last_error = ""
    while loop.time() < deadline:
        try:
            payload = await _read_job_text(client, poll_url, headers)
        except ImageGenError as error:
            # Upstream has already accepted (and billed) this task: a hiccup on
            # the poll must not bubble out to the outer retry, which would
            # resubmit the whole generation. Keep polling the same task id.
            transient = error.code == "upstream-unreachable" or (
                error.status is not None and error.status in TRANSIENT_STATUSES
            )
            if not transient or loop.time() + delay >= deadline:
                raise ImageGenError(str(error), error.code, error.status, no_retry=True) from error
            last_error = str(error)
            await asyncio.sleep(delay)
            delay = min(delay * 1.6, 5.0)
            continue
        status = str(payload.get("task_status") or payload.get("status") or "").lower()
        if status:
            last_status = status
        if status in ASYNC_FAILED:
            raise ImageGenError(
                str(payload.get("message") or "上游异步任务失败"), "upstream-rejected", no_retry=True
            )
        items = _image_items(payload)
        if items and (status in ASYNC_DONE or status == ""):
            return items
        if status in ASYNC_DONE and not items:
            raise ImageGenError("上游异步任务已完成，但没有返回图片", "upstream-empty", no_retry=True)
        # A status word outside the known sets means "still working": providers
        # invent their own vocabulary, and aborting here would abandon a paid
        # task. The deadline is the bound; only explicit failure words fail early.
        await asyncio.sleep(delay)
        delay = min(delay * 1.6, 5.0)
    detail = f"，最后状态：{last_status}" if last_status else (f"，最后错误：{last_error}" if last_error else "")
    raise ImageGenError(f"上游异步任务轮询超时（240 秒{detail}）", "upstream-timeout", no_retry=True)


async def _normalize_item(
    client: httpx.AsyncClient,
    item: Mapping[str, Any],
    api_base: str,
    api_key: str,
    download_timeout: float = IMAGE_FETCH_TIMEOUT_S,
) -> GeneratedImage:
    revised = item.get("revised_prompt") if isinstance(item.get("revised_prompt"), str) else None
    b64 = item.get("b64_json")
    if isinstance(b64, str) and b64.strip():
        raw = b64.strip()
        if raw.startswith("data:"):
            data, _ = parse_data_url(raw)
        else:
            try:
                data = base64.b64decode(raw, validate=False)
            except (binascii.Error, ValueError) as error:
                raise ImageGenError("上游返回的 base64 图片无法解码", "upstream-invalid") from error
        return GeneratedImage(data=data, mime=sniff_image_mime(data) or "image/png", revised_prompt=revised)
    url = item.get("url")
    if not isinstance(url, str) or not url:
        raise ImageGenError("上游图片项既没有 b64_json 也没有 url", "upstream-invalid")
    if url.startswith("data:"):
        data, mime = parse_data_url(url)
        return GeneratedImage(data=data, mime=sniff_image_mime(data) or mime, revised_prompt=revised)
    # The channel key is only ever forwarded to the channel's own origin: a
    # relay that answers with a foreign image URL must not harvest the Bearer.
    headers = {"authorization": f"Bearer {api_key}"} if _same_origin(url, api_base) else {}
    try:
        response = await client.get(url, headers=headers, timeout=httpx.Timeout(download_timeout, connect=15.0))
    except httpx.TimeoutException as error:
        raise ImageGenError(f"下载上游结果图片超时（{download_timeout:g} 秒）", "upstream-timeout") from error
    except httpx.HTTPError as error:
        raise ImageGenError(f"无法下载上游结果图片：{error}", "upstream-unreachable") from error
    if response.status_code >= 400:
        raise ImageGenError(
            f"下载上游结果图片失败（HTTP {response.status_code}）", "upstream-invalid", response.status_code
        )
    data = response.content
    if len(data) > MAX_RESULT_BYTES:
        raise ImageGenError("上游返回的图片超过 40MB 上限", "upstream-invalid")
    mime = sniff_image_mime(data) or mime_from_extension(urlsplit(url).path) or response.headers.get("content-type", "image/png")
    return GeneratedImage(data=data, mime=mime.split(";")[0], revised_prompt=revised)


def _reference_extension(mime: str) -> str:
    return _MIME_EXT.get(mime, "png")


def _build_edit_body(
    channel: Mapping[str, Any],
    request: Mapping[str, Any],
    params: Mapping[str, Any],
    references: list[ReferenceImage],
    mask: bytes | None,
) -> tuple[dict[str, str], Any]:
    """Build the image-to-image request body for the resolved family."""
    family = models.model_family(params["model"])
    prompt = str(request.get("prompt") or "")
    if not references:
        raise ImageGenError("图生图需要上传参考图片", "edit-image-missing")
    primary = references[0]
    extras = references[1:5]

    if family == "grok":
        body = {
            "model": params["model"],
            "prompt": prompt,
            "image": {"url": f"data:{primary.mime};base64,{base64.b64encode(primary.data).decode()}", "type": "image_url"},
            "response_format": "b64_json",
        }
        if "aspect_ratio" in params:
            body["aspect_ratio"] = params["aspect_ratio"]
        return {"content-type": "application/json"}, body

    if family == "nanobanana":
        files = [("image", ("reference-0.png", primary.data, primary.mime))]
        form: dict[str, Any] = {"prompt": prompt, "model": params["model"]}
        if "aspect_ratio" in params:
            form["aspect_ratio"] = params["aspect_ratio"]
        if "image_size" in params:
            form["image_size"] = params["image_size"]
        return {}, (form, files)

    if family == "sensenova":
        urls = [f"data:{ref.mime};base64,{base64.b64encode(ref.data).decode()}" for ref in references[:5]]
        body = {"prompt": prompt, **params, "images": [{"image_url": url} for url in urls]}
        return {"content-type": "application/json"}, body

    if family == "seedream":
        urls = [f"data:{ref.mime};base64,{base64.b64encode(ref.data).decode()}" for ref in references[:5]]
        body = {"model": params["model"], "prompt": prompt, "image": urls}
        for key in ("size", "resolution", "watermark"):
            if key in params:
                body[key] = params[key]
        body["response_format"] = params.get("response_format", "b64_json")
        return {"content-type": "application/json"}, body

    # Generic OpenAI-compatible multipart edit upload.
    files = [
        ("image[]" if extras else "image", (f"reference-{index}.{_reference_extension(ref.mime)}", ref.data, ref.mime))
        for index, ref in enumerate(references[:5])
    ]
    form = {"prompt": prompt, "model": params["model"]}
    for key in ("size", "quality", "detail", "watermark", "prompt_extend"):
        if key in params:
            form[key] = str(params[key]).lower() if isinstance(params[key], bool) else str(params[key])
    if mask is not None:
        if sniff_image_mime(mask) != "image/png":
            raise ImageGenError("遮罩必须为 PNG 图片", "mask-invalid")
        mask_size = image_dimensions(mask)
        primary_size = image_dimensions(primary.data)
        if mask_size is None or primary_size is None or mask_size != primary_size:
            raise ImageGenError("遮罩尺寸必须与第一张参考图片一致", "mask-size-mismatch")
        files.append(("mask", ("mask.png", mask, "image/png")))
    return {}, (form, files)


async def _request_one(
    client: httpx.AsyncClient,
    channel: Mapping[str, Any],
    request: Mapping[str, Any],
    params: Mapping[str, Any],
    references: list[ReferenceImage],
    mask: bytes | None,
) -> list[GeneratedImage]:
    base_url = normalize_base_url(channel)
    api_key = _api_key(channel)
    model = str(params["model"])
    mode = str(request.get("mode") or "generate")
    model_scope = is_model_scope(base_url)
    headers: dict[str, str] = {"authorization": f"Bearer {api_key}"}
    if model_scope:
        headers["X-ModelScope-Async-Mode"] = "true"

    if mode == "edit":
        extra_headers, payload = _build_edit_body(channel, request, params, references, mask)
        headers.update(extra_headers)
        path = "/images/generations" if models.model_family(model) == "seedream" else "/images/edits"
    else:
        headers["content-type"] = "application/json"
        payload = {"prompt": str(request.get("prompt") or ""), **params}
        path = "/images/generations"

    url = endpoint_url(channel, base_url, path)
    try:
        if isinstance(payload, dict):
            response = await client.post(url, headers=headers, json=payload)
        else:
            form, files = payload
            response = await client.post(url, headers=headers, data=form, files=files)
    except httpx.TimeoutException as error:
        raise ImageGenError("上游接口响应超时（240 秒）", "upstream-timeout") from error
    except httpx.HTTPError as error:
        raise ImageGenError(f"无法连接上游接口：{error} · 请求地址 {display_url(url)}", "upstream-unreachable") from error

    text = response.text
    try:
        body = json.loads(text) if text.strip() else None
    except ValueError as error:
        raise ImageGenError(f"上游接口返回了非 JSON 响应（HTTP {response.status_code}）", "upstream-invalid") from error
    if response.status_code >= 400 or not isinstance(body, dict):
        raise ImageGenError(
            f"{_error_message(body, response.status_code)}（HTTP {response.status_code} · {display_url(url)}）",
            "upstream-rejected",
            response.status_code,
        )

    task_id = body.get("task_id")
    if isinstance(task_id, str) and task_id.strip():
        items = await _poll_task(client, channel, base_url, task_id.strip(), api_key, model_scope)
        return [await _normalize_item(client, item, base_url, api_key, download_timeout_of(channel)) for item in items]

    records = body.get("data")
    if not isinstance(records, list):
        raise ImageGenError("上游响应缺少 data 数组", "upstream-invalid")
    if not records:
        raise ImageGenError("上游返回了 0 张图片", "upstream-empty")

    async_entries = [r for r in records if isinstance(r, dict) and isinstance(r.get("task_id"), str) and r["task_id"].strip()]
    if async_entries:
        collected: list[dict[str, Any]] = []
        for entry in async_entries:
            collected.extend(await _poll_task(client, channel, base_url, entry["task_id"].strip(), api_key, model_scope))
        if not collected:
            raise ImageGenError("上游异步任务完成但没有图片结果", "upstream-empty")
        return [await _normalize_item(client, item, base_url, api_key, download_timeout_of(channel)) for item in collected]

    items = _image_items(records)
    if not items:
        raise ImageGenError("上游响应里没有可识别的图片", "upstream-invalid")
    return [await _normalize_item(client, item, base_url, api_key, download_timeout_of(channel)) for item in items]


# --- Chat Completions helpers (ported from the upstream engine) -----------

_THINK_BLOCK = re.compile(r"<think>[\s\S]*?</think>", re.I)
_THINK_OPEN = re.compile(r"<think>", re.I)
_MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\((?:<)?(https?://[^)\s>]+|data:image/[^)\s>]+)(?:>)?\)", re.I)
_HTML_IMAGE = re.compile(r"<img\b[^>]*\bsrc\s*=\s*[\"']([^\"']+)[\"'][^>]*>", re.I)
_BARE_URL = re.compile(r"(?:https?://|data:image/)[^\s\"'<>)\]}]+", re.I)
_IMAGE_URL_SIGNAL = re.compile(
    r"(?:image|img|photo|picture|cdn|blob|cloudinary|googleusercontent|oaidalle|replicate|fal\.media|files\.oai)"
)


def _strip_chat_reasoning(value: str) -> str:
    """Drop `<think>…</think>` blocks — a gateway's reasoning must not be scanned for URLs."""
    without_closed = _THINK_BLOCK.sub("", value)
    dangling = _THINK_OPEN.search(without_closed)
    return (without_closed[: dangling.start()] if dangling else without_closed).strip()


def _is_likely_image_url(value: str) -> bool:
    if re.match(r"^data:image/[a-z0-9.+-]+;base64,", value, re.I):
        return True
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False
    if mime_from_extension(parts.path) is not None:
        return True
    signal = f"{parts.hostname or ''}{parts.path}{parts.query}".lower()
    return _IMAGE_URL_SIGNAL.search(signal) is not None


def _parse_embedded_json(value: str) -> Any:
    """A complete or fenced JSON value embedded in chat text."""
    cleaned = re.sub(r"^```(?:json)?", "", _strip_chat_reasoning(value), flags=re.I)
    cleaned = re.sub(r"```$", "", cleaned).strip()
    if not cleaned:
        return None
    attempts = [cleaned]
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start >= 0 and end > start:
        attempts.append(cleaned[start : end + 1])
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start >= 0 and end > start:
        attempts.append(cleaned[start : end + 1])
    for attempt in attempts:
        try:
            parsed = json.loads(attempt)
        except ValueError:
            continue
        if isinstance(parsed, (dict, list)):
            return parsed
    return None


def _collect_chat_image_items(value: Any, out: list[dict[str, Any]], seen: set[int], depth: int = 0) -> None:
    if depth > 6 or value is None:
        return
    if isinstance(value, str):
        text = _strip_chat_reasoning(value)
        parsed = _parse_embedded_json(text)
        if parsed is not None:
            _collect_chat_image_items(parsed, out, seen, depth + 1)
        for match in _MARKDOWN_IMAGE.finditer(text):
            out.append({"url": match.group(1)})
        for match in _HTML_IMAGE.finditer(text):
            out.append({"url": match.group(1)})
        for raw in _BARE_URL.findall(text):
            url = raw.rstrip(".,;:!?")
            if _is_likely_image_url(url):
                out.append({"url": url})
        return
    if isinstance(value, list):
        for item in value:
            _collect_chat_image_items(item, out, seen, depth + 1)
        return
    if not isinstance(value, dict):
        return
    marker = id(value)
    if marker in seen:
        return
    seen.add(marker)
    if isinstance(value.get("b64_json"), str) and value["b64_json"].strip():
        out.append({"b64_json": value["b64_json"]})
    for key in ("url", "image_url", "image", "output_url"):
        item = value.get(key)
        if isinstance(item, str) and item:
            if key != "url" or _is_likely_image_url(item):
                out.append({"url": item})
        elif isinstance(item, dict):
            nested = item.get("url")
            if key != "url" and isinstance(nested, str) and nested:
                out.append({"url": nested})
            else:
                _collect_chat_image_items(item, out, seen, depth + 1)
    for key in ("images", "content", "message", "data", "output", "result"):
        if key in value:
            _collect_chat_image_items(value[key], out, seen, depth + 1)


def _chat_image_items_of(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every image candidate in one Chat Completions response, deduplicated."""
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for record in _data_records_of(payload):
        _collect_chat_image_items(record, out, seen)
    choices = payload.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            _collect_chat_image_items(choice.get("message"), out, seen)
            _collect_chat_image_items(choice.get("delta"), out, seen)
    if not out:
        _collect_chat_image_items(payload.get("images"), out, seen)
    unique: dict[str, dict[str, Any]] = {}
    for item in out:
        raw_url = item.get("url")
        raw_b64 = item.get("b64_json")
        url = raw_url.strip() if isinstance(raw_url, str) else ""
        b64 = raw_b64.strip() if isinstance(raw_b64, str) else ""
        key = f"url:{url}" if url else (f"b64:{b64[:128]}" if b64 else "")
        if key and key not in unique:
            unique[key] = item
    return list(unique.values())


def _data_records_of(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    if isinstance(data, list):
        return [entry for entry in data if isinstance(entry, dict)]
    if isinstance(data, dict):
        return [data]
    for key in ("images", "output"):
        value = payload.get(key)
        if isinstance(value, list):
            return [entry for entry in value if isinstance(entry, dict)]
    return []


def _chat_error_text_of(payload: Mapping[str, Any]) -> str:
    """User-visible text from a Chat response, used only when no image was found."""
    choices = payload.get("choices")
    first = choices[0] if isinstance(choices, list) and choices else None
    content = first.get("message", {}).get("content") if isinstance(first, dict) else None
    pieces: list[str] = []
    if isinstance(content, str):
        pieces.append(_strip_chat_reasoning(content))
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, str):
                pieces.append(_strip_chat_reasoning(part))
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                pieces.append(_strip_chat_reasoning(part["text"]))
    text = "\n".join(piece for piece in pieces if piece).strip()
    if text:
        return text
    message = payload.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    error = payload.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"].strip()
    return ""


async def _post_json(
    client: httpx.AsyncClient,
    url: str,
    api_key: str,
    body: Mapping[str, Any],
    extra_headers: Mapping[str, str] | None = None,
) -> tuple[int, Any, str]:
    """POST JSON with the shared error mapping; returns (status, payload, raw text)."""
    headers = {"authorization": f"Bearer {api_key}", "content-type": "application/json", "accept": "application/json"}
    headers.update(extra_headers or {})
    try:
        response = await client.post(url, headers=headers, json=dict(body))
    except httpx.TimeoutException as error:
        raise ImageGenError("上游接口响应超时（240 秒）", "upstream-timeout") from error
    except httpx.HTTPError as error:
        raise ImageGenError(f"无法连接上游接口：{error} · 请求地址 {display_url(url)}", "upstream-unreachable") from error
    text = response.text
    try:
        payload = json.loads(text) if text.strip() else None
    except ValueError as error:
        raise ImageGenError(f"上游接口返回了非 JSON 响应（HTTP {response.status_code}）", "upstream-invalid") from error
    return response.status_code, payload, text


def _data_url_of(reference: ReferenceImage) -> str:
    return f"data:{reference.mime};base64,{base64.b64encode(reference.data).decode()}"


async def _generate_qwen(
    client: httpx.AsyncClient,
    channel: Mapping[str, Any],
    base_url: str,
    api_key: str,
    request: Mapping[str, Any],
    references: list[ReferenceImage],
) -> list[GeneratedImage]:
    """DashScope native multimodal-generation: chat-style input, `宽*高` sizes."""
    model = models.wire_model(request)
    content: list[dict[str, Any]] = []
    if str(request.get("mode") or "generate") == "edit":
        if not references:
            raise ImageGenError("图生图需要上传参考图片", "edit-image-missing")
        content.append({"image": _data_url_of(references[0])})
    content.append({"text": str(request.get("prompt") or "")})

    batchable = models.is_versioned_qwen_image(model)
    count = models.clamp_count(request.get("n", 1)) if batchable else 1
    parameters: dict[str, Any] = {}
    size = models.qwen_size(model, str(request.get("size") or ""))
    if size is not None:
        parameters["size"] = size
    if count > 1:
        parameters["n"] = count
    body = {"model": model, "input": {"messages": [{"role": "user", "content": content}]}, "parameters": parameters}

    url = f"{base_url}/services/aigc/multimodal-generation/generation"
    status, payload, _ = await _post_json(client, url, api_key, body)
    if status >= 400 or not isinstance(payload, dict):
        raise ImageGenError(
            f"{_error_message(payload, status)}（HTTP {status} · {display_url(url)}）", "upstream-rejected", status
        )
    urls: list[str] = []
    output = payload.get("output")
    choices = output.get("choices") if isinstance(output, dict) else None
    for choice in choices or []:
        message = choice.get("message") if isinstance(choice, dict) else None
        items = message.get("content") if isinstance(message, dict) else None
        for item in items or []:
            if isinstance(item, dict) and isinstance(item.get("image"), str) and item["image"]:
                urls.append(item["image"])
    if not urls:
        raise ImageGenError("上游响应缺少图片内容", "upstream-empty")
    return [await _normalize_item(client, {"url": url}, base_url, api_key, download_timeout_of(channel)) for url in urls]


async def _generate_minimax(
    client: httpx.AsyncClient,
    channel: Mapping[str, Any],
    base_url: str,
    api_key: str,
    request: Mapping[str, Any],
    references: list[ReferenceImage],
) -> list[GeneratedImage]:
    """MiniMax native `/image_generation`: base64 answers, `base_resp` is the verdict."""
    model = models.wire_model(request)
    prompt = str(request.get("prompt") or "")
    limit = models.prompt_char_limit(model)
    if limit is not None and len(prompt) >= limit:
        raise ImageGenError(
            f"MiniMax image-01 要求提示词少于 {limit} 字符（当前 {len(prompt)}），请精简后重试", "prompt-too-long"
        )
    body: dict[str, Any] = {"model": model, "prompt": prompt, "response_format": "base64"}
    ratio = str(request.get("size") or "").strip()
    if ratio and ratio != "auto":
        if ratio not in models.MINIMAX_RATIOS:
            allowed = " / ".join(sorted(models.MINIMAX_RATIOS))
            raise ImageGenError(f"MiniMax image-01 不支持 {ratio} 宽高比，可选：{allowed}", "size-unsupported")
        body["aspect_ratio"] = ratio
    count = min(models.MINIMAX_MAX_N, models.clamp_count(request.get("n", 1)))
    if count > 1:
        body["n"] = count
    if str(request.get("mode") or "generate") == "edit":
        if not references:
            raise ImageGenError("图生图需要上传参考图片", "edit-image-missing")
        # image-01 takes exactly one character reference per request.
        body["subject_reference"] = [{"type": "character", "image_file": _data_url_of(references[0])}]

    url = f"{base_url}/image_generation"
    status, payload, _ = await _post_json(client, url, api_key, body)
    if not isinstance(payload, dict):
        raise ImageGenError(
            f"{_error_message(payload, status)}（HTTP {status} · {display_url(url)}）", "upstream-rejected", status
        )
    base_resp = payload.get("base_resp")
    if isinstance(base_resp, dict):
        code = base_resp.get("status_code")
        if isinstance(code, int) and code != 0:
            message = base_resp.get("status_msg") or "unknown error"
            raise ImageGenError(
                f"MiniMax 拒绝请求（{code}）：{message}",
                "upstream-unauthorized" if code in (1004, 2049) else "upstream-rejected",
            )
    if status >= 400:
        raise ImageGenError(
            f"{_error_message(payload, status)}（HTTP {status} · {display_url(url)}）", "upstream-rejected", status
        )

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    raw_b64 = [item for item in (data.get("image_base64") or []) if isinstance(item, str) and item.strip()]
    raw_urls = [item for item in (data.get("image_urls") or []) if isinstance(item, str) and item]
    if not raw_b64 and not raw_urls:
        raise ImageGenError("上游响应缺少图片内容", "upstream-empty")

    images: list[GeneratedImage] = []
    for raw in raw_b64:
        encoded = raw.strip()
        if encoded.startswith("data:"):
            encoded = encoded.split(",", 1)[-1]
        try:
            block = base64.b64decode(encoded, validate=False)
        except (binascii.Error, ValueError) as error:
            raise ImageGenError("MiniMax 返回的 base64 图片无法解码", "upstream-invalid") from error
        images.append(GeneratedImage(data=block, mime=sniff_image_mime(block) or "image/jpeg"))
    for raw in raw_urls:
        images.append(await _normalize_item(client, {"url": raw}, base_url, api_key, download_timeout_of(channel)))
    return images


def _chat_endpoint(channel: Mapping[str, Any], base_url: str) -> str:
    if models.is_chat_completions_url(base_url) or channel.get("api_url_full") is True:
        return base_url
    return f"{base_url}/chat/completions"


async def _generate_chat(
    client: httpx.AsyncClient,
    channel: Mapping[str, Any],
    base_url: str,
    api_key: str,
    request: Mapping[str, Any],
    references: list[ReferenceImage],
) -> list[GeneratedImage]:
    """One Chat Completions image request; the count is satisfied by parallel calls."""
    prompt = str(request.get("prompt") or "")
    if str(request.get("mode") or "generate") == "edit":
        if not references:
            raise ImageGenError("图生图需要上传参考图片", "edit-image-missing")
        content: Any = [{"type": "text", "text": prompt}] + [
            {"type": "image_url", "image_url": {"url": _data_url_of(reference)}} for reference in references[:5]
        ]
    else:
        content = prompt
    url = _chat_endpoint(channel, base_url)
    body = {"model": models.wire_model(request), "messages": [{"role": "user", "content": content}], "stream": False}
    status, payload, _ = await _post_json(client, url, api_key, body)
    if status >= 400 or not isinstance(payload, dict):
        raise ImageGenError(
            f"{_error_message(payload, status)}（HTTP {status} · {display_url(url)}）", "upstream-rejected", status
        )
    items = _chat_image_items_of(payload)
    if not items:
        detail = _chat_error_text_of(payload)[:1200] or "Chat Completions 响应中没有可用的图片结果"
        raise ImageGenError(detail, "upstream-empty")
    return [await _normalize_item(client, item, base_url, api_key, download_timeout_of(channel)) for item in items]


async def generate(
    channel: Mapping[str, Any],
    request: Mapping[str, Any],
    references: Iterable[ReferenceImage] | None = None,
    mask: bytes | None = None,
    cancel: asyncio.Event | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    outcome: dict[str, Any] | None = None,
) -> list[GeneratedImage]:
    """Run one generation request through the protocol + family the channel implies.

    ``outcome`` receives ``{"partial_error": str | None}``: a fan-out batch that
    lost some of its parallel requests still returns its surviving images and
    names the loss there, instead of failing the whole request.
    """
    refs = list(references or [])
    model = models.wire_model(request)
    _check_channel_ready(channel)
    prompt = str(request.get("prompt") or "").strip()
    if not prompt:
        raise ImageGenError("提示词不能为空", "prompt-missing")
    limit = models.prompt_char_limit(model)
    if limit is not None and len(prompt) >= limit:
        raise ImageGenError(f"该模型提示词上限为 {limit} 个字符", "prompt-too-long")
    for ref in refs:
        if len(ref.data) > MAX_EDIT_IMAGE_BYTES:
            raise ImageGenError("参考图片超过 10MB 上限", "edit-image-too-large")

    base_url = normalize_base_url(channel)
    api_key = _api_key(channel)
    protocol = models.resolve_channel_protocol(channel)
    family = models.model_family(model)
    count = models.effective_count(request)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(UPSTREAM_TIMEOUT_S, connect=30.0),
        follow_redirects=True,
        transport=transport,
    ) as client:

        def guard() -> None:
            if cancel is not None and cancel.is_set():
                raise ImageGenError("任务已取消", "cancelled")

        if protocol == "chat-completions":
            # Chat has no batch parameter: one call per image.
            async def chat_one() -> list[GeneratedImage]:
                guard()
                return await with_transient_retry(
                    lambda: _generate_chat(client, channel, base_url, api_key, request, refs), cancel
                )

            settled = await asyncio.gather(*(chat_one() for _ in range(count)), return_exceptions=True)
            images, partial_error = _fan_out(settled, count)
        elif family == "qwen" and not is_model_scope(base_url):
            # ModelScope serves Qwen through the OpenAI shape, so only the
            # native DashScope host takes this branch (as upstream does).
            guard()
            images = list(
                await with_transient_retry(
                    lambda: _generate_qwen(client, channel, base_url, api_key, request, refs), cancel
                )
            )
            partial_error = None
        elif family == "minimax":
            guard()
            images = list(
                await with_transient_retry(
                    lambda: _generate_minimax(client, channel, base_url, api_key, request, refs), cancel
                )
            )
            partial_error = None
        else:
            params = models.effective_params(request, channel)

            async def images_one() -> list[GeneratedImage]:
                guard()
                return await with_transient_retry(
                    lambda: _request_one(client, channel, request, params, refs, mask), cancel
                )

            settled = await asyncio.gather(*(images_one() for _ in range(count)), return_exceptions=True)
            images, partial_error = _fan_out(settled, count)
    if outcome is not None:
        outcome["partial_error"] = partial_error
    return images


async def detect_models(
    channel: Mapping[str, Any],
    api_key: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Probe ``<base>/models`` and ``<base>/models?kind=image``.

    Filtered gateways (magpie-style) only expose image models through the
    ``kind=image`` list, while OpenAI-compatible gateways ignore the parameter —
    so probing both is purely additive, never a downgrade.
    """
    base_url = normalize_base_url(channel)
    key = api_key or _api_key(channel)
    headers = {"authorization": f"Bearer {key}"}
    urls = [f"{base_url}/models", f"{base_url}/models?kind=image"]
    seen: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=15.0), follow_redirects=True, transport=transport) as client:
        for url in urls:
            try:
                response = await client.get(url, headers=headers)
            except httpx.HTTPError as error:
                errors.append(f"{display_url(url)}: {error}")
                continue
            if response.status_code >= 400:
                errors.append(f"{display_url(url)}: HTTP {response.status_code}")
                continue
            try:
                payload = response.json()
            except ValueError:
                errors.append(f"{display_url(url)}: 非 JSON 响应")
                continue
            for entry in _model_entries(payload):
                mid = str(entry.get("id") or "").strip()
                if not mid or mid in seen:
                    continue
                describes_image = _entry_marks_image(entry)
                info = models.describe_model(mid)
                if not describes_image and not models.is_likely_image_model_id(mid):
                    continue
                seen[mid] = {
                    "id": mid,
                    "family": info["family"],
                    "label": info["label"],
                    "labelZh": info["labelZh"],
                    "known": info["known"],
                    "supportsEdit": info["supportsEdit"],
                    "supportsAspectRatio": info["supportsAspectRatio"],
                    "qualityTiers": info["qualityTiers"],
                }
    if not seen:
        detail = "；".join(errors) if errors else "接口未返回任何模型"
        raise ImageGenError(f"未探测到生图模型（{detail}）", "detect-empty")
    return {"models": sorted(seen.values(), key=lambda m: m["id"]), "probed": [display_url(u) for u in urls], "errors": errors}


def _model_entries(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        for key in ("data", "models", "items"):
            if isinstance(payload.get(key), list):
                return [item for item in payload[key] if isinstance(item, dict)]
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _entry_marks_image(entry: Mapping[str, Any]) -> bool:
    """Recognise ``kind`` / ``supported_endpoint_types`` / nested modalities."""
    kind = entry.get("kind")
    if isinstance(kind, str) and kind.lower() == "image":
        return True
    endpoints = entry.get("supported_endpoint_types")
    if isinstance(endpoints, list) and any("image" in str(e).lower() for e in endpoints):
        return True
    architecture = entry.get("architecture")
    if isinstance(architecture, dict):
        output = architecture.get("output_modalities") or architecture.get("modality")
        if isinstance(output, str) and "image" in output.lower():
            return True
        if isinstance(output, list) and any("image" in str(m).lower() for m in output):
            return True
    return False
