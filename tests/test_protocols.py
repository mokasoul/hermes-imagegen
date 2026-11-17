"""The three non-OpenAI-image protocols: Chat Completions, DashScope Qwen, MiniMax."""

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
    base = {"mode": "generate", "model": "gpt-image-2", "prompt": "a cat", "size": "1:1", "quality": "2k", "detail": "", "n": 1}
    base.update(overrides)
    return base


def _run(coro):
    return asyncio.run(coro)


def _b64png() -> str:
    return base64.b64encode(png_bytes()).decode()


# ── Chat Completions ──────────────────────────────────────────────────────


def test_chat_protocol_uses_chat_endpoint_and_simple_prompt_content():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "done ![img](https://cdn.example.com/a.png)"}}]},
        )

    def routed(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cdn.example.com":
            return httpx.Response(200, content=png_bytes(), headers={"content-type": "image/png"})
        return handler(request)

    images = _run(
        engine.generate(
            _channel(api_url="https://api.example.com/v1", protocol="chat-completions"),
            _request(model="some-image-chat-model"),
            transport=httpx.MockTransport(routed),
        )
    )
    assert seen["url"] == "https://api.example.com/v1/chat/completions"
    assert seen["body"]["messages"][0]["content"] == "a cat"
    assert seen["body"]["stream"] is False
    assert images[0].mime == "image/png"
    assert images[0].data == png_bytes()


def test_chat_protocol_auto_detected_from_a_full_chat_url():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"choices": [{"message": {"content": f"data:image/png;base64,{_b64png()}"}}]})

    _run(
        engine.generate(
            _channel(api_url="https://relay.example.com/v1/chat/completions"),
            _request(model="m"),
            transport=httpx.MockTransport(handler),
        )
    )
    assert seen["url"] == "https://relay.example.com/v1/chat/completions"


def test_chat_edit_sends_image_parts():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": f"![x](data:image/png;base64,{_b64png()})"}}]})

    _run(
        engine.generate(
            _channel(protocol="chat-completions"),
            _request(mode="edit"),
            references=[engine.ReferenceImage(data=png_bytes(2, 2), mime="image/png")],
            transport=httpx.MockTransport(handler),
        )
    )
    parts = seen["body"]["messages"][0]["content"]
    assert parts[0] == {"type": "text", "text": "a cat"}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_chat_extraction_ignores_reasoning_and_dedupes():
    payload = {
        "choices": [
            {
                "message": {
                    "content": (
                        "<think>internal https://img.thinking.example.com/x.png</think>\n"
                        "here: ![a](https://cdn.example.com/same.png) and again https://cdn.example.com/same.png"
                    )
                }
            }
        ]
    }
    items = engine._chat_image_items_of(payload)
    urls = [item["url"] for item in items]
    assert urls == ["https://cdn.example.com/same.png"]


def test_chat_image_object_and_b64_are_found():
    payload = {"choices": [{"message": {"content": [{"type": "image_url", "image_url": {"url": "https://x.example.com/i.png"}}, {"b64_json": _b64png()}]}}]}
    items = engine._chat_image_items_of(payload)
    assert len(items) == 2


def test_chat_without_images_surfaces_the_text_reason():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "I cannot draw that."}}]})

    with pytest.raises(engine.ImageGenError) as caught:
        _run(engine.generate(_channel(protocol="chat-completions"), _request(), transport=httpx.MockTransport(handler)))
    assert caught.value.code == "upstream-empty"
    assert "cannot draw" in str(caught.value)


def test_chat_count_fans_out_parallel_calls():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": f"![x](data:image/png;base64,{_b64png()})"}}]})

    images = _run(engine.generate(_channel(protocol="chat-completions"), _request(n=3), transport=httpx.MockTransport(handler)))
    assert len(calls) == 3
    assert len(images) == 3


# ── DashScope Qwen ────────────────────────────────────────────────────────


def test_qwen_native_body_and_url():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "dashscope-result.oss-cn-beijing.aliyuncs.com":
            return httpx.Response(200, content=png_bytes(), headers={"content-type": "image/png"})
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"output": {"choices": [{"message": {"content": [{"image": "https://dashscope-result.oss-cn-beijing.aliyuncs.com/a.png"}]}}]}},
        )

    images = _run(
        engine.generate(
            _channel(api_url="https://dashscope.aliyuncs.com/api/v1"),
            _request(model="qwen-image-2.0", size="1:1", n=3),
            transport=httpx.MockTransport(handler),
        )
    )
    assert seen["url"] == "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
    assert seen["body"]["parameters"] == {"size": "2048*2048", "n": 3}
    assert seen["body"]["input"]["messages"][0]["content"][0]["text"] == "a cat"
    assert images[0].data == png_bytes()


def test_qwen_classic_series_is_single_image_and_uses_classic_sizes():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"output": {"choices": [{"message": {"content": [{"image": f"data:image/png;base64,{_b64png()}"}]}}]}})

    _run(
        engine.generate(
            _channel(api_url="https://dashscope.aliyuncs.com/api/v1"),
            _request(model="qwen-image", size="16:9", n=4),
            transport=httpx.MockTransport(handler),
        )
    )
    assert seen["body"]["parameters"] == {"size": "1664*928"}


def test_qwen_edit_passes_the_reference_as_a_content_item():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"output": {"choices": [{"message": {"content": [{"image": f"data:image/png;base64,{_b64png()}"}]}}]}})

    _run(
        engine.generate(
            _channel(api_url="https://dashscope.aliyuncs.com/api/v1"),
            _request(model="qwen-image-2.0", mode="edit"),
            references=[engine.ReferenceImage(data=png_bytes(2, 2), mime="image/png")],
            transport=httpx.MockTransport(handler),
        )
    )
    first = seen["body"]["input"]["messages"][0]["content"][0]
    assert first["image"].startswith("data:image/png;base64,")


def test_qwen_on_modelscope_uses_the_openai_shape():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"data": [{"b64_json": _b64png()}]})

    _run(
        engine.generate(
            _channel(api_url="https://api-inference.modelscope.cn", api_key="sk-test-key"),
            _request(model="qwen-image-2.0"),
            transport=httpx.MockTransport(handler),
        )
    )
    assert seen["url"].endswith("/v1/images/generations")


# ── MiniMax ───────────────────────────────────────────────────────────────


def test_minimax_native_body_and_base64_results():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"base_resp": {"status_code": 0}, "data": {"image_base64": [_b64png()]}},
        )

    images = _run(
        engine.generate(
            _channel(api_url="https://api.minimax.chat/v1"),
            _request(model="image-01", size="16:9", n=3),
            transport=httpx.MockTransport(handler),
        )
    )
    assert seen["url"] == "https://api.minimax.chat/v1/image_generation"
    assert seen["body"]["response_format"] == "base64"
    assert seen["body"]["aspect_ratio"] == "16:9"
    assert seen["body"]["n"] == 3
    assert images[0].data == png_bytes()


def test_minimax_rejects_unsupported_ratio_before_the_call():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        calls.append(request)
        return httpx.Response(200, json={})

    with pytest.raises(engine.ImageGenError) as caught:
        _run(
            engine.generate(
                _channel(api_url="https://api.minimax.chat/v1"),
                _request(model="image-01", size="5:4"),
                transport=httpx.MockTransport(handler),
            )
        )
    assert caught.value.code == "size-unsupported"
    assert calls == []


def test_minimax_base_resp_error_is_the_verdict():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"base_resp": {"status_code": 1004, "status_msg": "invalid api key"}})

    with pytest.raises(engine.ImageGenError) as caught:
        _run(
            engine.generate(
                _channel(api_url="https://api.minimax.chat/v1"),
                _request(model="image-01"),
                transport=httpx.MockTransport(handler),
            )
        )
    assert caught.value.code == "upstream-unauthorized"
    assert "invalid api key" in str(caught.value)


def test_minimax_edit_sends_a_subject_reference():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"base_resp": {"status_code": 0}, "data": {"image_base64": [_b64png()]}})

    _run(
        engine.generate(
            _channel(api_url="https://api.minimax.chat/v1"),
            _request(model="image-01", mode="edit"),
            references=[engine.ReferenceImage(data=png_bytes(2, 2), mime="image/png")],
            transport=httpx.MockTransport(handler),
        )
    )
    reference = seen["body"]["subject_reference"][0]
    assert reference["type"] == "character"
    assert reference["image_file"].startswith("data:image/png;base64,")


def test_minimax_prompt_limit_is_enforced_locally():
    with pytest.raises(engine.ImageGenError) as caught:
        _run(engine.generate(_channel(api_url="https://api.minimax.chat/v1"), _request(model="image-01", prompt="x" * 1500)))
    assert caught.value.code == "prompt-too-long"


def test_minimax_url_results_are_downloaded():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"base_resp": {"status_code": 0}, "data": {"image_urls": ["https://api.minimax.chat/v1/files/a.png"]}},
            )
        return httpx.Response(200, content=png_bytes(), headers={"content-type": "image/png"})

    images = _run(
        engine.generate(
            _channel(api_url="https://api.minimax.chat/v1"),
            _request(model="image-01"),
            transport=httpx.MockTransport(handler),
        )
    )
    assert images[0].data == png_bytes()


def test_data_url_helper_roundtrip():
    reference = engine.ReferenceImage(data=png_bytes(1, 1), mime="image/png")
    assert engine._data_url_of(reference).startswith("data:image/png;base64,")
    assert engine.parse_data_url(png_data_url(1, 1))[1] == "image/png"
