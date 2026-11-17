"""Prompt-template library: five independent sources, bundled snapshots.

Port of the upstream ``src/templates-store.ts``. Each source is fully
independent: its own snapshot JSON (shipped with the package, so the library
works offline), its own refreshed runtime copy, its own image cache directory.
Sources never mix — the UI shows one tab per source and every request names it.

Image refs are resolved **server-side from the loaded case**, never taken from
the request: a client can ask for ``/templates/image/<source>/<case_id>`` and
nothing else, so a template case cannot be used to make the gateway fetch an
arbitrary URL (the upstream plugin's allow-list of image hosts is enforced the
same way, one step earlier).

Bundled material and its licences are recorded in NOTICE: yang0/handraw-style
(MIT), andy7076/image_prompt (MIT), EvoLinkAI/awesome-gpt-image-2-prompts
(CC0), plus the vibeui/canghe galleries' published JSON.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Mapping

import httpx

from . import paths

FETCH_TIMEOUT_S = 30.0
SNAPSHOT_TTL_S = 6 * 3600
MAX_IMAGE_BYTES = 12 * 1024 * 1024

_DATA_DIR = Path(__file__).resolve().parent / "templates_data"

CATEGORY_ZH: dict[str, str] = {
    "Architecture & Spaces": "建筑与空间",
    "Brand & Logos": "品牌与标志",
    "Brand & Icons": "品牌与图标",
    "Characters & People": "人物与角色",
    "Charts & Infographics": "图表与信息可视化",
    "Commerce & Product": "电商与产品",
    "E-commerce & Product": "电商与产品",
    "Editorial & Posters": "编辑与海报",
    "Food & Drink": "食物与饮品",
    "Illustration & Art": "插画与艺术",
    "Interior & Objects": "室内与静物",
    "Landscapes & Nature": "风景与自然",
    "Portraits & People": "肖像与人物",
    "Posters & Typography": "海报与排版",
    "Tech & UI": "科技与界面",
    "UI & Interface": "界面与交互",
    "Vehicles & Machines": "载具与机械",
}

SOURCES: dict[str, dict[str, Any]] = {
    "vibeui": {
        "id": "vibeui",
        "label": "精选案例库",
        "homepage": "https://vibeui.top/",
        "description": "awesome-gpt-image-2 精选提示词案例（vibeui.top 镜像）",
        "snapshot": "cases.json",
        "list_url": "https://vibeui.top/extra/awesome-gpt-image-2/data/cases.json",
        "image_base_url": "https://vibeui.top/extra/awesome-gpt-image-2/data/images/",
        "refreshable": True,
    },
    "canghe": {
        "id": "canghe",
        "label": "沧河案例库",
        "homepage": "https://gpt-image2.canghe.ai/",
        "description": "GPT-Image2 Prompt Gallery（gpt-image2.canghe.ai，定期更新）",
        "snapshot": "canghe-cases.json",
        "list_url": "https://gpt-image2.canghe.ai/cases.json",
        "image_base_url": "https://gpt-image2.canghe.ai/images/",
        "refreshable": True,
    },
    "handraw": {
        "id": "handraw",
        "label": "手绘模板库",
        "homepage": "https://github.com/yang0/handraw-style",
        "description": "手绘风格、排版图型与主题色图鉴（yang0/handraw-style）",
        "snapshot": "handraw-cases.json",
        "refreshable": False,
        "allowed_hosts": ("cdn.jsdelivr.net", "raw.githubusercontent.com"),
    },
    "prompt-signal": {
        "id": "prompt-signal",
        "label": "Prompt/Signal",
        "homepage": "https://github.com/andy7076/image_prompt",
        "description": "andy7076/image_prompt 的 570+ 精选生图提示词",
        "snapshot": "prompt-signal-cases.json",
        "refreshable": False,
        "allowed_hosts": ("raw.githubusercontent.com", "pbs.twimg.com", "mosaic.fxtwitter.com"),
    },
    "evolink": {
        "id": "evolink",
        "label": "GPT Image 2 案例库",
        "homepage": "https://github.com/EvoLinkAI/awesome-gpt-image-2-prompts",
        "description": "EvoLink 整理的 GPT Image 2 电商、广告、人像、海报、角色与 UI 案例",
        "snapshot": "evolink-cases.json",
        "refreshable": False,
        "allowed_hosts": ("raw.githubusercontent.com",),
    },
}

DEFAULT_SOURCE = "vibeui"


class TemplateError(ValueError):
    """Rejected template request (unknown source/case, non-refreshable source…)."""


def source_meta() -> list[dict[str, Any]]:
    return [
        {
            "id": source["id"],
            "label": source["label"],
            "homepage": source["homepage"],
            "description": source["description"],
            "refreshable": bool(source.get("refreshable")),
        }
        for source in SOURCES.values()
    ]


def _source(source_id: str) -> dict[str, Any]:
    source = SOURCES.get((source_id or DEFAULT_SOURCE).strip())
    if source is None:
        raise TemplateError(f"未知模板库：{source_id}")
    return source


def _runtime_dir() -> Path:
    root = paths.data_root() / "templates"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _image_dir(source_id: str) -> Path:
    root = paths.data_root() / "template-images" / source_id
    root.mkdir(parents=True, exist_ok=True)
    return root


def _favorites_path() -> Path:
    return paths.data_root() / "template-favorites.json"


def _normalise_case(raw: Mapping[str, Any]) -> dict[str, Any]:
    category = str(raw.get("category") or "")
    category_zh = str(raw.get("categoryZh") or "") or CATEGORY_ZH.get(category, category)
    return {
        "id": str(raw.get("id") or ""),
        "title": str(raw.get("title") or ""),
        "prompt": str(raw.get("prompt") or ""),
        "category": category,
        "categoryZh": category_zh,
        "styles": [str(tag) for tag in (raw.get("styles") or [])],
        "scenes": [str(tag) for tag in (raw.get("scenes") or [])],
        "sourceLabel": str(raw.get("sourceLabel") or ""),
        "sourceUrl": str(raw.get("sourceUrl") or ""),
        "githubUrl": str(raw.get("githubUrl") or ""),
        "image": str(raw.get("image") or ""),
        "featured": bool(raw.get("featured")),
    }


class TemplateLibrary:
    """Snapshot loader with an on-disk runtime copy and an image cache."""

    def __init__(self) -> None:
        self._cache: dict[str, dict[str, Any]] = {}

    # --- snapshots -------------------------------------------------------

    def _read_snapshot(self, source: dict[str, Any]) -> dict[str, Any]:
        runtime = _runtime_dir() / f"{source['id']}.json"
        candidates = [runtime, _DATA_DIR / source["snapshot"]]
        for path in candidates:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            cases = [_normalise_case(case) for case in raw.get("cases") or [] if isinstance(case, dict)]
            origin = "runtime" if path == runtime else "bundled"
            return {
                "sourceId": source["id"],
                "origin": origin,
                "fetchedAt": raw.get("fetchedAt") or "",
                "repository": raw.get("repository") or "",
                "cases": cases,
            }
        raise TemplateError(f"模板库 {source['id']} 的快照缺失")

    def snapshot(self, source_id: str, force: bool = False) -> dict[str, Any]:
        source = _source(source_id)
        cached = self._cache.get(source["id"])
        if cached is not None and not force and time.time() - cached["loadedAt"] < SNAPSHOT_TTL_S:
            return cached
        snapshot = self._read_snapshot(source)
        snapshot["loadedAt"] = time.time()
        self._cache[source["id"]] = snapshot
        return snapshot

    async def refresh(self, source_id: str) -> dict[str, Any]:
        """Re-fetch a refreshable source's list into the runtime copy."""
        source = _source(source_id)
        if not source.get("refreshable") or not source.get("list_url"):
            raise TemplateError("该模板库为随插件版本更新的内置快照，暂不支持在线刷新")
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(FETCH_TIMEOUT_S, connect=15.0), follow_redirects=True) as client:
                response = await client.get(source["list_url"])
        except httpx.HTTPError as error:
            raise TemplateError(f"无法连接模板库源站（{source['id']}）：{error}") from error
        if response.status_code >= 400:
            raise TemplateError(f"模板库源站拒绝请求（HTTP {response.status_code}）")
        try:
            raw = response.json()
        except ValueError as error:
            raise TemplateError("模板库源站返回了非 JSON 内容") from error
        cases = raw.get("cases") if isinstance(raw, dict) else None
        if not isinstance(cases, list) or not cases:
            raise TemplateError("模板库源站返回的案例列表为空")
        target = _runtime_dir() / f"{source['id']}.json"
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        tmp.replace(target)
        snapshot = self.snapshot(source["id"], force=True)
        return {"sourceId": source["id"], "total": len(snapshot["cases"]), "fetchedAt": snapshot["fetchedAt"]}

    # --- queries ---------------------------------------------------------

    def list(
        self,
        source_id: str,
        query: str = "",
        category: str = "",
        offset: int = 0,
        limit: int = 60,
    ) -> dict[str, Any]:
        snapshot = self.snapshot(source_id)
        needle = (query or "").strip().lower()
        categories: dict[str, str] = {}
        matches: list[dict[str, Any]] = []
        for case in snapshot["cases"]:
            categories.setdefault(case["category"], case["categoryZh"])
            if category and case["category"] != category:
                continue
            if needle:
                haystack = " ".join(
                    [case["title"], case["prompt"], " ".join(case["styles"]), " ".join(case["scenes"]), case["sourceLabel"]]
                ).lower()
                if needle not in haystack:
                    continue
            matches.append(case)
        offset = max(0, int(offset))
        limit = max(1, min(120, int(limit)))
        return {
            "sourceId": snapshot["sourceId"],
            "origin": snapshot["origin"],
            "fetchedAt": snapshot["fetchedAt"],
            "total": len(matches),
            "offset": offset,
            "limit": limit,
            "cases": matches[offset : offset + limit],
            "categories": [{"id": key, "label": value} for key, value in sorted(categories.items())],
        }

    def sample(self, source_id: str, case_id: str) -> dict[str, Any]:
        snapshot = self.snapshot(source_id)
        for case in snapshot["cases"]:
            if case["id"] == str(case_id):
                return case
        raise TemplateError(f"模板库里没有案例 {case_id}")

    # --- images ----------------------------------------------------------

    @staticmethod
    def _candidate_urls(source: dict[str, Any], image_ref: str) -> list[str]:
        if image_ref.startswith(("http://", "https://")):
            allowed = source.get("allowed_hosts") or ()
            host = re.sub(r"^https?://([^/]+).*$", r"\1", image_ref)
            if allowed and host not in allowed:
                raise TemplateError(f"该案例的图片域名不在白名单里：{host}")
            candidates = [image_ref]
            match = re.match(r"^https://cdn\.jsdelivr\.net/gh/yang0/handraw-style@([^/]+)(/.*)$", image_ref, re.I)
            if match is not None:
                candidates.append(f"https://raw.githubusercontent.com/yang0/handraw-style/{match.group(1)}{match.group(2)}")
            return candidates
        base = source.get("image_base_url")
        if not base:
            raise TemplateError(f"模板库 {source['id']} 没有图片基址")
        return [base + image_ref.lstrip("/")]

    async def image(self, source_id: str, case_id: str) -> tuple[bytes, str]:
        """Cached bytes for one case's reference image."""
        source = _source(source_id)
        case = self.sample(source["id"], case_id)
        ref = case["image"]
        if not ref:
            raise TemplateError("该案例没有参考图")
        digest = hashlib.sha1(ref.encode("utf-8")).hexdigest()[:12]
        suffix = Path(ref.split("?")[0]).suffix or ".jpg"
        cached = _image_dir(source["id"]) / f"{case['id']}-{digest}{suffix}"
        if cached.exists():
            return cached.read_bytes(), _mime_for(suffix)
        errors: list[str] = []
        async with httpx.AsyncClient(timeout=httpx.Timeout(FETCH_TIMEOUT_S, connect=15.0), follow_redirects=True) as client:
            for url in self._candidate_urls(source, ref):
                try:
                    response = await client.get(url)
                except httpx.HTTPError as error:
                    errors.append(f"{url}: {error}")
                    continue
                if response.status_code >= 400:
                    errors.append(f"{url}: HTTP {response.status_code}")
                    continue
                data = response.content
                if not data:
                    errors.append(f"{url}: 空响应")
                    continue
                if len(data) > MAX_IMAGE_BYTES:
                    raise TemplateError("模板参考图超过 12MB 上限")
                cached.write_bytes(data)
                return data, response.headers.get("content-type", _mime_for(suffix)).split(";")[0]
        raise TemplateError(f"取参考图失败（{'；'.join(errors[:3])}）")

    # --- favourites ------------------------------------------------------

    def favorites(self) -> list[dict[str, Any]]:
        path = _favorites_path()
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []

    def _save_favorites(self, items: list[dict[str, Any]]) -> None:
        path = _favorites_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def add_favorite(self, source_id: str, case_id: str) -> dict[str, Any]:
        case = self.sample(source_id, case_id)
        items = [item for item in self.favorites() if not (item.get("sourceId") == source_id and item.get("id") == case["id"])]
        items.insert(0, {"sourceId": source_id, "id": case["id"], "title": case["title"], "prompt": case["prompt"], "addedAt": time.time()})
        self._save_favorites(items)
        return {"favorites": items}

    def remove_favorite(self, source_id: str, case_id: str) -> dict[str, Any]:
        items = [item for item in self.favorites() if not (item.get("sourceId") == source_id and item.get("id") == case_id)]
        self._save_favorites(items)
        return {"favorites": items}


def _mime_for(suffix: str) -> str:
    mapping = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}
    return mapping.get(suffix.lower(), "image/jpeg")


_library: TemplateLibrary | None = None


def library() -> TemplateLibrary:
    global _library
    if _library is None:
        _library = TemplateLibrary()
    return _library


def reset_for_tests() -> None:
    global _library
    _library = None
