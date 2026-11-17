"""Pins the upstream request-shaping tables: pixel presets and per-family knobs."""

from __future__ import annotations

from imagegen_backend import models


def _request(**overrides):
    base = {"mode": "generate", "model": "gpt-image-2", "prompt": "a cat", "size": "1:1", "quality": "", "detail": "", "n": 1}
    base.update(overrides)
    return base


def test_family_detection_matches_upstream_catalog():
    assert models.model_family("gpt-image-2") == "gpt-image"
    assert models.model_family("gpt-image-2.5-flare") == "gpt-image"
    assert models.model_family("dall-e-3") == "dall-e"
    assert models.model_family("grok-imagine-image-2.0") == "grok"
    assert models.model_family("nanobanana2-lite") == "nanobanana"
    assert models.model_family("gemini-3-pro-image") == "nanobanana"
    assert models.model_family("doubao-seedream-4-0") == "seedream"
    assert models.model_family("seedream-5.0-pro") == "seedream"
    assert models.model_family("glm-image") == "zhipu"
    assert models.model_family("cogview-4") == "zhipu"
    assert models.model_family("qwen-image-2.0") == "qwen"
    assert models.model_family("image-01") == "minimax"
    assert models.model_family("totally-custom-thing") == "unknown"


def test_gpt_image_2_high_res_presets_match_documented_sizes():
    assert models.effective_params(_request(size="16:9", quality="2k"), {})["size"] == "2560x1440"
    assert models.effective_params(_request(size="16:9", quality="4k"), {})["size"] == "3840x2160"
    assert models.effective_params(_request(size="1:1", quality="4k"), {})["size"] == "2880x2880"
    assert models.effective_params(_request(size="1:1", quality="2k"), {})["size"] == "2048x2048"


def test_legacy_sizes_and_quality_mapping_for_unversioned_models():
    params = models.effective_params(_request(model="gpt-image-1", size="16:9", quality="1k"), {})
    assert params["size"] == "1792x1024"
    assert params["quality"] == "low"
    params = models.effective_params(_request(model="gpt-image-1", size="1:1", quality="2k"), {})
    assert params["size"] == "1024x1024"
    assert params["quality"] == "medium"


def test_auto_ratio_sends_no_size():
    assert "size" not in models.effective_params(_request(size="auto", quality="2k"), {})


def test_dall_e_3_keeps_supported_sizes_only():
    # 16:9 maps onto 1792x1024, which dall-e-3 accepts — it is kept, not clamped.
    params = models.effective_params(_request(model="dall-e-3", size="16:9", quality="1k"), {})
    assert params == {"model": "dall-e-3", "size": "1792x1024"}
    # 3:4 maps onto 1024x1536, which dall-e-3 does NOT accept → square default.
    params = models.effective_params(_request(model="dall-e-3", size="3:4"), {})
    assert params["size"] == "1024x1024"
    assert models.effective_count(_request(model="dall-e-3", n=4)) == 1


def test_grok_uses_ratio_and_resolution_vocabulary():
    params = models.effective_params(_request(model="grok-imagine-image-2.0", size="21:9", quality="4k"), {})
    assert params["aspect_ratio"] == "20:9"
    assert params["resolution"] == "2k"
    assert params["response_format"] == "b64_json"
    assert "size" not in params


def test_nanobanana_uses_image_size_tiers():
    params = models.effective_params(_request(model="nanobanana2", size="3:4", quality="4k"), {})
    assert params["aspect_ratio"] == "3:4"
    assert params["image_size"] == "4K"
    assert params["response_format"] == "b64_json"


def test_seedream_uses_resolution_tier_and_watermark_toggle():
    params = models.effective_params(_request(model="seedream-5.0-pro", size="16:9", quality="4k"), {"watermark": False})
    assert params["size"] == "2K"
    assert params["watermark"] is False
    assert params["response_format"] == "b64_json"
    volc = models.effective_params(_request(model="doubao-seedream-4-0", quality="1k"), {})
    assert volc["size"] == "1K"
    assert volc["response_format"] == "url"


def test_zhipu_quality_vocabulary_and_no_watermark_field():
    params = models.effective_params(_request(model="glm-image", size="1:1"), {"watermark": False})
    assert params["quality"] == "hd"
    assert params["size"] == "1024x1024"
    assert "watermark" not in params


def test_sensenova_aligns_pixels_and_honours_prompt_extend():
    params = models.effective_params(_request(model="sensenova-u1", size="1:1", quality="2k"), {"prompt_extend": False, "watermark": True})
    assert params["size"] == "2048x2048"
    assert params["prompt_extend"] is False
    assert params["watermark"] is True
    assert params["response_format"] == "b64_json"


def test_unknown_model_falls_back_to_openai_shape():
    params = models.effective_params(_request(model="mystery-image-model", size="1:1", quality="1k"), {})
    assert params["model"] == "mystery-image-model"
    assert params["size"] == "1024x1024"
    assert params["quality"] == "low"
    assert models.describe_model("mystery-image-model")["known"] is False


def test_count_clamping_and_wire_model_resolution():
    assert models.clamp_count(0) == 1
    assert models.clamp_count(9) == 4
    assert models.clamp_count("x") == 1
    assert models.wire_model({"model": "", "upstream": ""}) == "gpt-image-2"
    assert models.wire_model({"model": "alias", "upstream": "real-model"}) == "real-model"


def test_protocol_resolution():
    assert models.resolve_channel_protocol({"api_url": "https://api.example.com/v1"}) == "images"
    assert models.resolve_channel_protocol({"api_url": "https://api.example.com/v1/chat/completions"}) == "chat-completions"
    assert models.resolve_channel_protocol({"api_url": "https://x/v1", "protocol": "images"}) == "images"


def test_prompt_limit_only_for_documented_families():
    assert models.prompt_char_limit("image-01") == 1500
    assert models.prompt_char_limit("gpt-image-2") is None
