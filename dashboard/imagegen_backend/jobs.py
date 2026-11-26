"""In-process generation queue.

A job is a real ``asyncio`` task in the gateway loop: image models take up to
four minutes, which no HTTP request should hold open. The UI POSTs a job, polls
``/jobs/<id>``, and the finished result is persisted into the history store so a
gateway restart loses only in-flight work, never a saved image.

Job state is deliberately in-memory (a reload of the plugin drops it); the
history table is the durable record. A job whose state vanished is answered
with ``status: "lost"`` instead of a 404 so the UI can say "this run is gone"
rather than "unknown id".
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping

from . import engine, models
from .settings import Settings, resolve_api_key

MAX_JOBS = 50
JOB_STATES = ("queued", "running", "succeeded", "failed", "cancelled", "lost")


class JobError(ValueError):
    """Rejected job submission."""


def _decode_reference(entry: Any) -> engine.ReferenceImage:
    if not isinstance(entry, str) or not entry.strip():
        raise JobError("参考图片格式无效")
    data, mime = engine.parse_data_url(entry)
    if len(data) > engine.MAX_EDIT_IMAGE_BYTES:
        raise JobError("参考图片超过 10MB 上限")
    return engine.ReferenceImage(data=data, mime=mime)


@dataclass
class Job:
    id: str
    mode: str
    channel_id: str
    channel_name: str
    model: str
    prompt: str
    params: dict[str, Any]
    references: list[engine.ReferenceImage]
    mask: bytes | None
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    duration_ms: int | None = None
    error: str | None = None
    error_code: str | None = None
    # Set when a fan-out batch lost some of its parallel requests: those
    # positions have no image even though the job itself succeeded.
    partial_error: str | None = None
    generation_id: str | None = None
    images: list[dict[str, Any]] = field(default_factory=list)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[Any] | None = None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "mode": self.mode,
            "channel_id": self.channel_id,
            "channel_name": self.channel_name,
            "model": self.model,
            "prompt": self.prompt,
            "params": self.params,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "error_code": self.error_code,
            "partial_error": self.partial_error,
            "generation_id": self.generation_id,
            "images": self.images,
        }


class JobRunner:
    def __init__(self, settings: Settings, store: Any) -> None:
        self.settings = settings
        self.store = store
        self._jobs: dict[str, Job] = {}

    # --- submission ------------------------------------------------------

    def submit(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        channel_id = str(payload.get("channel_id") or "").strip()
        channel = self.settings.channel(channel_id) if channel_id else None
        if channel is None:
            raise JobError("请先选择一个渠道")
        model = str(payload.get("model") or "").strip() or str(channel.get("default_model") or "")
        if not model:
            raise JobError("请选择一个模型")
        if not resolve_api_key(channel):
            raise JobError("该渠道未配置 API 密钥")
        mode = str(payload.get("mode") or "generate")
        if mode not in ("generate", "edit"):
            raise JobError("mode 必须是 generate 或 edit")
        prompt = str(payload.get("prompt") or "")
        if not prompt.strip():
            raise JobError("提示词不能为空")
        references = [_decode_reference(entry) for entry in (payload.get("references") or [])][:5]
        mask: bytes | None = None
        raw_mask = payload.get("mask")
        if isinstance(raw_mask, str) and raw_mask.strip():
            mask, mask_mime = engine.parse_data_url(raw_mask)
            if mask_mime != "image/png":
                raise JobError("遮罩必须为 PNG 图片")
        params = {
            "size": str(payload.get("size") or ""),
            "quality": str(payload.get("quality") or ""),
            "detail": str(payload.get("detail") or ""),
            "n": models.clamp_count(payload.get("n", 1)),
        }
        job = Job(
            id=uuid.uuid4().hex[:12],
            mode=mode,
            channel_id=channel["id"],
            channel_name=str(channel.get("name") or ""),
            model=model,
            prompt=prompt,
            params=params,
            references=references,
            mask=mask,
        )
        generation_id = self.store.create_generation(
            {
                "mode": mode,
                "channel_id": job.channel_id,
                "channel_name": job.channel_name,
                "model": model,
                "prompt": prompt,
                "params": params,
            }
        )
        job.generation_id = generation_id
        self._remember(job)
        job.task = asyncio.create_task(self._run(job, channel))
        return job.public()

    def _remember(self, job: Job) -> None:
        self._jobs[job.id] = job
        if len(self._jobs) > MAX_JOBS:
            finished = [j for j in self._jobs.values() if j.status in ("succeeded", "failed", "cancelled")]
            for stale in finished[: len(self._jobs) - MAX_JOBS]:
                self._jobs.pop(stale.id, None)

    # --- execution -------------------------------------------------------

    async def _run(self, job: Job, channel: Mapping[str, Any]) -> None:
        job.status = "running"
        job.started_at = time.time()
        request = {
            "mode": job.mode,
            "model": job.model,
            "prompt": job.prompt,
            **job.params,
        }
        outcome: dict[str, Any] = {}
        try:
            images = await engine.generate(
                channel,
                request,
                references=job.references,
                mask=job.mask,
                cancel=job.cancel_event,
                outcome=outcome,
            )
        except asyncio.CancelledError:
            job.status = "cancelled"
            job.duration_ms = int((time.time() - job.started_at) * 1000)
            job.error = "任务已取消"
            job.error_code = "cancelled"
            self.store.finish_generation(
                job.generation_id or "", status="cancelled", duration_ms=job.duration_ms, error=job.error, error_code="cancelled"
            )
            raise
        except engine.ImageGenError as error:
            job.status, job.error, job.error_code = "failed", str(error), error.code
            job.duration_ms = int((time.time() - job.started_at) * 1000)
            self.store.finish_generation(
                job.generation_id or "",
                status="failed",
                duration_ms=job.duration_ms,
                error=job.error,
                error_code=job.error_code,
            )
            return
        except Exception as error:  # pragma: no cover - unexpected upstream/parse bug
            job.status, job.error, job.error_code = "failed", f"生成失败：{error}", "generate-failed"
            job.duration_ms = int((time.time() - job.started_at) * 1000)
            self.store.finish_generation(
                job.generation_id or "",
                status="failed",
                duration_ms=job.duration_ms,
                error=job.error,
                error_code=job.error_code,
            )
            return

        job.status = "succeeded"
        job.partial_error = outcome.get("partial_error")
        job.duration_ms = int((time.time() - job.started_at) * 1000)
        record = self.store.finish_generation(
            job.generation_id or "",
            status="succeeded",
            duration_ms=job.duration_ms,
            images=[(image.data, image.mime, image.revised_prompt) for image in images],
        )
        job.images = record.get("images", [])

    # --- control ---------------------------------------------------------

    def get(self, job_id: str) -> dict[str, Any] | None:
        job = self._jobs.get(job_id)
        return job.public() if job is not None else None

    def cancel(self, job_id: str) -> dict[str, Any] | None:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        job.cancel_event.set()
        if job.task is not None and not job.task.done():
            job.task.cancel()
        return job.public()

    def jobs(self) -> list[dict[str, Any]]:
        return [job.public() for job in sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)]
