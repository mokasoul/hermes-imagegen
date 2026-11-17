"""Opt-in live check for the hermes-imagegen backend — spends real upstream credit.

Drives the plugin router in-process over an ASGI transport, exactly the way the
desktop half talks to it: channel -> detect models -> job -> inline image ->
history. Two properties this script exists to protect:

- the channel stores only ``api_key_env``; the key is read from the process
  environment and never written to settings.json;
- the data root is a fresh temp dir, so a live check never pollutes the real
  ``~/.hermes/imagegen`` history.

Never runs in CI — it needs a real endpoint and key:

    export IMAGEGEN_LIVE_API_URL=https://your-relay.example.com/v1
    export IMAGEGEN_LIVE_KEY_ENV=MY_IMAGE_API_KEY   # ambient env var holding the key
    export MY_IMAGE_API_KEY=sk-…
    python scripts/live_check.py

Optional: IMAGEGEN_LIVE_MODEL (default: first detected id containing "image"),
IMAGEGEN_LIVE_PROMPT, IMAGEGEN_LIVE_SIZE (default 1:1), IMAGEGEN_LIVE_QUALITY
(default 1k).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "dashboard"))
os.environ["HERMES_IMAGEGEN_DATA"] = tempfile.mkdtemp(prefix="hermes-imagegen-live-")

import httpx  # noqa: E402
import plugin_api  # noqa: E402
from fastapi import FastAPI  # noqa: E402

PREFIX = "/api/plugins/hermes-imagegen"


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} 未设置 —— 见脚本文档头的用法")
    return value


def build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(plugin_api.router, prefix=PREFIX)
    return app


async def main() -> int:
    api_url = _required("IMAGEGEN_LIVE_API_URL")
    key_env = _required("IMAGEGEN_LIVE_KEY_ENV")
    raw_key = os.environ.get(key_env, "").strip()
    if not raw_key:
        raise SystemExit(f"环境变量 {key_env} 为空")
    prompt = os.environ.get("IMAGEGEN_LIVE_PROMPT", "一只橘猫坐在窗台上，午后侧光，胶片质感")
    size = os.environ.get("IMAGEGEN_LIVE_SIZE", "1:1")
    quality = os.environ.get("IMAGEGEN_LIVE_QUALITY", "1k")

    app = build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=120.0) as client:

        async def call(path: str, method: str = "GET", body: dict | None = None, timeout: float = 120.0) -> tuple[int, Any]:
            response = await client.request(method, f"{PREFIX}{path}", json=body, timeout=timeout)
            try:
                return response.status_code, response.json()
            except ValueError:
                return response.status_code, response.text

        print("health:", await call("/health"))

        status, payload = await call(
            "/settings/channels",
            "POST",
            {"name": "live-check", "api_url": api_url, "api_key_env": key_env},
        )
        if status != 200:
            print("channel failed:", status, payload)
            return 1
        channel = payload["channel"]
        print("channel:", channel["id"], "| has_api_key:", channel["has_api_key"], "| from_env:", channel["api_key_from_env"])
        channel_id = channel["id"]

        status, detected = await call("/channels/detect", "POST", {"channel_id": channel_id, "save": True}, timeout=90)
        if status != 200:
            print("detect failed:", status, detected)
            return 1
        models = [m["id"] for m in detected["models"]]
        print("detect: 200 | models:", models)
        model = os.environ.get("IMAGEGEN_LIVE_MODEL", "").strip()
        if not model:
            model = next((m for m in models if "image" in m), models[0] if models else "")
        if not model:
            print("no image model detected")
            return 1
        print("model:", model)

        status, submitted = await call(
            "/jobs",
            "POST",
            {
                "channel_id": channel_id,
                "model": model,
                "mode": "generate",
                "prompt": prompt,
                "size": size,
                "quality": quality,
                "n": 1,
            },
            timeout=30,
        )
        if status != 200:
            print("job submit failed:", status, submitted)
            return 1
        job_id = submitted["job"]["id"]
        print("job submitted:", job_id)

        job = None
        for _ in range(150):
            _, payload = await call(f"/jobs/{job_id}")
            job = payload["job"]
            if job["status"] in ("succeeded", "failed", "cancelled", "lost"):
                break
            await asyncio.sleep(2)
        print("job:", job["status"], "| duration_ms:", job.get("duration_ms"), "| error:", job.get("error"))
        if job["status"] != "succeeded":
            return 1

        image = job["images"][0]
        _, inline = await call(f"/files/{image['id']}/inline")
        out = Path(os.environ["HERMES_IMAGEGEN_DATA"]) / "check.png"
        out.write_bytes(base64.b64decode(inline["data_url"].split(",", 1)[1]))
        print(f"image: {image['mime']} {image['size_bytes']} bytes -> {out}")

        _, history = await call("/history")
        record = history["items"][0]
        print("history total:", history["total"], "| latest:", record["status"], len(record["images"]), "image(s)")

        _, settings = await call("/settings")
        print("settings exposes the raw key?", raw_key in json.dumps(settings))
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
