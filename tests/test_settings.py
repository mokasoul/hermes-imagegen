from __future__ import annotations

import os
import stat

import pytest

from imagegen_backend.settings import Settings, SettingsError, mask_channel, resolve_api_key


def _settings(tmp_path) -> Settings:
    return Settings(tmp_path / "settings.json")


def test_upsert_masks_key_and_persists(tmp_path):
    settings = _settings(tmp_path)
    channel = settings.upsert_channel({"name": "本机", "api_url": "https://api.example.com/v1/", "api_key": "sk-secret-1234"})
    assert channel["has_api_key"] is True
    assert channel["api_key_hint"] == "…1234"
    assert "api_key" not in channel
    assert channel["api_url"] == "https://api.example.com/v1"
    stored = (tmp_path / "settings.json").read_text(encoding="utf-8")
    assert "sk-secret-1234" in stored  # on disk, never in an API response
    assert stat.S_IMODE(os.stat(tmp_path / "settings.json").st_mode) == 0o600


def test_blank_key_keeps_stored_secret_and_clear_wipes_it(tmp_path):
    settings = _settings(tmp_path)
    created = settings.upsert_channel({"name": "c", "api_url": "https://api.example.com/v1", "api_key": "sk-keep-9999"})
    updated = settings.upsert_channel({"id": created["id"], "name": "c2", "api_url": "https://api.example.com/v1", "api_key": ""})
    assert updated["has_api_key"] is True
    assert updated["api_key_hint"] == "…9999"
    cleared = settings.upsert_channel(
        {"id": created["id"], "name": "c2", "api_url": "https://api.example.com/v1", "clear_api_key": True}
    )
    assert cleared["has_api_key"] is False
    assert cleared["api_key_hint"] == ""


def test_masked_echo_is_not_stored_as_the_secret(tmp_path):
    settings = _settings(tmp_path)
    created = settings.upsert_channel({"name": "c", "api_url": "https://api.example.com/v1", "api_key": "sk-real-4242"})
    settings.upsert_channel({"id": created["id"], "name": "c", "api_url": "https://api.example.com/v1", "api_key": "***"})
    assert settings.channel(created["id"])["api_key"] == "sk-real-4242"


def test_env_key_resolution(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    channel = settings.upsert_channel({"name": "c", "api_url": "https://api.example.com/v1", "api_key_env": "MY_IMG_KEY"})
    monkeypatch.setenv("MY_IMG_KEY", "sk-from-env")
    assert channel["api_key_from_env"] is True
    assert resolve_api_key(settings.channel(channel["id"])) == "sk-from-env"
    monkeypatch.delenv("MY_IMG_KEY")
    assert resolve_api_key(settings.channel(channel["id"])) == ""


def test_unknown_id_cannot_be_created(tmp_path):
    settings = _settings(tmp_path)
    with pytest.raises(SettingsError):
        settings.upsert_channel({"id": "ghost", "name": "x", "api_url": "https://api.example.com/v1"})


def test_url_validation_is_strict(tmp_path):
    settings = _settings(tmp_path)
    for bad in ("", "ftp://x/v1", "file:///etc/passwd", "api.example.com"):
        with pytest.raises(SettingsError):
            settings.upsert_channel({"name": "x", "api_url": bad, "api_key": "k"})


def test_delete_reassigns_default_channel(tmp_path):
    settings = _settings(tmp_path)
    first = settings.upsert_channel({"name": "a", "api_url": "https://a.example.com/v1", "api_key": "k"})
    second = settings.upsert_channel({"name": "b", "api_url": "https://b.example.com/v1", "api_key": "k"})
    assert settings.public()["default_channel_id"] == first["id"]
    settings.delete_channel(first["id"])
    assert settings.public()["default_channel_id"] == second["id"]
    with pytest.raises(SettingsError):
        settings.delete_channel("nope")


def test_add_models_dedupes_and_preserves_order(tmp_path):
    settings = _settings(tmp_path)
    channel = settings.upsert_channel({"name": "a", "api_url": "https://a.example.com/v1", "api_key": "k"})
    merged = settings.add_models(channel["id"], ["gpt-image-2", "gpt-image-1", "gpt-image-2"])
    assert merged["models"] == ["gpt-image-2", "gpt-image-1"]
    assert merged["default_model"] == "gpt-image-2"


def test_mask_channel_hides_secret():
    public = mask_channel({"id": "1", "api_key": "sk-abcdefgh", "api_url": "https://x/v1"})
    assert public["api_key_hint"] == "…efgh"
    assert "sk-abcdefgh" not in str(public)
