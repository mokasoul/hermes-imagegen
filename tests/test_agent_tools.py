"""The chat tools: same job pipeline as the workbench, no network."""

from __future__ import annotations

import asyncio

import pytest
from conftest import png_bytes

from imagegen_backend import agent_tools, engine, runtime


def _run(coro):
    return asyncio.run(coro)


def _setup_channel(models_list=("gpt-image-2", "gpt-image-2.5"), default="gpt-image-2", name="本机"):
    settings = runtime.settings()
    channel = settings.upsert_channel(
        {
            "name": name,
            "api_url": "https://api.example.com/v1",
            "api_key": "sk-test-key",
            "models": list(models_list),
            "default_model": default,
        }
    )
    return settings.channel(channel["id"])


@pytest.fixture
def fake_engine(monkeypatch):
    calls = []

    async def generate(channel, request, references=None, mask=None, cancel=None, transport=None):
        calls.append({"channel": channel, "request": dict(request), "references": list(references or [])})
        return [engine.GeneratedImage(data=png_bytes(4, 4), mime="image/png")]

    monkeypatch.setattr(engine, "generate", generate)
    return calls


def test_generate_requires_a_channel():
    with pytest.raises(ValueError) as caught:
        _run(agent_tools.handle_generate_image({"prompt": "a cat"}))
    assert "还没有配置任何生图渠道" in str(caught.value)


def test_generate_uses_default_channel_and_model(fake_engine):
    _setup_channel()
    text = _run(agent_tools.handle_generate_image({"prompt": "a cat", "size": "16:9", "quality": "2k", "count": 1}))
    assert fake_engine[0]["request"]["model"] == "gpt-image-2"
    assert fake_engine[0]["request"]["size"] == "16:9"
    assert "状态：succeeded" in text
    assert "MEDIA:" in text
    assert "图片 id：" in text


def test_generate_accepts_channel_and_model_by_name(fake_engine):
    _setup_channel(name="官方")
    _run(agent_tools.handle_generate_image({"prompt": "a cat", "channel": "官方", "model": "gpt-image-2.5"}))
    assert fake_engine[0]["request"]["model"] == "gpt-image-2.5"


def test_unknown_model_is_refused_with_the_list(fake_engine):
    _setup_channel()
    with pytest.raises(ValueError) as caught:
        _run(agent_tools.handle_generate_image({"prompt": "a cat", "model": "dall-e-3"}))
    assert "没有模型 dall-e-3" in str(caught.value)
    assert "gpt-image-2" in str(caught.value)


def test_background_mode_returns_a_task_id(fake_engine):
    _setup_channel()
    text = _run(agent_tools.handle_generate_image({"prompt": "a cat", "wait_for_completion": False}))
    assert "task_id：" in text
    task_id = text.split("task_id：")[1].splitlines()[0].strip()
    status = _run(agent_tools.handle_get_image_generation_task({"task_id": task_id}))
    assert task_id in status


def test_get_unknown_task_is_honest(fake_engine):
    text = _run(agent_tools.handle_get_image_generation_task({"task_id": "nope"}))
    assert "找不到任务 nope" in text


def test_edit_from_a_local_file(fake_engine, tmp_path):
    _setup_channel()
    source = tmp_path / "source.png"
    source.write_bytes(png_bytes(3, 3))
    text = _run(agent_tools.handle_edit_image({"prompt": "换成夜景", "source_image": str(source)}))
    assert fake_engine[0]["request"]["mode"] == "edit"
    assert len(fake_engine[0]["references"]) == 1
    assert fake_engine[0]["references"][0].data == png_bytes(3, 3)
    assert "MEDIA:" in text


def test_edit_from_a_generated_image_id(fake_engine):
    _setup_channel()
    first = _run(agent_tools.handle_generate_image({"prompt": "a cat"}))
    image_id = first.split("图片 id：")[1].split("、")[0].splitlines()[0].strip()
    _run(agent_tools.handle_edit_image({"prompt": "加顶帽子", "source_image": image_id}))
    assert fake_engine[-1]["request"]["mode"] == "edit"
    assert fake_engine[-1]["references"][0].data == png_bytes(4, 4)


def test_edit_with_a_missing_source_is_a_clear_error(fake_engine):
    _setup_channel()
    with pytest.raises(ValueError) as caught:
        _run(agent_tools.handle_edit_image({"prompt": "x", "source_image": "/tmp/definitely-not-here.png"}))
    assert "找不到源图" in str(caught.value)


def test_edit_rejects_a_non_image_file(fake_engine, tmp_path):
    _setup_channel()
    bogus = tmp_path / "notes.txt"
    bogus.write_text("hello")
    with pytest.raises(ValueError) as caught:
        _run(agent_tools.handle_edit_image({"prompt": "x", "source_image": str(bogus)}))
    assert "不是可识别的图片格式" in str(caught.value)


def test_cancel_unknown_task_raises(fake_engine):
    with pytest.raises(ValueError):
        _run(agent_tools.handle_cancel_image_generation_task({"task_id": "nope"}))


def test_failure_is_reported_not_swallowed(monkeypatch):
    _setup_channel()

    async def failing(channel, request, references=None, mask=None, cancel=None, transport=None):
        raise engine.ImageGenError("上游拒绝", "upstream-rejected")

    monkeypatch.setattr(engine, "generate", failing)
    text = _run(agent_tools.handle_generate_image({"prompt": "a cat"}))
    assert "状态：failed" in text
    assert "上游拒绝" in text
    assert "upstream-rejected" in text
