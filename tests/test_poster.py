"""Poster recipes: defaults, invariants, and the canvas save path."""

from __future__ import annotations

import pytest
from conftest import png_bytes

from imagegen_backend import canvas, poster


def _asset(asset_id="a" * 32):
    return {"id": asset_id, "mime": "image/png", "name": "bg.png"}


def _recipe(**overrides):
    recipe = {
        "version": 1,
        "width": 1024,
        "height": 1536,
        "background": _asset(),
        "layers": [
            {"id": "t1", "type": "text", "x": 40, "y": 60, "width": 944, "text": "新品上市",
             "fontSize": 88, "fontFamily": "sans-serif", "color": "#FFFFFF", "bold": True, "align": "center"}
        ],
    }
    recipe.update(overrides)
    return recipe


def test_default_recipe_reads_the_image_size():
    data = png_bytes(120, 80)
    recipe = poster.default_recipe(_asset(), data)
    assert recipe["width"] == 120 and recipe["height"] == 80
    assert recipe["layers"][0]["text"] == "标题"
    assert recipe["layers"][0]["align"] == "center"
    # the default recipe must itself validate
    poster.validate(recipe)


def test_colour_is_lowercased_and_floats_are_fine():
    checked = poster.validate(_recipe())
    assert checked["layers"][0]["color"] == "#ffffff"
    assert checked["width"] == 1024


def test_bad_dimensions_are_refused():
    with pytest.raises(poster.PosterError):
        poster.validate(_recipe(width=0))
    with pytest.raises(poster.PosterError):
        poster.validate(_recipe(width=5000))


def test_unknown_layer_type_is_named():
    with pytest.raises(poster.PosterError) as caught:
        poster.validate(_recipe(layers=[{"id": "x", "type": "sticker", "x": 0, "y": 0, "width": 10}]))
    assert "sticker" in str(caught.value)


def test_duplicate_layer_ids_are_refused():
    layer = _recipe()["layers"][0]
    with pytest.raises(poster.PosterError) as caught:
        poster.validate(_recipe(layers=[layer, dict(layer)]))
    assert "重复" in str(caught.value)


def test_layer_outside_the_canvas_is_refused():
    layer = dict(_recipe()["layers"][0], x=1024)
    with pytest.raises(poster.PosterError) as caught:
        poster.validate(_recipe(layers=[layer]))
    assert "超出画布" in str(caught.value)


def test_text_layer_rules():
    base = _recipe()["layers"][0]
    for patch, needle in (
        ({"fontSize": 4}, "字号"),
        ({"color": "white"}, "颜色"),
        ({"fontFamily": "comic"}, "字体"),
        ({"align": "justify"}, "对齐"),
        ({"text": "x" * 4001}, "内容"),
    ):
        with pytest.raises(poster.PosterError) as caught:
            poster.validate(_recipe(layers=[dict(base, **patch)]))
        assert needle in str(caught.value), needle


def test_logo_layer_requires_an_image_asset():
    logo = {"id": "l1", "type": "image", "x": 10, "y": 10, "width": 200, "height": 100, "asset": _asset()}
    assert poster.validate(_recipe(layers=[logo]))["layers"][0]["type"] == "image"
    bad = dict(logo, asset={"id": "b" * 32, "mime": "text/plain", "name": ""})
    with pytest.raises(poster.PosterError) as caught:
        poster.validate(_recipe(layers=[bad]))
    assert "PNG/JPEG" in str(caught.value)


def test_background_must_be_an_image():
    with pytest.raises(poster.PosterError):
        poster.validate(_recipe(background={"id": "c" * 32, "mime": "application/pdf", "name": ""}))


def test_too_many_layers_are_refused():
    layer = _recipe()["layers"][0]
    many = [dict(layer, id=f"t{index}") for index in range(poster.MAX_POSTER_LAYERS + 1)]
    with pytest.raises(poster.PosterError) as caught:
        poster.validate(_recipe(layers=many))
    assert "图层最多" in str(caught.value)


def test_canvas_save_validates_the_recipe():
    page = canvas.create_page("海报")
    document = dict(page)
    document["nodes"] = [
        {"id": "n1", "type": "image", "title": "底图", "x": 0, "y": 0, "width": 260, "height": 260,
         "metadata": {"asset": _asset(), "poster": _recipe()}}
    ]
    saved = canvas.save_page(page["id"], document)
    assert saved["nodes"][0]["metadata"]["poster"]["layers"][0]["text"] == "新品上市"

    broken = dict(document)
    broken["nodes"] = [dict(document["nodes"][0], metadata={"asset": _asset(), "poster": _recipe(width=-5)})]
    with pytest.raises(canvas.CanvasError) as caught:
        canvas.save_page(page["id"], broken)
    assert "宽高" in str(caught.value)
