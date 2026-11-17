"""Channel settings: storage, masking, credential resolution.

The API key never leaves the gateway process: every read path returns a masked
view (``has_api_key`` + a 4-char hint), and the raw value is only used inside
``engine`` when a request is built. Keys live in ``settings.json`` with mode
0600, or in an environment variable when a channel sets ``api_key_env`` — so a
credential can stay out of the file entirely.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Mapping

from . import paths

SECRET_FIELDS = ("api_key",)
MASK = "***"
DEFAULT_PROTOCOL = "auto"
VALID_PROTOCOLS = ("auto", "images", "chat-completions")


class SettingsError(ValueError):
    """Rejected settings mutation (bad url, unknown channel, …)."""


def _clean_str(value: Any, limit: int = 2000) -> str:
    return str(value or "").strip()[:limit]


def new_channel_id() -> str:
    return uuid.uuid4().hex[:12]


def normalize_api_url(value: str) -> str:
    url = _clean_str(value)
    if not url:
        raise SettingsError("api_url 不能为空")
    if not re.match(r"^https?://", url, re.I):
        raise SettingsError("api_url 必须以 http:// 或 https:// 开头")
    return url.rstrip("/")


def normalize_channel(payload: Mapping[str, Any], existing: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Validate one channel payload, carrying secrets over from ``existing``."""
    base: dict[str, Any] = dict(existing or {})
    channel = {
        "id": _clean_str(base.get("id")) or new_channel_id(),
        "name": _clean_str(payload.get("name", base.get("name", ""))) or "默认渠道",
        "api_url": normalize_api_url(str(payload.get("api_url", base.get("api_url", "")))),
        "api_url_full": bool(payload.get("api_url_full", base.get("api_url_full", False))),
        "protocol": _clean_str(payload.get("protocol", base.get("protocol", DEFAULT_PROTOCOL))) or DEFAULT_PROTOCOL,
        "api_key": _clean_str(base.get("api_key"), 4000),
        "api_key_env": _clean_str(payload.get("api_key_env", base.get("api_key_env", "")), 200),
        "models": [str(m).strip() for m in (payload.get("models", base.get("models", [])) or []) if str(m).strip()],
        "default_model": _clean_str(payload.get("default_model", base.get("default_model", "")), 200),
        "watermark": payload.get("watermark", base.get("watermark")),
        "prompt_extend": payload.get("prompt_extend", base.get("prompt_extend")),
    }
    if channel["protocol"] not in VALID_PROTOCOLS:
        raise SettingsError(f"protocol 必须是 {'/'.join(VALID_PROTOCOLS)}")
    # Secret handling: an absent/blank api_key keeps the stored one; an explicit
    # clear_api_key=true wipes it. The masked echo the UI sends back is ignored.
    if payload.get("clear_api_key") is True:
        channel["api_key"] = ""
    else:
        candidate = payload.get("api_key")
        if isinstance(candidate, str) and candidate.strip() and candidate.strip() != MASK:
            channel["api_key"] = candidate.strip()[:4000]
    for key in ("watermark", "prompt_extend"):
        value = channel[key]
        if value is None or isinstance(value, bool):
            continue
        raise SettingsError(f"{key} 必须是布尔值或 null")
    return channel


def mask_channel(channel: Mapping[str, Any]) -> dict[str, Any]:
    """Public view: everything but the secret, plus enough to render the UI."""
    key = str(channel.get("api_key") or "")
    env_name = str(channel.get("api_key_env") or "")
    return {
        "id": channel.get("id", ""),
        "name": channel.get("name", ""),
        "api_url": channel.get("api_url", ""),
        "api_url_full": bool(channel.get("api_url_full", False)),
        "protocol": channel.get("protocol", DEFAULT_PROTOCOL),
        "models": list(channel.get("models") or []),
        "default_model": channel.get("default_model", ""),
        "watermark": channel.get("watermark"),
        "prompt_extend": channel.get("prompt_extend"),
        "api_key_env": env_name,
        "has_api_key": bool(key) or bool(env_name),
        "api_key_hint": f"…{key[-4:]}" if len(key) >= 8 else "",
        "api_key_from_env": bool(env_name) and not bool(key),
    }


def resolve_api_key(channel: Mapping[str, Any]) -> str:
    """Stored key first, then the channel's environment variable."""
    stored = str(channel.get("api_key") or "").strip()
    if stored:
        return stored
    env_name = str(channel.get("api_key_env") or "").strip()
    if env_name:
        return os.environ.get(env_name, "").strip()
    return ""


class Settings:
    """Channel/defaults store backed by one JSON file (mode 0600)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else paths.settings_file()
        self._data: dict[str, Any] = {"channels": [], "default_channel_id": "", "default_model": ""}
        self.load()

    # --- persistence -----------------------------------------------------

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        channels = raw.get("channels")
        self._data = {
            "channels": [self._coerce_stored(c) for c in channels if isinstance(c, dict)] if isinstance(channels, list) else [],
            "default_channel_id": _clean_str(raw.get("default_channel_id"), 64),
            "default_model": _clean_str(raw.get("default_model"), 200),
        }

    @staticmethod
    def _coerce_stored(channel: Mapping[str, Any]) -> dict[str, Any]:
        """Load a stored channel without re-validating its URL to death."""
        out = {
            "id": _clean_str(channel.get("id")) or new_channel_id(),
            "name": _clean_str(channel.get("name")) or "默认渠道",
            "api_url": _clean_str(channel.get("api_url")).rstrip("/"),
            "api_url_full": bool(channel.get("api_url_full", False)),
            "protocol": _clean_str(channel.get("protocol")) or DEFAULT_PROTOCOL,
            "api_key": _clean_str(channel.get("api_key"), 4000),
            "api_key_env": _clean_str(channel.get("api_key_env"), 200),
            "models": [str(m) for m in (channel.get("models") or []) if str(m).strip()],
            "default_model": _clean_str(channel.get("default_model"), 200),
            "watermark": channel.get("watermark") if isinstance(channel.get("watermark"), bool) else None,
            "prompt_extend": channel.get("prompt_extend") if isinstance(channel.get("prompt_extend"), bool) else None,
        }
        if out["protocol"] not in VALID_PROTOCOLS:
            out["protocol"] = DEFAULT_PROTOCOL
        return out

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        payload = json.dumps(self._data, ensure_ascii=False, indent=2)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
        finally:
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
        os.replace(tmp, self.path)

    # --- accessors -------------------------------------------------------

    @property
    def channels(self) -> list[dict[str, Any]]:
        return [dict(c) for c in self._data["channels"]]

    def channel(self, channel_id: str) -> dict[str, Any] | None:
        for channel in self._data["channels"]:
            if channel.get("id") == channel_id:
                return dict(channel)
        return None

    def public(self) -> dict[str, Any]:
        return {
            "channels": [mask_channel(c) for c in self._data["channels"]],
            "default_channel_id": self._data.get("default_channel_id", ""),
            "default_model": self._data.get("default_model", ""),
            "data_dir": str(paths.data_root()),
        }

    def upsert_channel(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        channel_id = _clean_str(payload.get("id"), 64)
        existing = self.channel(channel_id) if channel_id else None
        if channel_id and existing is None:
            # An unknown id means the UI is editing a channel that is gone —
            # creating one under that id would silently resurrect a phantom.
            raise SettingsError("渠道不存在，请刷新后重试")
        channel = normalize_channel(payload, existing)
        channels = list(self._data["channels"])
        for index, item in enumerate(channels):
            if item.get("id") == channel["id"]:
                channels[index] = channel
                break
        else:
            channels.append(channel)
        self._data["channels"] = channels
        if not self._data.get("default_channel_id"):
            self._data["default_channel_id"] = channel["id"]
        self.save()
        return mask_channel(channel)

    def delete_channel(self, channel_id: str) -> None:
        channels = [c for c in self._data["channels"] if c.get("id") != channel_id]
        if len(channels) == len(self._data["channels"]):
            raise SettingsError("渠道不存在")
        self._data["channels"] = channels
        if self._data.get("default_channel_id") == channel_id:
            self._data["default_channel_id"] = channels[0]["id"] if channels else ""
        self.save()

    def set_defaults(self, default_channel_id: str | None = None, default_model: str | None = None) -> None:
        if default_channel_id is not None:
            target = _clean_str(default_channel_id, 64)
            if target and self.channel(target) is None:
                raise SettingsError("默认渠道不存在")
            self._data["default_channel_id"] = target
        if default_model is not None:
            self._data["default_model"] = _clean_str(default_model, 200)
        self.save()

    def add_models(self, channel_id: str, models: list[str]) -> dict[str, Any]:
        """Merge detected model ids into a channel (order preserved, deduped)."""
        channel = self.channel(channel_id)
        if channel is None:
            raise SettingsError("渠道不存在")
        merged = list(channel.get("models") or [])
        for model in models:
            mid = _clean_str(model, 200)
            if mid and mid not in merged:
                merged.append(mid)
        channel["models"] = merged
        if not channel.get("default_model") and merged:
            channel["default_model"] = merged[0]
        self._data["channels"] = [channel if c.get("id") == channel_id else c for c in self._data["channels"]]
        self.save()
        return mask_channel(channel)
