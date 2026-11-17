"""Editable poster recipe: text and logo layers over one image.

Port of the upstream ``src/poster.ts`` (validation, wrapping rules) with the
same field names, so a recipe written here reads like upstream's.

The recipe is stored on the canvas image node (``metadata.poster``) and stays
editable; the flattened PNG is a separate asset. The client does the painting
(one ``<canvas>`` drives both the preview and the export) — this module only
owns the invariants, so a bad recipe is refused at save time instead of blowing
up at export time.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from . import engine

MAX_POSTER_LAYERS = 40
MAX_POSTER_SIDE = 4096
FONT_FAMILIES = {"sans-serif", "serif", "monospace"}
ALIGNMENTS = {"left", "center", "right"}
IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
_HEX = re.compile(r"^#[a-f0-9]{6}$", re.I)


class PosterError(ValueError):
    """Recipe rejected — the message names the offending field."""


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and float(value) == float(value)


def _positive(value: Any, limit: int = MAX_POSTER_SIDE) -> bool:
    return _finite(value) and 0 < float(value) <= limit


def asset_ref(value: Any) -> dict[str, str]:
    """Our asset refs are ``{id, mime, name}``; anything else is refused."""
    if not isinstance(value, Mapping):
        raise PosterError("素材引用无效")
    asset_id = str(value.get("id") or "")
    mime = str(value.get("mime") or "")
    if not re.match(r"^[a-f0-9]{16,64}$", asset_id):
        raise PosterError("素材 id 无效")
    if mime not in IMAGE_MIMES:
        raise PosterError(f"海报只接受 PNG/JPEG/WEBP/GIF 图片（收到 {mime or '空'}）")
    return {"id": asset_id, "mime": mime, "name": str(value.get("name") or "")}


def validate(recipe: Any) -> dict[str, Any]:
    """Return a normalised recipe or raise :class:`PosterError`."""
    if not isinstance(recipe, Mapping):
        raise PosterError("海报配方必须是对象")
    width = recipe.get("width")
    height = recipe.get("height")
    if not isinstance(width, int) or not isinstance(height, int) or not _positive(width) or not _positive(height):
        raise PosterError("海报宽高必须是 1–4096 的整数")
    background = asset_ref(recipe.get("background"))
    layers_raw = recipe.get("layers")
    if not isinstance(layers_raw, list):
        raise PosterError("layers 必须是数组")
    if len(layers_raw) > MAX_POSTER_LAYERS:
        raise PosterError(f"图层最多 {MAX_POSTER_LAYERS} 个")

    layers: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in layers_raw:
        if not isinstance(raw, Mapping):
            raise PosterError("图层必须是对象")
        layer_id = str(raw.get("id") or "")
        if not layer_id or len(layer_id) > 100:
            raise PosterError("图层 id 无效（1–100 字符）")
        if layer_id in seen:
            raise PosterError(f"图层 id 重复：{layer_id}")
        seen.add(layer_id)
        x, y = raw.get("x"), raw.get("y")
        if not _finite(x) or not _finite(y) or float(x) < 0 or float(y) < 0 or float(x) >= width or float(y) >= height:
            raise PosterError(f"图层 {layer_id} 的位置超出画布")
        layer_width = raw.get("width")
        if not _positive(layer_width, width):
            raise PosterError(f"图层 {layer_id} 的宽度非法")
        kind = raw.get("type")
        if kind == "image":
            layer_height = raw.get("height")
            if not _positive(layer_height, height):
                raise PosterError(f"Logo 图层 {layer_id} 的高度非法")
            layers.append(
                {
                    "id": layer_id,
                    "type": "image",
                    "x": float(x),
                    "y": float(y),
                    "width": float(layer_width),
                    "height": float(layer_height),
                    "asset": asset_ref(raw.get("asset")),
                }
            )
            continue
        if kind != "text":
            raise PosterError(f"图层 {layer_id} 的类型不支持：{kind or '(空)'}")
        text = raw.get("text")
        if not isinstance(text, str) or len(text) > 4000:
            raise PosterError(f"文字图层 {layer_id} 的内容非法（最多 4000 字）")
        font_size = raw.get("fontSize")
        if not _finite(font_size) or not 8 <= float(font_size) <= 512:
            raise PosterError(f"文字图层 {layer_id} 的字号必须在 8–512 之间")
        family = str(raw.get("fontFamily") or "sans-serif")
        if family not in FONT_FAMILIES:
            raise PosterError(f"字体类别不支持：{family}")
        color = str(raw.get("color") or "#ffffff")
        if not _HEX.match(color):
            raise PosterError(f"颜色必须是 #rrggbb：{color}")
        align = str(raw.get("align") or "left")
        if align not in ALIGNMENTS:
            raise PosterError(f"对齐方式不支持：{align}")
        layers.append(
            {
                "id": layer_id,
                "type": "text",
                "x": float(x),
                "y": float(y),
                "width": float(layer_width),
                "text": text,
                "fontSize": float(font_size),
                "fontFamily": family,
                "color": color.lower(),
                "bold": bool(raw.get("bold")),
                "align": align,
            }
        )
    return {"version": 1, "width": width, "height": height, "background": background, "layers": layers}


def default_recipe(asset: Mapping[str, Any], data: bytes) -> dict[str, Any]:
    """A starting recipe: the image as background, one centred title layer."""
    dimensions = engine.image_dimensions(data)
    if dimensions is None:
        raise PosterError("无法读取图片尺寸，不能作为海报底图")
    width, height = dimensions
    if width > MAX_POSTER_SIDE or height > MAX_POSTER_SIDE:
        raise PosterError(f"底图最长边超过 {MAX_POSTER_SIDE} 像素，请先缩小")
    margin = max(16, round(width * 0.06))
    return {
        "version": 1,
        "width": width,
        "height": height,
        "background": asset_ref(asset),
        "layers": [
            {
                "id": "t_title",
                "type": "text",
                "x": float(margin),
                "y": float(round(height * 0.08)),
                "width": float(width - margin * 2),
                "text": "标题",
                "fontSize": float(max(24, round(height * 0.07))),
                "fontFamily": "sans-serif",
                "color": "#ffffff",
                "bold": True,
                "align": "center",
            }
        ],
    }


def new_text_layer(recipe: Mapping[str, Any], order: int) -> dict[str, Any]:
    width = int(recipe["width"])
    height = int(recipe["height"])
    margin = max(16, round(width * 0.06))
    return {
        "id": f"t_{order}",
        "type": "text",
        "x": float(margin),
        "y": float(round(height * 0.7)),
        "width": float(width - margin * 2),
        "text": "新文字",
        "fontSize": float(max(18, round(height * 0.045))),
        "fontFamily": "sans-serif",
        "color": "#ffffff",
        "bold": False,
        "align": "left",
    }
