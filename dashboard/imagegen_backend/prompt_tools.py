"""Prompt helpers: expand, polish and reverse-engineer a prompt with a chat model.

Port of the upstream ``src/prompt-enhancer.ts`` (enhance/polish) and the
reverse-engineering instructions from ``src/skill-runner.ts``.

The chat model rides the **user's own channel**: an OpenAI-compatible
``/chat/completions`` call with that channel's key. When the channel has no
usable chat model the UI hides the buttons and the API says so — this port never
invents a model or falls back to another endpoint silently.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

import httpx

from . import engine, models

MAX_VISION_IMAGES = 6
CHAT_TIMEOUT_S = 120.0

REVERSE_DETAIL_HINT: dict[str, dict[str, str]] = {
    "brief": {
        "zh": "长度：60 词以内，只写决定主体与风格的关键线索。",
        "en": "Length: under 60 words; only the cues that decide subject and style.",
    },
    "standard": {
        "zh": "长度：约 120 词，覆盖主体、构图、光线、色彩与风格。",
        "en": "Length: about 120 words, covering subject, composition, lighting, colour and style.",
    },
    "detailed": {
        "zh": "长度：可到 220 词，逐项写清镜头、光线、材质、后期与画质词。",
        "en": "Length: up to 220 words; spell out lens, lighting, materials, post and quality terms.",
    },
}

ENHANCE_SYSTEM = (
    "You are an expert image-prompt editor. Expand the user request into one vivid, specific "
    "image-generation prompt. Preserve intent and language. Add only useful visual detail: subject, "
    "composition, lighting, materials, color, camera/style and quality. Return only the finished "
    "prompt, with no preface or markdown."
)

POLISH_SYSTEM = (
    "你是电商生图提示词编辑。请在保持图片用途、商品事实、平台、语言和原有结构不变的前提下，"
    "润色文字并补充可执行的视觉细节，减少重复和空泛表达。只输出最终提示词，不要解释、标题或 Markdown。"
)


def reverse_system(language: str, detail: str, count: int) -> str:
    """System prompt for prompt reverse-engineering (verbatim from upstream)."""
    hint = REVERSE_DETAIL_HINT.get(detail, REVERSE_DETAIL_HINT["standard"])
    if language == "zh":
        lines = [
            "你是图像逆向提示词工程师：读图，写出一段可以直接喂给文生图模型、能尽量复现这张图的提示词。",
            "规则：",
            "1. 只输出提示词正文。不要解释、不要标题、不要 markdown、不要用引号把整段包起来。",
            "2. 组织顺序：主体与动作 → 构图与镜头（视角/景别/焦段）→ 光线与氛围 → 色彩与影调 → 材质与细节 → 风格与媒介（摄影/插画/3D/胶片…）→ 画质与参数词。",
            "3. 只写看得见的具体信息：「85mm 定焦、f/1.8、浅景深、背景奶油散景」而不是「虚化」；「浅金色晨光从左侧 45° 打入，地面拉出长阴影」而不是「光线很好」。",
            "4. 图里有文字时，用引号原样抄录关键文字并说明它的位置与层级；不要编造品牌名、作者名或水印。",
            "5. 多图时：第一张定主体与构图，其余只贡献风格、材质、配色线索，并在结尾注明「其余参考图提供风格」。",
            "6. 不堆砌「高清、8K、杰作、最佳画质」这类空词；只有原图确实呈现该质感时才写。",
            f"7. {hint['zh']}",
            "8. 这是一组参考图，写成一段整体提示词，不要逐张分写。" if count > 1 else "",
        ]
        return "\n".join(line for line in lines if line)
    lines = [
        "You are an image prompt reverse-engineer: read the picture and write one prompt that a "
        "text-to-image model could use to reproduce it.",
        "Rules:",
        "1. Output the prompt only - no explanation, no heading, no markdown, no surrounding quotes.",
        "2. Order: subject and action -> composition and lens (angle, framing, focal length) -> "
        "lighting and atmosphere -> colour and tonality -> materials and detail -> style and medium "
        "(photo/illustration/3D/film) -> quality and parameter terms.",
        "3. Only concrete, visible facts: \"85mm prime, f/1.8, shallow depth of field, creamy "
        "background bokeh\" rather than \"blurred\"; \"low golden morning light from camera left at "
        "45 degrees, long shadows on the floor\" rather than \"good lighting\".",
        "4. When the image contains text, quote the key wording verbatim and say where it sits and "
        "how prominent it is; never invent brands, authors or watermarks.",
        "5. With several references: the first sets subject and composition, the others only "
        "contribute style, material and palette cues; say so at the end.",
        "6. No filler (\"high quality, 8K, masterpiece\"); only claim a look the source actually shows.",
        f"7. {hint['en']}",
        "8. These are reference images: write one combined prompt, not one per image." if count > 1 else "",
    ]
    return "\n".join(line for line in lines if line)


def chat_endpoint(channel: Mapping[str, Any]) -> str:
    base = engine.normalize_base_url(channel)
    if models.is_chat_completions_url(base) or channel.get("api_url_full") is True:
        return base
    return f"{base}/chat/completions"


async def list_models(channel: Mapping[str, Any], transport: httpx.AsyncBaseTransport | None = None) -> list[str]:
    """Plain ``/models`` — the chat list must be exactly what the endpoint exposes."""
    base = engine.normalize_base_url(channel)
    key = engine._api_key(channel)
    url = f"{base}/models"
    async with httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=15.0), follow_redirects=True, transport=transport) as client:
        response = await client.get(url, headers={"authorization": f"Bearer {key}"})
    if response.status_code >= 400:
        raise engine.ImageGenError(f"读取模型列表失败（HTTP {response.status_code} · {engine.display_url(url)}）", "upstream-rejected")
    try:
        payload = response.json()
    except ValueError as error:
        raise engine.ImageGenError("模型列表返回了非 JSON 响应", "upstream-invalid") from error
    ids = [str(entry.get("id")) for entry in (payload.get("data") or []) if isinstance(entry, dict) and entry.get("id")]
    return sorted(set(ids))


def chat_model_for(channel: Mapping[str, Any], requested: Any = None) -> str:
    """The chat model to use: explicit request, else the first non-image id."""
    wanted = str(requested or "").strip()
    if wanted:
        return wanted
    candidates = [str(model) for model in (channel.get("models") or [])]
    for candidate in candidates:
        if not models.is_likely_image_model_id(candidate) and models.model_family(candidate) == "unknown":
            return candidate
    raise engine.ImageGenError(
        "该渠道没有可用的对话模型：请在里面挑一个文本模型（如 deepseek-chat / gpt-4o-mini）",
        "prompt-model-missing",
    )


async def chat_complete(
    channel: Mapping[str, Any],
    model: str,
    *,
    system: str,
    content: Any,
    temperature: float = 0.4,
    max_tokens: int | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """One chat completion, reasoning artifacts stripped, empty answers rejected."""
    url = chat_endpoint(channel)
    key = engine._api_key(channel)
    body: dict[str, Any] = {
        "model": model,
        "temperature": temperature,
        "stream": False,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ],
    }
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(CHAT_TIMEOUT_S, connect=30.0), follow_redirects=True, transport=transport
    ) as client:
        try:
            response = await client.post(url, headers={"authorization": f"Bearer {key}", "content-type": "application/json"}, json=body)
        except httpx.TimeoutException as error:
            raise engine.ImageGenError("提示词模型响应超时（120 秒）", "upstream-timeout") from error
        except httpx.HTTPError as error:
            raise engine.ImageGenError(f"无法连接提示词模型：{error} · {engine.display_url(url)}", "upstream-unreachable") from error
    try:
        payload = response.json()
    except ValueError as error:
        raise engine.ImageGenError(
            f"提示词模型返回了非 JSON 响应（HTTP {response.status_code}）", "upstream-invalid"
        ) from error
    if response.status_code >= 400 or not isinstance(payload, dict):
        raise engine.ImageGenError(
            f"{engine._error_message(payload, response.status_code)}（HTTP {response.status_code} · {engine.display_url(url)}）",
            "upstream-rejected",
        )
    choices = payload.get("choices")
    first = choices[0] if isinstance(choices, list) and choices else None
    content_value = first.get("message", {}).get("content") if isinstance(first, dict) else None
    if isinstance(content_value, list):
        content_value = "\n".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content_value
        )
    if not isinstance(content_value, str) or not content_value.strip():
        raise engine.ImageGenError("提示词模型返回了空答案", "upstream-empty")
    answer = engine._strip_chat_reasoning(content_value)
    if not answer:
        raise engine.ImageGenError("提示词模型只返回了思考内容（<think> 里没有正文）", "upstream-empty")
    return answer


async def enhance_prompt(
    channel: Mapping[str, Any], model: str, prompt: str, transport: httpx.AsyncBaseTransport | None = None
) -> str:
    return await chat_complete(channel, model, system=ENHANCE_SYSTEM, content=prompt, temperature=0.7, transport=transport)


async def polish_prompt(
    channel: Mapping[str, Any], model: str, prompt: str, transport: httpx.AsyncBaseTransport | None = None
) -> str:
    return await chat_complete(channel, model, system=POLISH_SYSTEM, content=prompt, temperature=0.35, transport=transport)


async def reverse_prompt(
    channel: Mapping[str, Any],
    model: str,
    images: list[engine.ReferenceImage],
    *,
    detail: str = "standard",
    output: str = "zh",
    instruction: str = "",
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """Read reference images and write one reproducing prompt."""
    if not images:
        raise engine.ImageGenError("反推需要至少一张图片", "edit-image-missing")
    chosen = images[:MAX_VISION_IMAGES]
    language = "en" if output == "en" else "zh"
    parts: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": f"请反推这 {len(chosen)} 张参考图的生成提示词。" if language == "zh" else f"Reverse-engineer the generation prompt for {len(chosen)} reference image(s).",
        }
    ]
    for image in chosen:
        parts.append({"type": "image_url", "image_url": {"url": engine._data_url_of(image)}})
    if instruction.strip():
        parts.append({"type": "text", "text": ("额外要求：" if language == "zh" else "Extra requirement: ") + instruction.strip()})
    return await chat_complete(
        channel,
        model,
        system=reverse_system(language, detail if detail in REVERSE_DETAIL_HINT else "standard", len(chosen)),
        content=parts,
        temperature=0.35,
        transport=transport,
    )


def payload_json(content: str) -> Any:
    """Strip a fence and pull the outermost JSON value (for future skill steps)."""
    cleaned = engine._strip_chat_reasoning(content)
    start_obj, end_obj = cleaned.find("{"), cleaned.rfind("}")
    start_arr, end_arr = cleaned.find("["), cleaned.rfind("]")
    use_array = start_arr >= 0 and end_arr > start_arr and (start_obj < 0 or start_arr < start_obj)
    start, end = (start_arr, end_arr) if use_array else (start_obj, end_obj)
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(cleaned[start : end + 1])
    except ValueError:
        return None
