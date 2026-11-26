"""Upstream reliability: transient retry, partial fan-out, poll safety.

Every case runs against ``httpx.MockTransport`` — no network, no credentials.
The retry/poll sleeps are shortened so the suite stays fast; the logic under
test (how many attempts, which failures are final, whether a paid task is
resubmitted) is untouched.
"""

from __future__ import annotations

import asyncio
import base64

import httpx
import pytest
from conftest import png_bytes

from imagegen_backend import engine, settings


def _channel(**overrides):
    base = {"id": "c1", "name": "测试渠道", "api_url": "https://api.example.com/v1", "api_key": "sk-test-key"}
    base.update(overrides)
    return base


def _request(**overrides):
    base = {"mode": "generate", "model": "gpt-image-2", "prompt": "a cat", "size": "1:1", "quality": "2k", "n": 1}
    base.update(overrides)
    return base


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    monkeypatch.setattr(engine, "RETRY_BASE_DELAY_S", 0.0)

    async def no_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)


def _image_response():
    return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(png_bytes()).decode()}]})


def test_transient_503_is_retried_once():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(503, json={"error": {"message": "gateway busy"}})
        return _image_response()

    images = _run(engine.generate(_channel(), _request(), transport=httpx.MockTransport(handler)))
    assert len(calls) == 2
    assert len(images) == 1


def test_auth_rejection_is_final_and_not_retried():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(401, json={"error": {"message": "invalid key"}})

    with pytest.raises(engine.ImageGenError) as caught:
        _run(engine.generate(_channel(), _request(), transport=httpx.MockTransport(handler)))
    assert caught.value.status == 401
    assert len(calls) == 1, "a 401 must not be retried (it would only burn quota)"


def test_retries_stop_after_the_third_attempt():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(429, json={"error": {"message": "slow down"}})

    with pytest.raises(engine.ImageGenError) as caught:
        _run(engine.generate(_channel(), _request(), transport=httpx.MockTransport(handler)))
    assert caught.value.status == 429
    assert len(calls) == engine.RETRY_MAX_ATTEMPTS


def test_partial_fan_out_keeps_surviving_images_and_names_the_loss():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(400, json={"error": {"message": "content policy"}})
        return _image_response()

    outcome: dict = {}
    images = _run(
        engine.generate(_channel(), _request(n=2), transport=httpx.MockTransport(handler), outcome=outcome)
    )
    assert len(calls) == 2
    assert len(images) == 1, "the surviving image must still come back"
    assert outcome["partial_error"].startswith("1/2 张图片生成失败")
    assert "content policy" in outcome["partial_error"]


def test_full_batch_failure_still_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "content policy"}})

    with pytest.raises(engine.ImageGenError) as caught:
        _run(engine.generate(_channel(), _request(n=2), transport=httpx.MockTransport(handler)))
    assert caught.value.status == 400


def test_poll_hiccup_does_not_resubmit_a_paid_task():
    submissions = []
    polls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/images/generations"):
            submissions.append(str(request.url))
            return httpx.Response(200, json={"task_id": "task-1"})
        polls.append(str(request.url))
        if len(polls) == 1:
            return httpx.Response(503, json={"error": {"message": "edge glitch"}})
        return httpx.Response(
            200, json={"status": "completed", "data": [{"b64_json": base64.b64encode(png_bytes()).decode()}]}
        )

    images = _run(engine.generate(_channel(), _request(), transport=httpx.MockTransport(handler)))
    assert len(submissions) == 1, "an accepted task must never be submitted twice"
    assert len(polls) == 2
    assert len(images) == 1


def test_poll_timeout_names_the_last_status():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/images/generations"):
            return httpx.Response(200, json={"task_id": "task-1"})
        return httpx.Response(200, json={"status": "queued-ish"})

    original_time = engine.asyncio.get_running_loop

    class Loop:
        """First two reads are inside the budget, then the deadline is past."""

        def __init__(self):
            self.reads = 0

        def time(self):
            self.reads += 1
            return 0.0 if self.reads <= 2 else engine.ASYNC_POLL_MAX_S * 10

    def fake_loop():
        return Loop()

    engine.asyncio.get_running_loop = fake_loop
    try:
        with pytest.raises(engine.ImageGenError) as caught:
            _run(engine.generate(_channel(), _request(), transport=httpx.MockTransport(handler)))
    finally:
        engine.asyncio.get_running_loop = original_time
    assert caught.value.code == "upstream-timeout"
    assert "最后状态：queued-ish" in str(caught.value)


# ── download budget ────────────────────────────────────────────────────────


def test_download_timeout_defaults_to_sixty_seconds():
    assert engine.download_timeout_of({}) == 60.0
    assert engine.download_timeout_of({"download_timeout_s": 120}) == 120.0
    # Out-of-range or junk values fall back instead of producing an unusable client.
    assert engine.download_timeout_of({"download_timeout_s": 5}) == 60.0
    assert engine.download_timeout_of({"download_timeout_s": 900}) == 60.0
    assert engine.download_timeout_of({"download_timeout_s": "nope"}) == 60.0


def test_channel_validates_and_exposes_the_download_budget():
    channel = settings.normalize_channel({"name": "渠道", "api_url": "https://api.example.com/v1"})
    assert channel["download_timeout_s"] == 60.0
    tuned = settings.normalize_channel(
        {"name": "渠道", "api_url": "https://api.example.com/v1", "download_timeout_s": 180}
    )
    assert tuned["download_timeout_s"] == 180.0
    assert settings.mask_channel(tuned)["download_timeout_s"] == 180.0
    for bad in (5, 601, "fast"):
        with pytest.raises(settings.SettingsError):
            settings.normalize_channel(
                {"name": "渠道", "api_url": "https://api.example.com/v1", "download_timeout_s": bad}
            )
