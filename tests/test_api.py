"""Route behaviour end-to-end through FastAPI, with the engine mocked."""

from __future__ import annotations

import time

import pytest
from conftest import png_bytes, png_data_url
from fastapi import FastAPI
from fastapi.testclient import TestClient

from imagegen_backend import engine as image_engine


@pytest.fixture
def client(monkeypatch):
    import plugin_api

    # Fresh stores per test: the data root moves with HERMES_IMAGEGEN_DATA.
    monkeypatch.setattr(plugin_api, "_settings", None)
    monkeypatch.setattr(plugin_api, "_store", None)
    monkeypatch.setattr(plugin_api, "_runner", None)
    app = FastAPI()
    app.include_router(plugin_api.router, prefix="/api/plugins/hermes-imagegen")
    with TestClient(app) as test_client:
        yield test_client


def _add_channel(client, **overrides):
    payload = {"name": "本机", "api_url": "https://api.example.com/v1", "api_key": "sk-test-1234", "models": ["gpt-image-2"]}
    payload.update(overrides)
    response = client.post("/api/plugins/hermes-imagegen/settings/channels", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["channel"]


def _fake_generate(images=None, error=None):
    async def generate(channel, request, references=None, mask=None, cancel=None, transport=None):
        if error is not None:
            raise error
        return images or [image_engine.GeneratedImage(data=png_bytes(), mime="image/png")]

    return generate


def _wait_job(client, job_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/plugins/hermes-imagegen/jobs/{job_id}").json()["job"]
        if job["status"] in ("succeeded", "failed", "cancelled", "lost"):
            return job
        time.sleep(0.02)
    raise AssertionError("job did not settle")


def test_health_and_bootstrap_shape(client):
    health = client.get("/api/plugins/hermes-imagegen/health").json()
    assert health["ok"] is True
    assert health["version"].count(".") >= 1
    boot = client.get("/api/plugins/hermes-imagegen/bootstrap").json()
    assert boot["settings"]["channels"] == []
    assert boot["jobs"] == []


def test_settings_never_return_the_key(client):
    channel = _add_channel(client)
    assert channel["has_api_key"] is True
    body = client.get("/api/plugins/hermes-imagegen/settings").text
    assert "sk-test-1234" not in body
    assert channel["api_key_hint"] == "…1234"


def test_job_runs_and_image_is_served(client, monkeypatch):
    _add_channel(client)
    monkeypatch.setattr(image_engine, "generate", _fake_generate())
    created = client.post(
        "/api/plugins/hermes-imagegen/jobs",
        json={
            "channel_id": client.get("/api/plugins/hermes-imagegen/settings").json()["channels"][0]["id"],
            "model": "gpt-image-2",
            "mode": "generate",
            "prompt": "a cat",
            "size": "1:1",
            "quality": "2k",
            "n": 1,
        },
    )
    assert created.status_code == 200, created.text
    job = _wait_job(client, created.json()["job"]["id"])
    assert job["status"] == "succeeded", job
    assert len(job["images"]) == 1
    image = job["images"][0]
    served = client.get(f"/api/plugins/hermes-imagegen{image['url']}")
    assert served.status_code == 200
    assert served.headers["content-type"].startswith("image/png")
    assert served.content == png_bytes()


def test_failed_job_surfaces_error_and_history_record(client, monkeypatch):
    _add_channel(client)
    monkeypatch.setattr(
        image_engine, "generate", _fake_generate(error=image_engine.ImageGenError("上游拒绝", "upstream-rejected"))
    )
    channel_id = client.get("/api/plugins/hermes-imagegen/settings").json()["channels"][0]["id"]
    created = client.post(
        "/api/plugins/hermes-imagegen/jobs",
        json={"channel_id": channel_id, "model": "gpt-image-2", "mode": "generate", "prompt": "x"},
    )
    job = _wait_job(client, created.json()["job"]["id"])
    assert job["status"] == "failed"
    assert job["error_code"] == "upstream-rejected"
    history = client.get("/api/plugins/hermes-imagegen/history").json()
    assert history["total"] == 1
    assert history["items"][0]["status"] == "failed"
    assert history["items"][0]["error"] == "上游拒绝"


def test_edit_job_decodes_references(client, monkeypatch):
    _add_channel(client)
    captured = {}

    async def generate(channel, request, references=None, mask=None, cancel=None, transport=None):
        captured["references"] = len(references or [])
        captured["mode"] = request["mode"]
        return [image_engine.GeneratedImage(data=png_bytes(2, 2), mime="image/png")]

    monkeypatch.setattr(image_engine, "generate", generate)
    channel_id = client.get("/api/plugins/hermes-imagegen/settings").json()["channels"][0]["id"]
    created = client.post(
        "/api/plugins/hermes-imagegen/jobs",
        json={
            "channel_id": channel_id,
            "model": "gpt-image-2",
            "mode": "edit",
            "prompt": "把猫换成狗",
            "references": [png_data_url(2, 2)],
        },
    )
    assert created.status_code == 200, created.text
    _wait_job(client, created.json()["job"]["id"])
    assert captured == {"references": 1, "mode": "edit"}


def test_job_validation_errors(client):
    response = client.post("/api/plugins/hermes-imagegen/jobs", json={"channel_id": "missing", "model": "m", "prompt": "x"})
    assert response.status_code == 400
    assert "渠道" in response.text
    _add_channel(client)
    channel_id = client.get("/api/plugins/hermes-imagegen/settings").json()["channels"][0]["id"]
    response = client.post(
        "/api/plugins/hermes-imagegen/jobs", json={"channel_id": channel_id, "model": "m", "prompt": "   "}
    )
    assert response.status_code == 400


def test_lost_job_is_reported_not_404(client):
    response = client.get("/api/plugins/hermes-imagegen/jobs/does-not-exist")
    assert response.status_code == 200
    assert response.json()["job"]["status"] == "lost"


def test_favorite_endpoint_toggles_and_filters(client, monkeypatch):
    _add_channel(client)
    monkeypatch.setattr(image_engine, "generate", _fake_generate())
    channel_id = client.get("/api/plugins/hermes-imagegen/settings").json()["channels"][0]["id"]
    created = client.post(
        "/api/plugins/hermes-imagegen/jobs",
        json={"channel_id": channel_id, "model": "gpt-image-2", "mode": "generate", "prompt": "a cat"},
    )
    job = _wait_job(client, created.json()["job"]["id"])
    generation_id = job["generation_id"]
    starred = client.post(f"/api/plugins/hermes-imagegen/history/{generation_id}/favorite", json={"value": True})
    assert starred.status_code == 200
    assert starred.json()["generation"]["favorite"] == 1
    assert client.get("/api/plugins/hermes-imagegen/history?favorite=true").json()["total"] == 1
    assert client.get("/api/plugins/hermes-imagegen/history").json()["total"] == 1
    assert client.post("/api/plugins/hermes-imagegen/history/nope/favorite", json={"value": True}).status_code == 404


def test_inline_route_returns_data_url(client, monkeypatch):
    _add_channel(client)
    monkeypatch.setattr(image_engine, "generate", _fake_generate())
    channel_id = client.get("/api/plugins/hermes-imagegen/settings").json()["channels"][0]["id"]
    created = client.post(
        "/api/plugins/hermes-imagegen/jobs",
        json={"channel_id": channel_id, "model": "gpt-image-2", "mode": "generate", "prompt": "a cat"},
    )
    job = _wait_job(client, created.json()["job"]["id"])
    image_id = job["images"][0]["id"]
    inline = client.get(f"/api/plugins/hermes-imagegen/files/{image_id}/inline").json()
    assert inline["mime"] == "image/png"
    assert inline["data_url"].startswith("data:image/png;base64,")
    assert client.get("/api/plugins/hermes-imagegen/files/nope/inline").status_code == 404


def test_history_delete_removes_files(client, monkeypatch):
    _add_channel(client)
    monkeypatch.setattr(image_engine, "generate", _fake_generate())
    channel_id = client.get("/api/plugins/hermes-imagegen/settings").json()["channels"][0]["id"]
    created = client.post(
        "/api/plugins/hermes-imagegen/jobs",
        json={"channel_id": channel_id, "model": "gpt-image-2", "mode": "generate", "prompt": "a cat"},
    )
    job = _wait_job(client, created.json()["job"]["id"])
    generation_id = job["generation_id"]
    image_url = job["images"][0]["url"]
    assert client.get(f"/api/plugins/hermes-imagegen{image_url}").status_code == 200
    assert client.delete(f"/api/plugins/hermes-imagegen/history/{generation_id}").status_code == 200
    assert client.get(f"/api/plugins/hermes-imagegen{image_url}").status_code == 404
    assert client.get("/api/plugins/hermes-imagegen/history").json()["total"] == 0


def test_detect_saves_models_when_asked(client, monkeypatch):
    channel = _add_channel(client, models=[])

    async def fake_detect(channel_arg, api_key=None, transport=None):
        return {"models": [{"id": "gpt-image-2", "family": "gpt-image", "label": "gpt-image", "labelZh": "GPT 图像", "known": True, "supportsEdit": True, "supportsAspectRatio": False, "qualityTiers": ["1K"]}], "probed": [], "errors": []}

    monkeypatch.setattr(image_engine, "detect_models", fake_detect)
    response = client.post(
        "/api/plugins/hermes-imagegen/channels/detect", json={"channel_id": channel["id"], "save": True}
    )
    assert response.status_code == 200, response.text
    assert response.json()["channel"]["models"] == ["gpt-image-2"]
