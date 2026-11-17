"""E-commerce product sets: slot presets, visual briefs and the prompt composer.

Port of the pure logic from the upstream panel (``PRODUCT_SET_SLOTS``,
``ecommerceVisualBrief``, ``ecommercePrompt``, ``ecommerceSlotPrompt``,
``withAnchorNote``). The upstream feature lives inside a 3.8k-line React panel;
the part worth porting is the *prompt contract*, because that is what makes a
set look like one product instead of six unrelated pictures.

Generation itself is not special-cased: the panel submits these prompts to the
normal job pipeline one image at a time.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

MAX_SLOTS = 12
MAX_IMAGES_PER_SLOT = 4
ROLE_LABELS = {
    "product": "商品主体",
    "packaging": "包装",
    "detail": "细节/角度",
    "style": "风格参考",
}
DEFAULT_INFO = (
    "突出商品真实材质、结构和核心价值；保持商品颜色、形状、Logo、包装文字和结构真实，不添加不存在的配件"
)
ANCHOR_PREFIX = (
    "商品套图一致性约束：附件是本套商品的主图，图中商品（外形、颜色、材质、Logo、包装文字）必须与附件完全一致，"
    "不得重新发明商品。"
)

SLOTS: tuple[dict[str, Any], ...] = (
    {"key": "main", "label": "主图", "description": "干净背景，突出商品主体", "count": 1, "enabled": True, "refRole": "product"},
    {"key": "selling-point", "label": "卖点图", "description": "用画面展示商品核心卖点", "count": 2, "enabled": True, "refRole": "product"},
    {"key": "scene", "label": "场景图", "description": "真实生活或使用场景", "count": 2, "enabled": True, "refRole": "product"},
    {"key": "detail", "label": "细节图", "description": "材质、结构或工艺特写", "count": 1, "enabled": True, "refRole": "detail"},
    {"key": "spec", "label": "规格图", "description": "尺寸、容量或参数展示", "count": 1, "enabled": False, "refRole": "product"},
    {"key": "model", "label": "使用图", "description": "人物上手或穿戴效果", "count": 1, "enabled": False, "refRole": "product"},
)

VISUAL_BRIEFS: dict[str, list[str]] = {
    "main": [
        "以纯净高级背景完整展示商品主体，突出正面与侧面结构、材质和屏幕/镜头等关键设计，商品居中但保留自然阴影和真实比例"
    ],
    "selling-point": [
        "制作卖点视觉图：用局部放大、功能结果或使用场景形成构图分区，突出核心功能和购买理由，不要只是商品正面照",
        "制作第二张卖点视觉图：聚焦差异化优势和附加价值，使用与第一张不同的视角、背景、光线和视觉重点",
    ],
    "scene": [
        "制作真实生活场景图：商品自然融入室内或日常环境，加入合理背景道具和环境光，呈现使用氛围，不要白底棚拍",
        "制作第二张场景图：换成不同环境或使用时刻，如户外、办公或休闲场景，机位、背景、光线和整体氛围必须与第一张明显不同",
    ],
    "detail": ["制作微距细节图：近距离突出材质、工艺、接口或结构，浅景深、高细节，构图聚焦局部而非完整商品"],
    "spec": ["制作规格信息图：突出尺寸、容量或参数关系，使用清晰的辅助构图和留白，不复制主图构图"],
    "model": ["制作人物使用图：展示手持、佩戴或实际使用状态，突出人与商品的交互和真实尺度"],
}
FALLBACK_BRIEF = "使用与同组其他图片不同的构图、视角、背景和视觉重点"


class EcommerceError(ValueError):
    """Rejected draft — the message names the offending field."""


def slot_presets() -> list[dict[str, Any]]:
    return [dict(slot) for slot in SLOTS]


def visual_brief(slot_key: str, index: int) -> str:
    choices = VISUAL_BRIEFS.get(slot_key) or [FALLBACK_BRIEF]
    return choices[min(max(0, index), len(choices) - 1)]


def effective_language(draft: Mapping[str, Any]) -> str:
    language = str(draft.get("language") or "中文").strip()
    if language == "custom":
        return str(draft.get("customLanguage") or "").strip()
    return language


def compose_prompt(draft: Mapping[str, Any], slot: Mapping[str, Any], index: int = 0) -> str:
    """One image's prompt — the exact contract upstream uses."""
    slot_key = str(slot.get("key") or "")
    slot_label = str(slot.get("label") or slot_key)
    info = str(draft.get("promptInfo") or "").strip() or DEFAULT_INFO
    language = effective_language(draft) or "中文"
    product = str(draft.get("productName") or "").strip() or "该商品"
    role = slot.get("refRole")
    ref_clause = (
        f"本图以上传的{ROLE_LABELS.get(str(role), '')}图片为参考，商品与风格必须与参考图保持一致；"
        if role and role != "none" and str(role) in ROLE_LABELS
        else ""
    )
    distinct = (
        ""
        if slot_key == "main"
        else "这不是商品主图的简单复刻：只复用参考图中的商品外观和身份，必须重新设计背景、机位、构图、道具和光效，"
        "不得输出与主图相同的白底正面居中画面。"
    )
    category = str(draft.get("category") or "").strip() or "未指定"
    platform = str(draft.get("platform") or "").strip() or "未指定"
    return (
        f"生成一张电商{slot_label}。视觉目标：{visual_brief(slot_key, index)}。{distinct}"
        f"商品身份与一致性约束：{product}的外形、比例、颜色、材质、镜头模组、Logo 与核心结构必须与"
        f"{'文字描述' if ref_clause == '' else '参考图'}保持一致，只允许改变场景、视角、构图和光效，不得重新发明或替换商品。"
        f"商品品类：{category}；平台：{platform}；语言：{language}。{ref_clause}"
        f"商品信息与要求：{info}。整体要求：商品主体清晰、比例真实、光线自然、细节真实、适合电商发布。"
    )


def slot_prompt(draft: Mapping[str, Any], slot: Mapping[str, Any], index: int) -> str:
    """A per-image override (from the preview board) wins over the composed prompt."""
    overrides = draft.get("promptOverrides") or {}
    if isinstance(overrides, Mapping):
        override = overrides.get(f"{slot.get('key')}-{index + 1}")
        if isinstance(override, str) and override.strip():
            return override
    return compose_prompt(draft, slot, index)


def with_anchor(prompt: str) -> str:
    """Consistency prefix for slots generated after the main image exists."""
    return ANCHOR_PREFIX + prompt


def normalise_slots(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        return slot_presets()
    if len(raw) > MAX_SLOTS:
        raise EcommerceError(f"最多 {MAX_SLOTS} 个 slot")
    slots: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise EcommerceError("slot 必须是对象")
        key = str(entry.get("key") or "").strip()
        if not re.match(r"^[a-z][a-z0-9-]{0,31}$", key):
            raise EcommerceError(f"slot key 非法：{key or '(空)'}")
        if key in seen:
            raise EcommerceError(f"slot key 重复：{key}")
        seen.add(key)
        count = entry.get("count", 1)
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= MAX_IMAGES_PER_SLOT:
            raise EcommerceError(f"slot {key} 的 count 必须是 1–{MAX_IMAGES_PER_SLOT} 的整数")
        slots.append(
            {
                "key": key,
                "label": str(entry.get("label") or key),
                "description": str(entry.get("description") or ""),
                "count": count,
                "enabled": bool(entry.get("enabled", True)),
                "refRole": str(entry.get("refRole") or "product"),
            }
        )
    return slots


def plan(draft: Mapping[str, Any]) -> dict[str, Any]:
    """The pre-generation board: one entry per image, prompts editable."""
    if not isinstance(draft, Mapping):
        raise EcommerceError("草稿必须是对象")
    slots = normalise_slots(draft.get("slots"))
    items: list[dict[str, Any]] = []
    for slot in slots:
        if not slot["enabled"]:
            continue
        for index in range(slot["count"]):
            items.append(
                {
                    "slotKey": slot["key"],
                    "slotLabel": slot["label"],
                    "slotDescription": slot["description"],
                    "index": index,
                    "refRole": slot["refRole"],
                    "prompt": slot_prompt(draft, slot, index),
                }
            )
    if not items:
        raise EcommerceError("没有启用任何 slot，先勾选要生成的类型")
    return {
        "projectName": str(draft.get("projectName") or "").strip() or str(draft.get("productName") or "").strip() or "电商套图",
        "size": str(draft.get("size") or "1:1"),
        "total": len(items),
        "slots": [slot for slot in slots if slot["enabled"]],
        "items": items,
    }
