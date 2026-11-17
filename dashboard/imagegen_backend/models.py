"""Model-family catalog and upstream request shaping.

Python port of the upstream DSH plugin's ``src/model-catalog.ts`` and
``effectiveParams()`` / ``effectiveCount()`` in ``src/engine.ts``. The tables are
copied verbatim on purpose: they encode documented per-provider wire formats
(pixel presets, aspect-ratio vocabularies, quality tiers) that must not drift
from the upstream project until we have our own provider-doc citations.

Pure data + pure functions — no I/O, so the tests can pin every mapping.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

ModelFamily = str

FAMILIES: tuple[str, ...] = (
    "gpt-image",
    "dall-e",
    "grok",
    "nanobanana",
    "seedream",
    "sensenova",
    "zhipu",
    "qwen",
    "minimax",
    "unknown",
)

# Capability annotation per family (upstream ``ENTRIES``).
_ENTRIES: dict[str, dict[str, Any]] = {
    "gpt-image": {
        "label": "gpt-image",
        "labelZh": "GPT 图像",
        "known": True,
        "supportsEdit": True,
        "supportsAspectRatio": False,
        "qualityTiers": ["1K", "2K", "4K"],
    },
    "dall-e": {
        "label": "DALL·E",
        "labelZh": "DALL·E",
        "known": True,
        "supportsEdit": True,
        "supportsAspectRatio": False,
        "qualityTiers": ["auto"],
    },
    "grok": {
        "label": "grok",
        "labelZh": "Grok",
        "known": True,
        "supportsEdit": True,
        "supportsAspectRatio": True,
        "qualityTiers": ["1K", "2K"],
    },
    "nanobanana": {
        "label": "nanobanana",
        "labelZh": "Nano Banana",
        "known": True,
        "supportsEdit": True,
        "supportsAspectRatio": True,
        "qualityTiers": ["1K", "2K", "4K"],
    },
    "seedream": {
        "label": "seedream",
        "labelZh": "Seedream",
        "known": True,
        "supportsEdit": True,
        "supportsAspectRatio": True,
        "supportsWatermarkToggle": True,
        "qualityTiers": ["1K", "2K"],
    },
    "sensenova": {
        "label": "SenseNova",
        "labelZh": "日日新图像",
        "known": True,
        "supportsEdit": True,
        "supportsAspectRatio": False,
        "supportsWatermarkToggle": True,
        "supportsPromptExtend": True,
        "qualityTiers": ["1K", "2K", "4K"],
    },
    "zhipu": {
        "label": "GLM-Image",
        "labelZh": "智谱图像",
        "known": True,
        "supportsEdit": False,
        "supportsAspectRatio": False,
        "qualityTiers": ["HD"],
    },
    "qwen": {
        "label": "qwen-image",
        "labelZh": "千问图像",
        "known": True,
        "supportsEdit": True,
        "supportsAspectRatio": True,
        "qualityTiers": ["auto"],
    },
    "minimax": {
        "label": "MiniMax",
        "labelZh": "MiniMax 图像",
        "known": True,
        "supportsEdit": True,
        "supportsAspectRatio": True,
        "qualityTiers": ["auto"],
    },
}

_UNKNOWN_ENTRY: dict[str, Any] = {
    "label": "unknown",
    "labelZh": "未知协议",
    "known": False,
    "supportsEdit": True,
    "supportsAspectRatio": False,
    "qualityTiers": [],
}

# Documented upstream prompt hard limits (characters).
PROMPT_CHAR_LIMITS: dict[str, int] = {"minimax": 1500}

NANOBANANA_GEMINI_IDS: frozenset[str] = frozenset(
    {
        "gemini-3-pro-image",
        "gemini-3-pro-image-preview",
        "gemini-3.1-flash-image",
        "gemini-3.1-flash-image-preview",
        "gemini-3.1-flash-lite-image",
        "gemini-2.5-flash-image",
    }
)

_LEGACY_OPENAI_SIZE_BY_RATIO: dict[str, str] = {
    "1:1": "1024x1024",
    "3:4": "1024x1536",
    "4:3": "1536x1024",
    "9:16": "1024x1792",
    "2:3": "1024x1536",
    "3:2": "1536x1024",
    "16:9": "1792x1024",
    "21:9": "1792x1024",
}

_OPENAI_HIGH_RES_SIZES_BY_RATIO: dict[str, dict[str, str]] = {
    "1:1": {"2k": "2048x2048", "4k": "2880x2880"},
    "3:4": {"2k": "1536x2048", "4k": "2448x3264"},
    "4:3": {"2k": "2048x1536", "4k": "3264x2448"},
    "9:16": {"2k": "1440x2560", "4k": "2160x3840"},
    "2:3": {"2k": "1728x2592", "4k": "2304x3456"},
    "3:2": {"2k": "2592x1728", "4k": "3456x2304"},
    "16:9": {"2k": "2560x1440", "4k": "3840x2160"},
    "21:9": {"2k": "2560x1104", "4k": "3840x1648"},
}

_QWEN_SIZE_CLASSIC: dict[str, str] = {
    "16:9": "1664*928",
    "21:9": "1664*928",
    "4:3": "1472*1104",
    "3:2": "1472*1104",
    "1:1": "1328*1328",
    "3:4": "1104*1472",
    "2:3": "1104*1472",
    "9:16": "928*1664",
}

_QWEN_SIZE_HD: dict[str, str] = {
    "16:9": "2688*1536",
    "21:9": "2688*1536",
    "4:3": "2368*1728",
    "3:2": "2368*1728",
    "1:1": "2048*2048",
    "3:4": "1728*2368",
    "2:3": "1728*2368",
    "9:16": "1536*2688",
}

GROK_ASPECT_ALIASES: dict[str, str] = {"21:9": "20:9"}

DALLE3_SIZES: frozenset[str] = frozenset({"1024x1024", "1792x1024", "1024x1792"})

MINIMAX_RATIOS: frozenset[str] = frozenset(
    {"1:1", "16:9", "4:3", "3:2", "2:3", "3:4", "9:16", "21:9"}
)

MINIMAX_MAX_N = 9
MAX_IMAGES_PER_REQUEST = 4
DEFAULT_WIRE_MODEL = "gpt-image-2"


def describe_family(model: str) -> dict[str, Any]:
    """Classify one upstream model id into its request-shaping family."""
    mid = (model or "").strip()
    if re.match(r"^gpt-image", mid, re.I):
        family = "gpt-image"
    elif re.match(r"^dall-e", mid, re.I):
        family = "dall-e"
    elif re.match(r"^grok-imagine(?:-|$)", mid):
        family = "grok"
    elif re.match(r"^nanobanana", mid, re.I) or mid in NANOBANANA_GEMINI_IDS:
        family = "nanobanana"
    elif re.match(r"^(?:doubao-)?seedream", mid, re.I):
        family = "seedream"
    elif re.match(r"^(?:sensenova[/:])?sensenova-u(?:\d|[-_.]|$)", mid, re.I):
        family = "sensenova"
    elif re.match(r"^(?:glm-image|cogview(?:-|$))", mid, re.I):
        family = "zhipu"
    elif re.match(r"^qwen-image(?:[-_.]|$)", mid, re.I):
        family = "qwen"
    elif re.match(r"^(?:minimax[-_/])?image-\d+", mid, re.I):
        family = "minimax"
    else:
        return {"family": "unknown", **_UNKNOWN_ENTRY}
    return {"family": family, **_ENTRIES[family]}


def describe_model(model: str) -> dict[str, Any]:
    """Shared capability source for request shaping and channel controls."""
    entry = describe_family(model)
    return {
        "supportsWatermarkToggle": False,
        "supportsPromptExtend": False,
        **entry,
    }


def model_family(model: str) -> ModelFamily:
    return describe_model(model)["family"]


def prompt_char_limit(model: str) -> int | None:
    family = model_family(model)
    return PROMPT_CHAR_LIMITS.get(family)


_IMAGE_ID_HINT = re.compile(
    r"(?:^|[-_.])(?:image|img|diffusion|flux|cogview|imagen|seedream|nanobanana"
    r"|grok-imagine|dall-e|stable-diffusion|sdxl|pixart|kolors|ideogram|midjourney"
    r"|recraft|hunyuan|jimeng|wanx|hidream|playground)(?:$|[-_.])",
    re.I,
)


def is_likely_image_model_id(model: str) -> bool:
    """Conservative filter for gateways whose /models list carries only ids."""
    if model_family(model) == "sensenova":
        return True
    return bool(_IMAGE_ID_HINT.search((model or "").strip()))


def wire_model(request: Mapping[str, Any]) -> str:
    """The model id actually sent upstream: explicit wire id wins, then alias."""
    explicit = str(request.get("upstream") or "").strip()
    if explicit:
        return explicit
    alias = str(request.get("model") or "").strip()
    return alias or DEFAULT_WIRE_MODEL


def is_chat_completions_url(value: str) -> bool:
    trimmed = (value or "").strip().rstrip("/")
    return re.search(r"/chat/completions$", trimmed) is not None


def resolve_channel_protocol(channel: Mapping[str, Any]) -> str:
    """'images' or 'chat-completions', honouring an explicit channel choice."""
    explicit = str(channel.get("protocol") or "auto").strip()
    if explicit in ("images", "chat-completions"):
        return explicit
    return "chat-completions" if is_chat_completions_url(str(channel.get("api_url") or "")) else "images"


def openai_size_for(model: str, ratio: str, quality: str) -> str | None:
    """Select by the resolved wire model, not its user-facing alias."""
    if ratio in ("", "auto"):
        return None
    version = re.match(r"^gpt-image-(\d+)(?:\.\d+)?(?:-|$)", (model or "").strip(), re.I)
    if version is not None and int(version.group(1)) >= 2 and quality in ("2k", "4k"):
        return _OPENAI_HIGH_RES_SIZES_BY_RATIO.get(ratio, {}).get(quality)
    return _LEGACY_OPENAI_SIZE_BY_RATIO.get(ratio)


def is_versioned_qwen_image(model: str) -> bool:
    """Versioned ids (qwen-image-2.0 / -3.0-pro / …) take the large size set."""
    return re.match(r"^qwen-image-\d+\.\d", (model or "").strip(), re.I) is not None


def qwen_size(model: str, ratio: str) -> str | None:
    if ratio in ("", "auto"):
        return None
    table = _QWEN_SIZE_HD if is_versioned_qwen_image(model) else _QWEN_SIZE_CLASSIC
    return table.get(ratio)


def seedream_size(quality: str) -> str:
    return "1K" if quality == "1k" else "2K"


def _is_volc_seedream(model: str) -> bool:
    return re.match(r"^doubao-seedream(?:-|$)", (model or "").strip(), re.I) is not None


def _is_glm_image(model: str) -> bool:
    return re.match(r"^glm-image(?:-|$)", (model or "").strip(), re.I) is not None


def clamp_count(n: Any) -> int:
    try:
        value = float(n)
    except (TypeError, ValueError):
        return 1
    if value != value:  # NaN
        return 1
    return max(1, min(MAX_IMAGES_PER_REQUEST, int(round(value))))


def effective_count(request: Mapping[str, Any]) -> int:
    if wire_model(request) == "dall-e-3":
        return 1
    return clamp_count(request.get("n", 1))


def effective_params(request: Mapping[str, Any], channel: Mapping[str, Any]) -> dict[str, Any]:
    """Pick the effective per-model upstream parameters.

    Never includes ``n``: batch parameters are rejected by Responses-API-based
    gateways, so a requested count is satisfied by parallel single-image calls.
    """
    model = wire_model(request)
    capabilities = describe_model(model)
    ratio = str(request.get("size") or "")
    quality = str(request.get("quality") or "")
    detail = str(request.get("detail") or "")

    preferences: dict[str, Any] = {}
    if capabilities["supportsWatermarkToggle"] and isinstance(channel.get("watermark"), bool):
        preferences["watermark"] = channel["watermark"]
    if capabilities["supportsPromptExtend"] and isinstance(channel.get("prompt_extend"), bool):
        preferences["prompt_extend"] = channel["prompt_extend"]

    family = capabilities["family"]

    if family == "sensenova":
        pixels = openai_size_for("gpt-image-2", ratio, quality)
        params: dict[str, Any] = {"model": model, "response_format": "b64_json", **preferences}
        if pixels is not None:
            w, h = pixels.split("x")
            params["size"] = f"{round(int(w) / 32) * 32}x{round(int(h) / 32) * 32}"
        return params

    if model == "dall-e-3":
        pixel = _LEGACY_OPENAI_SIZE_BY_RATIO.get(ratio)
        size = pixel if pixel in DALLE3_SIZES else "1024x1024"
        return {"model": model, "size": size}

    if family == "grok":
        params = {"model": model, "response_format": "b64_json"}
        if ratio not in ("", "auto"):
            params["aspect_ratio"] = GROK_ASPECT_ALIASES.get(ratio, ratio)
        if quality not in ("", "auto"):
            params["resolution"] = "2k" if quality == "4k" else quality
        return params

    if family == "nanobanana":
        params = {"model": model, "response_format": "b64_json"}
        if ratio not in ("", "auto"):
            params["aspect_ratio"] = ratio
        if quality not in ("", "auto"):
            params["image_size"] = quality.upper()
        return params

    if family == "seedream":
        return {
            "model": model,
            "size": seedream_size(quality),
            "response_format": "url" if _is_volc_seedream(model) else "b64_json",
            **preferences,
        }

    if family == "zhipu":
        params = {"model": model, "quality": "hd" if _is_glm_image(model) else "standard"}
        if ratio not in ("", "auto") and ratio in _LEGACY_OPENAI_SIZE_BY_RATIO:
            params["size"] = _LEGACY_OPENAI_SIZE_BY_RATIO[ratio]
        return params

    params = {"model": model}
    openai_size = openai_size_for(model, ratio, quality)
    if openai_size is not None:
        params["size"] = openai_size
    if quality == "1k":
        params["quality"] = "low"
    elif quality == "2k":
        params["quality"] = "medium"
    elif quality == "4k":
        params["quality"] = "high"
    if detail:
        params["detail"] = detail
    params.update(preferences)
    return params
