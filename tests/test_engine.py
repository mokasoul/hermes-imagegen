"""Engine behaviour against a mock upstream — no network, no real credentials."""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest
from conftest import png_bytes, png_data_url

from imagegen_backend import engine


def _channel(**overrides):
    base = {"id": "c1", "name": "测试渠道", "api_url": "https://api.example.com/v1", "api_key": "sk-test-key"}
    base.update(overrides)
    return base


def _request(**overrides):
    base = {"mode": "generate", "model": "gpt-image-2", "prompt": "a cat", "size": "16:9", "quality": "2k", "detail": "", "n": 1}
    base.update(overrides)
    return base


def _run(coro):
    return asyncio.run(coro)


def test_text_to_image_sends_shaped_size_and_decodes_b64():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(png_bytes()).decode()}]})

    images = _run(engine.generate(_channel(), _request(), transport=httpx.MockTransport(handler)))
    assert seen["url"] == "https://api.example.com/v1/images/generations"
    assert seen["auth"] == "Bearer sk-test-key"
    assert seen["body"]["size"] == "2560x1440"
    assert seen["body"]["quality"] == "medium"
    assert len(images) == 1
    assert images[0].mime == "image/png"
    assert images[0].data == png_bytes()


def test_count_fans_out_parallel_requests():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(png_bytes()).decode()}]})

    images = _run(engine.generate(_channel(), _request(n=3), transport=httpx.MockTransport(handler)))
    assert len(calls) == 3
    assert len(images) == 3


def test_result_url_download_carries_key_only_on_same_origin():
    same_origin_auth = []
    foreign_auth = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"data": [{"url": "https://api.example.com/v1/files/a.png"}, {"url": "https://cdn.other-host.net/b.png"}]},
            )
        if request.url.host == "api.example.com":
            same_origin_auth.append(request.headers.get("authorization"))
        else:
            foreign_auth.append(request.headers.get("authorization"))
        return httpx.Response(200, content=png_bytes(), headers={"content-type": "image/png"})

    images = _run(engine.generate(_channel(), _request(), transport=httpx.MockTransport(handler)))
    assert len(images) == 2
    assert same_origin_auth == ["Bearer sk-test-key"]
    assert foreign_auth == [None]


def test_upstream_error_message_names_status_and_path():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "no such route"}})

    with pytest.raises(engine.ImageGenError) as caught:
        _run(engine.generate(_channel(), _request(), transport=httpx.MockTransport(handler)))
    assert caught.value.code == "upstream-rejected"
    assert "HTTP 404" in str(caught.value)
    assert "https://api.example.com/v1/images/generations" in str(caught.value)
    assert "sk-test-key" not in str(caught.value)


def test_missing_config_and_prompt_fail_fast():
    with pytest.raises(engine.ImageGenError) as caught:
        _run(engine.generate(_channel(api_key=""), _request()))
    assert caught.value.code == "config-missing"
    with pytest.raises(engine.ImageGenError) as caught:
        _run(engine.generate(_channel(), _request(prompt="   ")))
    assert caught.value.code == "prompt-missing"


def test_chat_completions_protocol_is_refused_not_guessed():
    channel = _channel(api_url="https://api.example.com/v1/chat/completions")
    with pytest.raises(engine.ImageGenError) as caught:
        _run(engine.generate(channel, _request()))
    assert caught.value.code == "protocol-unsupported"


def test_edit_upload_is_multipart_with_reference_and_mask():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["content_type"] = request.headers.get("content-type", "")
        body = request.content
        seen["has_image_field"] = b'name="image"' in body or b'name="image[]"' in body
        seen["has_mask_field"] = b'name="mask"' in body
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(png_bytes()).decode()}]})

    refs = [engine.ReferenceImage(data=png_bytes(4, 4), mime="image/png")]
    mask = png_bytes(4, 4)
    _run(
        engine.generate(
            _channel(), _request(mode="edit"), references=refs, mask=mask, transport=httpx.MockTransport(handler)
        )
    )
    assert seen["url"].endswith("/images/edits")
    assert seen["content_type"].startswith("multipart/form-data")
    assert seen["has_image_field"] is True
    assert seen["has_mask_field"] is True


def test_mask_size_mismatch_is_rejected_before_any_call():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        calls.append(request)
        return httpx.Response(500)

    refs = [engine.ReferenceImage(data=png_bytes(4, 4), mime="image/png")]
    with pytest.raises(engine.ImageGenError) as caught:
        _run(
            engine.generate(
                _channel(),
                _request(mode="edit"),
                references=refs,
                mask=png_bytes(8, 8),
                transport=httpx.MockTransport(handler),
            )
        )
    assert caught.value.code == "mask-size-mismatch"
    assert calls == []


def test_async_task_is_polled():
    polls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"task_id": "task-1"})
        polls.append(str(request.url))
        if len(polls) < 2:
            return httpx.Response(200, json={"task_status": "processing"})
        return httpx.Response(200, json={"task_status": "succeeded", "data": [{"b64_json": base64.b64encode(png_bytes()).decode()}]})

    images = _run(engine.generate(_channel(), _request(), transport=httpx.MockTransport(handler)))
    assert len(images) == 1
    assert polls[0] == "https://api.example.com/v1/tasks/task-1"


def test_detect_models_merges_filtered_and_full_lists():
    seen_urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        if "kind=image" in str(request.url):
            return httpx.Response(
                200,
                json={"data": [{"id": "gpt-image-2", "kind": "image"}, {"id": "a-random-image-model", "architecture": {"output_modalities": ["image"]}}]},
            )
        return httpx.Response(200, json={"data": [{"id": "gpt-image-2"}, {"id": "deepseek-chat"}]})

    result = _run(engine.detect_models(_channel(), transport=httpx.MockTransport(handler)))
    ids = [m["id"] for m in result["models"]]
    assert ids == ["a-random-image-model", "gpt-image-2"]
    assert "deepseek-chat" not in ids
    assert seen_urls == ["https://api.example.com/v1/models", "https://api.example.com/v1/models?kind=image"]


def test_detect_models_reports_empty_with_reason():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "deepseek-chat"}]})

    with pytest.raises(engine.ImageGenError) as caught:
        _run(engine.detect_models(_channel(), transport=httpx.MockTransport(handler)))
    assert caught.value.code == "detect-empty"
    assert "未探测到生图模型" in str(caught.value)


def test_data_url_helpers():
    data, mime = engine.parse_data_url(png_data_url(2, 3))
    assert mime == "image/png"
    assert engine.image_dimensions(data) == (2, 3)
    assert engine.sniff_image_mime(data) == "image/png"
    with pytest.raises(engine.ImageGenError):
        engine.parse_data_url("https://example.com/a.png")


def test_model_scope_base_url_gets_v1():
    channel = _channel(api_url="https://api-inference.modelscope.cn")
    assert engine.normalize_base_url(channel) == "https://api-inference.modelscope.cn/v1"
    assert engine.normalize_base_url(_channel(api_url="https://api.example.com/v1")) == "https://api.example.com/v1"
