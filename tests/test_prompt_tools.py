"""Prompt helpers: enhance / polish / reverse via the channel's own chat model."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from conftest import png_bytes

from imagegen_backend import engine, prompt_tools


def _channel(**overrides):
    base = {
        "id": "c1",
        "name": "测试渠道",
        "api_url": "https://api.example.com/v1",
        "api_key": "sk-test-key",
        "models": ["gpt-image-2", "deepseek-chat"],
    }
    base.update(overrides)
    return base


def _run(coro):
    return asyncio.run(coro)


def _chat_reply(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}]})


def test_enhance_calls_chat_completions_with_the_upstream_system_prompt():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return _chat_reply("一只橘猫，85mm 定焦，f/1.8，浅景深，午后侧光")
        return _chat_reply("")

    text = _run(prompt_tools.enhance_prompt(_channel(), "deepseek-chat", "一只猫", transport=httpx.MockTransport(handler)))
    assert seen["url"] == "https://api.example.com/v1/chat/completions"
    assert seen["body"]["model"] == "deepseek-chat"
    assert seen["body"]["temperature"] == 0.7
    assert "expert image-prompt editor" in seen["body"]["messages"][0]["content"]
    assert seen["body"]["messages"][1] == {"role": "user", "content": "一只猫"}
    assert text.startswith("一只橘猫")


def test_polish_uses_the_zh_ecommerce_system_prompt():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _chat_reply("润色后的提示词")

    _run(prompt_tools.polish_prompt(_channel(), "deepseek-chat", "连衣裙", transport=httpx.MockTransport(handler)))
    assert "电商生图提示词编辑" in seen["body"]["messages"][0]["content"]
    assert seen["body"]["temperature"] == 0.35


def test_reverse_sends_images_and_quotes_the_rule_set():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _chat_reply("主体：一杯拿铁。85mm 定焦，浅景深，侧逆光。")

    images = [engine.ReferenceImage(data=png_bytes(2, 2), mime="image/png")]
    text = _run(prompt_tools.reverse_prompt(_channel(), "deepseek-chat", images, transport=httpx.MockTransport(handler)))
    system = seen["body"]["messages"][0]["content"]
    assert "图像逆向提示词工程师" in system
    assert "8K" in system and "不堆砌" in system
    parts = seen["body"]["messages"][1]["content"]
    assert parts[0]["type"] == "text"
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert text.startswith("主体")


def test_reverse_english_output_and_detail_length():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _chat_reply("A cup of latte, 85mm prime, shallow depth of field.")

    images = [engine.ReferenceImage(data=png_bytes(2, 2), mime="image/png")]
    _run(
        prompt_tools.reverse_prompt(
            _channel(), "deepseek-chat", images, detail="brief", output="en", instruction="focus on the cup",
            transport=httpx.MockTransport(handler),
        )
    )
    system = seen["body"]["messages"][0]["content"]
    assert "reverse-engineer" in system
    assert "under 60 words" in system
    parts = seen["body"]["messages"][1]["content"]
    assert parts[-1]["text"].startswith("Extra requirement:")


def test_reasoning_blocks_are_stripped_from_the_answer():
    def handler(request: httpx.Request) -> httpx.Response:
        return _chat_reply("<think>先想想</think>最终提示词：一只猫")

    text = _run(prompt_tools.enhance_prompt(_channel(), "deepseek-chat", "猫", transport=httpx.MockTransport(handler)))
    assert text == "最终提示词：一只猫"


def test_reasoning_only_answer_is_an_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return _chat_reply("<think>只有思考</think>")

    with pytest.raises(engine.ImageGenError) as caught:
        _run(prompt_tools.enhance_prompt(_channel(), "deepseek-chat", "猫", transport=httpx.MockTransport(handler)))
    assert caught.value.code == "upstream-empty"


def test_empty_answer_is_an_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return _chat_reply("   ")

    with pytest.raises(engine.ImageGenError) as caught:
        _run(prompt_tools.polish_prompt(_channel(), "deepseek-chat", "猫", transport=httpx.MockTransport(handler)))
    assert caught.value.code == "upstream-empty"


def test_upstream_error_message_is_surfaced_verbatim():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "invalid api key"}})

    with pytest.raises(engine.ImageGenError) as caught:
        _run(prompt_tools.enhance_prompt(_channel(), "deepseek-chat", "猫", transport=httpx.MockTransport(handler)))
    assert caught.value.code == "upstream-rejected"
    assert "invalid api key" in str(caught.value)
    assert "HTTP 401" in str(caught.value)


def test_chat_model_defaults_to_the_first_non_image_model():
    assert prompt_tools.chat_model_for(_channel()) == "deepseek-chat"
    assert prompt_tools.chat_model_for(_channel(), "gpt-4o-mini") == "gpt-4o-mini"


def test_channel_without_a_chat_model_is_refused_not_guessed():
    with pytest.raises(engine.ImageGenError) as caught:
        prompt_tools.chat_model_for(_channel(models=["gpt-image-2", "seedream-5.0"]))
    assert caught.value.code == "prompt-model-missing"


def test_list_models_reads_the_plain_endpoint():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"data": [{"id": "gpt-image-2"}, {"id": "deepseek-chat"}]})

    models = _run(prompt_tools.list_models(_channel(), transport=httpx.MockTransport(handler)))
    assert seen["url"] == "https://api.example.com/v1/models"
    assert models == ["deepseek-chat", "gpt-image-2"]


def test_reverse_requires_an_image():
    with pytest.raises(engine.ImageGenError) as caught:
        _run(prompt_tools.reverse_prompt(_channel(), "deepseek-chat", []))
    assert caught.value.code == "edit-image-missing"


def test_payload_json_tolerates_fences():
    assert prompt_tools.payload_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert prompt_tools.payload_json("no json here") is None
