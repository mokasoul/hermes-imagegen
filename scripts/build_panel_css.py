#!/usr/bin/env python3
"""Generate the plugin's stylesheets from the upstream DSH CSS modules.

The upstream panel ships as several CSS modules (``panel.module.css``,
``studio-results.module.css``, ``image-action-menu.module.css``,
``generation-loading.module.css``) using the DSH design tokens
(``--dsw-alias-*``). We cannot import a CSS module from a desktop plugin (one
plain ESM file, loaded uncompiled), so the stylesheets are generated once at
build/dev time and served by the plugin's own backend.

Each sheet gets its own scope root, because upstream classes collide once they
stop being hashed: ``panel.module.css`` and ``studio-results.module.css`` both
define ``.card``. Every rule therefore lands under the root the surface owns::

    panel.module.css          -> .ig-root      (the panel shell)
    studio-results.module.css -> .ig-results   (the generation board)
    image-action-menu.module.css -> .ig-menu   (the result action menu)
    generation-loading.module.css -> .ig-loading (the waiting animation)

Common transforms:

* every selector is scoped under the sheet's root so the file cannot leak into
  the app's own chrome (the upstream files have generic names like ``.card``);
* every ``--dsw-alias-*`` token is mapped to the matching Hermes app token with
  the DSH value kept as a fallback, so the panel follows the app's theme;
* ``@keyframes`` are renamed with an ``ig-`` prefix and their references
  rewritten, for the same collision reason.

Run: ``python scripts/build_panel_css.py`` (writes ``desktop/panel*.css``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DESIGN = ROOT / "design"
OUT_DIR = ROOT / "desktop"


@dataclass(frozen=True)
class Sheet:
    source: str
    out: str
    root: str
    # A sheet whose root class is applied to the outermost element the sheet
    # styles needs a "same element" selector variant (``.ig-loading.field``),
    # not only the descendant one (``.ig-loading .field``).
    self_match: bool = True


SHEETS = (
    # The panel shell keeps its historical single-root behaviour: its root class
    # sits on the same element as ``.panel``, which is special-cased below.
    Sheet("panel.module.css", "panel.css", ".ig-root", self_match=False),
    Sheet("studio-results.module.css", "panel-results.css", ".ig-results"),
    Sheet("image-action-menu.module.css", "panel-menu.css", ".ig-menu"),
    Sheet("generation-loading.module.css", "panel-loading.css", ".ig-loading"),
)

# DSH alias -> Hermes app tokens (first entry wins, the rest are fallbacks).
TOKENS = {
    "brand-primary": "--ui-accent",
    "label-primary": "--ui-text-primary",
    "label-secondary": "--ui-text-secondary",
    "label-tertiary": "--ui-text-tertiary",
    "label-quaternary": "--ui-text-quaternary",
    "label-dimmed": "--ui-text-quaternary",
    "label-error": "--ui-red",
    "status-danger": "--ui-red",
    "state-error": "--ui-red",
    "bg-base": "--ui-bg-primary",
    "bg-layer-1": "--ui-bg-secondary",
    "bg-layer-2": "--ui-bg-tertiary",
    "bg-layer-3": "--ui-bg-quaternary",
    "bg-mask-1": "--ui-bg-quaternary",
    "border-l1": "--ui-stroke-secondary",
    "border-l2": "--ui-stroke-primary",
    "interactive-bg-hover": "--ui-row-hover-background",
    "state-success-primary": "--ui-green",
    "state-warn-primary": "--ui-orange",
    "state-business-primary": "--ui-blue",
}


def map_tokens(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        alias = match.group(1)
        fallback = match.group(2) or ""
        ours = TOKENS.get(alias)
        if ours is None:
            return f"var(--ig-{alias}{fallback})"
        return f"var({ours}{fallback})"

    # Handles both var(--dsw-alias-x) and var(--dsw-alias-x, fallback).
    # A token can sit inside another token's fallback
    # (var(--dsw-alias-a, var(--dsw-alias-b))), so the pass repeats until the
    # alias namespace is gone — one pass leaves the nested one behind.
    for _ in range(8):
        if "--dsw-alias-" not in text:
            break
        text = re.sub(r"var\(--dsw-alias-([a-z0-9-]+)(\s*,[^;]*?)?\)", repl, text)
    return text


def scope_selector(selector: str, root: str, self_match: bool) -> str:
    part = selector.strip()
    if not part:
        return part
    if part.startswith(":root") or part.startswith("html") or part.startswith("body"):
        return root
    if part.startswith(root):
        return part
    scoped = f"{root} {part}"
    if self_match:
        scoped = f"{root}{part}, {scoped}"
    # The plugin puts both classes on one element (`className="ig-root panel"`),
    # so the upstream root class also needs a self-matching variant.
    if part == ".panel":
        scoped = f"{root}{part}, {root} {part}"
    return scoped


def transform(css: str, root: str, self_match: bool) -> str:
    out: list[str] = []
    depth = 0
    media_stack: list[str] = []
    keyframes: set[str] = set()
    # Depth of the keyframes block currently open; its `from`/`to` children are
    # steps, not selectors, so they must not be scoped.
    keyframes_at: int | None = None
    buffer: list[str] = []

    i = 0
    length = len(css)
    while i < length:
        char = css[i]
        if char == "{":
            head = "".join(buffer).strip()
            buffer = []
            head = re.sub(r"/\*.*?\*/", "", head, flags=re.S).strip()
            if head.startswith("@keyframes") or head.startswith("@-webkit-keyframes"):
                name = head.split()[-1].strip()
                keyframes.add(name)
                out.append(f"{head.replace(name, 'ig-' + name)}{{")
                depth += 1
                keyframes_at = depth
                media_stack = []
                i += 1
                continue
            if head.startswith("@"):
                out.append(f"{head}{{")
                depth += 1
                media_stack.append(head)
                i += 1
                continue
            if keyframes_at is not None and depth >= keyframes_at:
                # A keyframe step (`from`, `to`, `48%`) — copy it verbatim.
                out.append(f"{head}{{\n")
                depth += 1
                i += 1
                continue
            scoped = ", ".join(
                scope_selector(part, root, self_match) for part in head.split(",")
            )
            out.append(f"{scoped}{{\n")
            depth += 1
            media_stack = []
            i += 1
            continue
        if char == "}":
            # flush a declaration-only tail
            tail = "".join(buffer).strip()
            buffer = []
            if tail:
                out.append(tail + "\n")
            out.append("}\n")
            depth = max(0, depth - 1)
            if keyframes_at is not None and depth < keyframes_at:
                keyframes_at = None
            if depth == 0:
                media_stack = []
            i += 1
            continue
        buffer.append(char)
        i += 1

    text = "".join(out)
    if keyframes:
        names = "|".join(re.escape(name) for name in sorted(keyframes, key=len, reverse=True))
        text = re.sub(
            r"(animation(?:-name)?\s*:\s*)([^;{}]+)",
            lambda m: m.group(1)
            + re.sub(rf"(?<![\w-])({names})(?![\w-])", lambda k: "ig-" + k.group(1), m.group(2)),
            text,
        )
    return text


def render(sheet: Sheet) -> int:
    source = (DESIGN / sheet.source).read_text(encoding="utf-8")
    body = map_tokens(transform(source, sheet.root, sheet.self_match))
    header = (
        f"/* GENERATED by scripts/build_panel_css.py from the upstream DSH\n"
        f" * stylesheet {sheet.source} (dickpy/dsh-imagegen, Apache-2.0).\n"
        f" * Do not edit by hand: re-run the script after changing the upstream\n"
        f" * file. Every rule is scoped under {sheet.root} and themed with Hermes\n"
        f" * app tokens. */\n\n"
    )
    out = OUT_DIR / sheet.out
    out.write_text(header + body, encoding="utf-8")
    return len(body.splitlines())


def main() -> None:
    for sheet in SHEETS:
        lines = render(sheet)
        print(f"wrote desktop/{sheet.out} ({lines} lines, root {sheet.root})")


if __name__ == "__main__":
    main()
