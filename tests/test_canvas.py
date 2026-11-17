"""Infinite canvas: page CRUD, optimistic concurrency, nodes, assets."""

from __future__ import annotations

import base64

import pytest
from conftest import png_bytes

from imagegen_backend import canvas


def _page_with_nodes():
    page = canvas.create_page("测试画布")
    document = dict(page)
    document["nodes"] = [
        {"id": "n_text", "type": "text", "title": "提示词", "x": 0, "y": 0, "width": 260, "height": 120,
         "metadata": {"text": "一只橘猫"}},
        {"id": "n_cfg", "type": "config", "title": "生成配置", "x": 320, "y": 0, "width": 240, "height": 200,
         "metadata": {"model": "gpt-image-2", "size": "1:1", "quality": "2k", "count": 2}},
        {"id": "n_img", "type": "image", "title": "结果", "x": 640, "y": 0, "width": 260, "height": 260,
         "metadata": {"asset": {"id": "abc123def456", "mime": "image/png", "name": "a.png"}, "status": "success"}},
    ]
    document["connections"] = [{"fromNodeId": "n_text", "toNodeId": "n_cfg"}, {"fromNodeId": "n_cfg", "toNodeId": "n_img"}]
    return page["id"], document


def test_create_list_read_roundtrip():
    page = canvas.create_page("我的画布")
    assert page["version"] == 2 and page["revision"] == 1
    pages = canvas.list_pages()
    assert [entry["id"] for entry in pages] == [page["id"]]
    assert pages[0]["nodeCount"] == 0
    assert canvas.read_page(page["id"])["title"] == "我的画布"


def test_save_bumps_revision_and_keeps_created_at():
    canvas_id, document = _page_with_nodes()
    before = canvas.read_page(canvas_id)
    saved = canvas.save_page(canvas_id, document, expected_revision=before["revision"])
    assert saved["revision"] == before["revision"] + 1
    assert saved["createdAt"] == before["createdAt"]
    assert len(saved["nodes"]) == 3
    assert len(saved["connections"]) == 2


def test_stale_revision_is_a_conflict():
    canvas_id, document = _page_with_nodes()
    current = canvas.read_page(canvas_id)
    canvas.save_page(canvas_id, document, expected_revision=current["revision"])
    with pytest.raises(canvas.CanvasConflict) as caught:
        canvas.save_page(canvas_id, document, expected_revision=current["revision"])
    assert "已被其他窗口修改" in str(caught.value)


def test_unknown_node_type_is_refused():
    canvas_id, document = _page_with_nodes()
    document["nodes"].append({"id": "n_bad", "type": "sketch", "x": 0, "y": 0})
    with pytest.raises(canvas.CanvasError) as caught:
        canvas.save_page(canvas_id, document)
    assert "不支持的节点类型" in str(caught.value)


def test_duplicate_node_ids_are_refused():
    canvas_id, document = _page_with_nodes()
    document["nodes"].append(dict(document["nodes"][0]))
    with pytest.raises(canvas.CanvasError) as caught:
        canvas.save_page(canvas_id, document)
    assert "重复" in str(caught.value)


def test_edges_to_missing_nodes_are_dropped_and_self_loops_ignored():
    canvas_id, document = _page_with_nodes()
    document["connections"] += [
        {"fromNodeId": "n_text", "toNodeId": "n_missing"},
        {"fromNodeId": "n_text", "toNodeId": "n_text"},
    ]
    saved = canvas.save_page(canvas_id, document)
    assert len(saved["connections"]) == 2
    assert all(edge["toNodeId"] != "n_missing" for edge in saved["connections"])


def test_viewport_is_clamped():
    canvas_id, document = _page_with_nodes()
    document["viewport"] = {"x": 10, "y": -20, "k": 99}
    saved = canvas.save_page(canvas_id, document)
    assert saved["viewport"]["k"] == 3.0
    document["viewport"] = {"x": 0, "y": 0, "k": 0.01}
    assert canvas.save_page(canvas_id, document)["viewport"]["k"] == 0.2


def test_unknown_background_falls_back_to_dots():
    canvas_id, document = _page_with_nodes()
    document["background"] = "galaxy"
    assert canvas.save_page(canvas_id, document)["background"] == "dots"


def test_favorite_and_remove():
    canvas_id, _ = _page_with_nodes()
    assert canvas.set_favorite(canvas_id, True)["favorite"] is True
    assert canvas.list_pages()[0]["favorite"] is True
    canvas.remove_page(canvas_id)
    with pytest.raises(canvas.CanvasError):
        canvas.read_page(canvas_id)
    assert canvas.list_pages() == []


def test_illegal_ids_are_refused():
    with pytest.raises(canvas.CanvasError):
        canvas.read_page("../etc/passwd")
    with pytest.raises(canvas.CanvasError):
        canvas.read_asset("../../x")


def test_assets_roundtrip_as_data_url_and_bytes():
    data = png_bytes(3, 3)
    asset = canvas.save_asset_data_url(f"data:image/png;base64,{base64.b64encode(data).decode()}", "a.png")
    assert len(asset["id"]) == 32
    read, mime = canvas.read_asset(asset["id"])
    assert read == data and mime == "image/png"
    assert canvas.asset_pool_size() == 1


def test_non_base64_asset_is_refused():
    with pytest.raises(canvas.CanvasError) as caught:
        canvas.save_asset_data_url("data:image/png,notbase64")
    assert "base64" in str(caught.value)


def test_empty_asset_is_refused():
    with pytest.raises(canvas.CanvasError) as caught:
        canvas.save_asset_data_url("data:image/png;base64,")
    assert "素材为空" in str(caught.value)
