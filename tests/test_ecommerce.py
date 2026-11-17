"""E-commerce set prompts: slot presets, briefs, consistency contract."""

from __future__ import annotations

import pytest

from imagegen_backend import ecommerce


def _draft(**overrides):
    draft = {
        "projectName": "植护洗衣液套图",
        "category": "日化",
        "platform": "抖音",
        "language": "中文",
        "size": "1:1",
        "productName": "植护洗衣液 2kg",
        "promptInfo": "瓶身蓝色，标签写「植护」，主打去渍",
    }
    draft.update(overrides)
    return draft


def test_slot_presets_match_upstream():
    slots = ecommerce.slot_presets()
    assert [slot["key"] for slot in slots] == ["main", "selling-point", "scene", "detail", "spec", "model"]
    assert [slot["count"] for slot in slots] == [1, 2, 2, 1, 1, 1]
    assert [slot["enabled"] for slot in slots] == [True, True, True, True, False, False]
    assert slots[3]["refRole"] == "detail"


def test_briefs_cycle_and_fall_back():
    assert ecommerce.visual_brief("scene", 0) != ecommerce.visual_brief("scene", 1)
    # beyond the last variant it repeats the last one instead of crashing
    assert ecommerce.visual_brief("scene", 5) == ecommerce.visual_brief("scene", 1)
    assert ecommerce.visual_brief("unknown-slot", 0) == ecommerce.FALLBACK_BRIEF


def test_prompt_carries_the_consistency_contract():
    slot = ecommerce.SLOTS[0]
    prompt = ecommerce.compose_prompt(_draft(), slot)
    assert "生成一张电商主图" in prompt
    assert "植护洗衣液 2kg" in prompt
    assert "不得重新发明或替换商品" in prompt
    assert "商品品类：日化；平台：抖音；语言：中文" in prompt
    assert "本图以上传的商品主体图片为参考" in prompt
    # the main slot must NOT get the "don't copy the main image" clause
    assert "这不是商品主图的简单复刻" not in prompt


def test_non_main_slots_get_the_distinctness_clause():
    prompt = ecommerce.compose_prompt(_draft(), ecommerce.SLOTS[1], 0)
    assert "这不是商品主图的简单复刻" in prompt
    assert "卖点图" in prompt


def test_defaults_when_the_draft_is_sparse():
    prompt = ecommerce.compose_prompt({}, ecommerce.SLOTS[0])
    assert ecommerce.DEFAULT_INFO in prompt
    assert "该商品" in prompt
    assert "未指定" in prompt


def test_custom_language_and_no_reference_role():
    draft = _draft(language="custom", customLanguage="English")
    prompt = ecommerce.compose_prompt(draft, {"key": "main", "label": "主图", "refRole": "none"})
    assert "语言：English" in prompt
    assert "本图以上传的" not in prompt


def test_overrides_win_per_image():
    draft = _draft(promptOverrides={"scene-2": "自定义第二张场景"})
    slot = next(entry for entry in ecommerce.SLOTS if entry["key"] == "scene")
    assert ecommerce.slot_prompt(draft, slot, 1) == "自定义第二张场景"
    assert "生成一张电商场景图" in ecommerce.slot_prompt(draft, slot, 0)


def test_plan_skips_disabled_slots_and_counts_images():
    result = ecommerce.plan(_draft())
    assert result["total"] == 6  # 1 + 2 + 2 + 1
    assert [item["slotKey"] for item in result["items"]] == [
        "main", "selling-point", "selling-point", "scene", "scene", "detail",
    ]
    assert result["projectName"] == "植护洗衣液套图"


def test_plan_honours_a_custom_slot_selection():
    result = ecommerce.plan(
        _draft(slots=[{"key": "main", "label": "主图", "count": 2, "enabled": True, "refRole": "product"}])
    )
    assert result["total"] == 2
    assert len(result["slots"]) == 1


def test_plan_refuses_when_nothing_is_enabled():
    with pytest.raises(ecommerce.EcommerceError) as caught:
        ecommerce.plan(_draft(slots=[{"key": "main", "label": "主图", "count": 1, "enabled": False}]))
    assert "没有启用任何 slot" in str(caught.value)


def test_slot_validation():
    with pytest.raises(ecommerce.EcommerceError):
        ecommerce.normalise_slots([{"key": "Main", "count": 1}])
    with pytest.raises(ecommerce.EcommerceError):
        ecommerce.normalise_slots([{"key": "main", "count": 0}])
    with pytest.raises(ecommerce.EcommerceError):
        ecommerce.normalise_slots([{"key": "main", "count": 9}])
    with pytest.raises(ecommerce.EcommerceError):
        ecommerce.normalise_slots([{"key": "main", "count": 1}, {"key": "main", "count": 1}])
    with pytest.raises(ecommerce.EcommerceError):
        ecommerce.normalise_slots([{"key": f"s{index}", "count": 1} for index in range(ecommerce.MAX_SLOTS + 1)])


def test_anchor_prefix_is_verbatim():
    assert ecommerce.with_anchor("X").endswith("X")
    assert ecommerce.with_anchor("X").startswith("商品套图一致性约束：附件是本套商品的主图")
