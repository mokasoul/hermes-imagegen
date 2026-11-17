"""Prompt-template library: bundled snapshots, search, favourites, URL safety."""

from __future__ import annotations

import pytest

from imagegen_backend import templates


@pytest.fixture(autouse=True)
def fresh_library():
    templates.reset_for_tests()
    yield
    templates.reset_for_tests()


def test_five_sources_are_registered():
    metas = templates.source_meta()
    ids = [meta["id"] for meta in metas]
    assert ids == ["vibeui", "canghe", "handraw", "prompt-signal", "evolink"]
    assert [meta["refreshable"] for meta in metas] == [True, True, False, False, False]
    assert all(meta["label"] and meta["homepage"] for meta in metas)


def test_bundled_snapshots_load_offline():
    library = templates.library()
    for source_id in templates.SOURCES:
        snapshot = library.snapshot(source_id)
        assert snapshot["origin"] == "bundled"
        assert len(snapshot["cases"]) > 400
        case = snapshot["cases"][0]
        assert case["id"] and case["title"] and case["prompt"]
        assert case["categoryZh"]  # zh label resolved, never empty


def test_list_paginates_and_reports_categories():
    library = templates.library()
    page = library.list("vibeui", offset=0, limit=5)
    assert page["total"] > 5
    assert len(page["cases"]) == 5
    assert page["categories"]
    second = library.list("vibeui", offset=5, limit=5)
    assert second["cases"][0]["id"] != page["cases"][0]["id"]


def test_search_matches_title_prompt_and_tags():
    library = templates.library()
    page = library.list("vibeui", query="3D")
    assert page["total"] > 0
    assert all(
        "3d" in (case["title"] + case["prompt"] + " ".join(case["styles"]) + " ".join(case["scenes"])).lower()
        for case in page["cases"]
    )


def test_category_filter_narrows_results():
    library = templates.library()
    everything = library.list("vibeui", limit=1)
    category = everything["categories"][0]["id"]
    filtered = library.list("vibeui", category=category)
    assert filtered["total"] > 0
    assert {case["category"] for case in filtered["cases"]} == {category}


def test_sample_and_unknown_ids():
    library = templates.library()
    first = library.list("vibeui", limit=1)["cases"][0]
    assert library.sample("vibeui", first["id"])["title"] == first["title"]
    with pytest.raises(templates.TemplateError):
        library.sample("vibeui", "no-such-case")
    with pytest.raises(templates.TemplateError):
        library.list("nope")


def test_favourites_roundtrip(tmp_path):
    library = templates.library()
    case = library.list("canghe", limit=1)["cases"][0]
    added = library.add_favorite("canghe", case["id"])
    assert len(added["favorites"]) == 1
    assert added["favorites"][0]["prompt"] == case["prompt"]
    # idempotent
    assert len(library.add_favorite("canghe", case["id"])["favorites"]) == 1
    assert library.remove_favorite("canghe", case["id"])["favorites"] == []


def test_non_refreshable_source_refuses_online_refresh():
    library = templates.library()
    with pytest.raises(templates.TemplateError) as caught:
        import asyncio

        asyncio.run(library.refresh("handraw"))
    assert "内置快照" in str(caught.value)


def test_image_urls_are_resolved_from_the_case_not_the_request():
    library = templates.library()
    vibeui = templates.SOURCES["vibeui"]
    assert library._candidate_urls(vibeui, "case444.jpg") == [
        "https://vibeui.top/extra/awesome-gpt-image-2/data/images/case444.jpg"
    ]
    handraw = templates.SOURCES["handraw"]
    cdn = "https://cdn.jsdelivr.net/gh/yang0/handraw-style@main/docs/a.png"
    assert library._candidate_urls(handraw, cdn) == [
        cdn,
        "https://raw.githubusercontent.com/yang0/handraw-style/main/docs/a.png",
    ]


def test_foreign_image_host_is_refused():
    library = templates.library()
    with pytest.raises(templates.TemplateError) as caught:
        library._candidate_urls(templates.SOURCES["prompt-signal"], "https://evil.example.com/a.png")
    assert "白名单" in str(caught.value)
