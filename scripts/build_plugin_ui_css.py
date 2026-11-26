#!/usr/bin/env python3
"""Generate the plugin's own utility stylesheet from the classes it actually uses.

Why this exists: a desktop plugin's class strings are never seen by the app's
Tailwind build (it only scans the app's own sources), so `flex items-center
gap-2` written in plugin.js compiles to nothing and the view renders as raw
unstyled HTML. The plugin therefore has to ship its own CSS.

This script scans ``desktop/plugin.js`` for ``className: '...'`` tokens, renders
CSS for the utility vocabulary it finds (Tailwind-compatible naming, same
conversions: spacing unit = 4px, ``h-2`` = 8px, ``text-[0.6875rem]`` =
10.5px…), and writes ``desktop/plugin-ui.css``. Tokens already covered by the
generated upstream sheet (``desktop/panel.css``) are skipped.

Anything it cannot render is reported and fails the run — a class that no
stylesheet defines is a dead string, which is exactly the bug this guards.

Run: ``python scripts/build_plugin_ui_css.py``
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "desktop" / "plugin.js"
UPSTREAM_CSS = ROOT / "desktop" / "panel.css"
# Hand-written sheet that owns the DSH studio surface; its class names count as
# defined so the coverage gate does not report them as dead strings.
HANDWRITTEN_CSS = ROOT / "desktop" / "plugin-dsh.css"
OUT = ROOT / "desktop" / "plugin-ui.css"

SPACE = 4  # Tailwind spacing scale unit, in px

# Classes the upstream sheet already styles; they are not ours to redefine.
UPSTREAM = {
    "panel", "panelHeader", "panelHeading", "panelTitle", "panelHeaderActions",
    "studio", "generation", "config", "configScroll", "configHeader", "configToggle",
    "card", "cardHead", "cardHint", "modeRow", "modePill", "promptBox", "taskRow",
    "taskRows", "galleryMasonry", "galleryCard", "gallerySelect", "galleryImageButton",
    "galleryImage", "galleryBadge", "galleryCardActions", "galleryCardAction",
    "galleryCardFooter", "galleryAvatar", "galleryCardInfo", "galleryTags",
    "galleryTagEdit", "galleryTagEditor", "galleryRemove", "generationDuration",
    "connectionStatus", "connectionDot",
}


def px(value: str, unit: int = SPACE) -> str:
    return f"{float(value) * unit:g}px"


def render(token: str) -> str | None:
    """Return the CSS declaration block for one utility token, or None."""
    # --- variants: hover:* and group-hover:*
    for prefix in ("group-hover:", "hover:"):
        if token.startswith(prefix):
            base = token[len(prefix):]
            body = render(base)
            if body is None:
                return None
            marker = f"@{prefix}"
            return f"{marker}{body}"

    # --- arbitrary values: prop-[value]
    m = re.fullmatch(r"([a-z-]+)-\[(.+)\]", token)
    if m:
        prop, value = m.group(1), m.group(2)
        value = value.replace("_", " ")
        # rem is unreliable here: the app sets its own root font-size, so convert
        # to px at generation time instead of trusting 1rem == 16px at runtime.
        if prop == "text" and value.endswith("rem"):
            value = f"{float(value[:-3]) * 16:g}px"
        table = {
            "h": "height", "w": "width", "max-w": "max-width", "min-w": "min-width",
            "max-h": "max-height", "min-h": "min-height", "rounded": "border-radius",
            "text": "font-size", "aspect": "aspect-ratio", "grid-cols": "grid-template-columns",
            "gap": "gap", "p": "padding", "px": "padding-inline", "py": "padding-block",
        }
        if prop in table:
            return f"{table[prop]}: {value};"
        return None

    # --- theme colours: bg-(--ui-x), bg-(--ui-x)/70, text-(--ui-x), border-(--ui-x)
    m = re.fullmatch(r"(bg|text|border|ring)-\((--[a-z0-9-]+)\)(?:/(\d+))?", token)
    if m:
        kind, var, alpha = m.groups()
        colour = f"var({var})"
        if alpha:
            colour = f"color-mix(in srgb, {colour} {alpha}%, transparent)"
        prop = {"bg": "background-color", "text": "color",
                "border": "border-color", "ring": "--tw-ring-color"}[kind]
        return f"{prop}: {colour};"

    m = re.fullmatch(r"bg-(black|white|transparent)/(\d+)", token)
    if m:
        colour, alpha = m.groups()
        if colour == "transparent":
            return "background-color: transparent;"
        return f"background-color: rgb(0 0 0 / {int(alpha) / 100:g});"

    if token == "bg-transparent":
        return "background-color: transparent;"

    # --- spacing / sizing scales
    m = re.fullmatch(r"(p|px|py|pt|pb|pl|pr|m|mx|my|mt|mb|ml|mr|gap|gap-x|gap-y)-(\d+(?:\.\d+)?)", token)
    if m:
        prop_key, scale = m.groups()
        value = px(scale)
        table = {
            "p": "padding", "px": "padding-inline", "py": "padding-block",
            "pt": "padding-top", "pb": "padding-bottom", "pl": "padding-left",
            "pr": "padding-right", "m": "margin", "mx": "margin-inline",
            "my": "margin-block", "mt": "margin-top", "mb": "margin-bottom",
            "ml": "margin-left", "mr": "margin-right", "gap": "gap",
            "gap-x": "column-gap", "gap-y": "row-gap",
        }
        return f"{table[prop_key]}: {value};"

    m = re.fullmatch(r"(w|h|size)-(\d+(?:\.\d+)?)", token)
    if m:
        prop_key, scale = m.groups()
        value = px(scale)
        if prop_key == "size":
            return f"width: {value}; height: {value};"
        return f"{'width' if prop_key == 'w' else 'height'}: {value};"

    # --- borders / radius
    if token == "border":
        return "border: 1px solid var(--ui-stroke-secondary);"
    m = re.fullmatch(r"border-([brtl])(?:-(\d+))?", token)
    if m:
        side = {"b": "border-bottom", "r": "border-right", "t": "border-top", "l": "border-left"}[m.group(1)]
        width = f"{int(m.group(2))}px" if m.group(2) else "1px"
        return f"{side}: {width} solid var(--ui-stroke-secondary);"
    if token == "border-dashed":
        return "border-style: dashed;"
    if token == "border-none":
        return "border: none;"
    m = re.fullmatch(r"rounded(?:-(none|sm|md|lg|xl|full))?", token)
    if m:
        value = {
            None: "6px", "none": "0", "sm": "4px", "md": "6px", "lg": "8px",
            "xl": "12px", "full": "9999px",
        }[m.group(1)]
        return f"border-radius: {value};"

    # --- typography
    m = re.fullmatch(r"text-(\d+(?:\.\d+)?)rem", token)
    if m:
        return f"font-size: {float(m.group(1)) * 16:g}px;"
    m = re.fullmatch(r"text-\[(\d+(?:\.\d+)?)(rem|px)\]", token)
    if m:
        value = float(m.group(1)) * 16 if m.group(2) == "rem" else float(m.group(1))
        return f"font-size: {value:g}px;"
    if token == "font-medium":
        return "font-weight: 500;"
    if token == "font-semibold":
        return "font-weight: 600;"
    if token == "font-mono":
        return "font-family: ui-monospace, SFMono-Regular, Menlo, monospace;"
    if token == "truncate":
        return "overflow: hidden; text-overflow: ellipsis; white-space: nowrap;"
    if token == "break-words":
        return "overflow-wrap: anywhere;"
    if token == "whitespace-nowrap":
        return "white-space: nowrap;"
    if token == "whitespace-pre-wrap":
        return "white-space: pre-wrap;"
    if token == "line-clamp-2":
        return ("display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;"
                " overflow: hidden;")

    # --- layout
    simple = {
        "flex": "display: flex;", "inline-flex": "display: inline-flex;",
        "grid": "display: grid;", "block": "display: block;",
        "inline-block": "display: inline-block;", "hidden": "display: none;",
        "relative": "position: relative;", "absolute": "position: absolute;",
        "fixed": "position: fixed;", "sticky": "position: sticky;",
        "inset-0": "inset: 0;",
        "flex-col": "flex-direction: column;", "flex-row": "flex-direction: row;",
        "flex-wrap": "flex-wrap: wrap;", "flex-1": "flex: 1 1 0%;",
        "flex-none": "flex: none;", "shrink-0": "flex-shrink: 0;",
        "grow": "flex-grow: 1;", "min-h-0": "min-height: 0;",
        "min-w-0": "min-width: 0;", "w-full": "width: 100%;",
        "h-full": "height: 100%;", "h-auto": "height: auto;",
        "items-center": "align-items: center;", "items-start": "align-items: flex-start;",
        "items-stretch": "align-items: stretch;", "items-end": "align-items: flex-end;",
        "justify-center": "justify-content: center;",
        "justify-between": "justify-content: space-between;",
        "justify-end": "justify-content: flex-end;",
        "justify-start": "justify-content: flex-start;",
        "overflow-hidden": "overflow: hidden;", "overflow-auto": "overflow: auto;",
        "overflow-y-auto": "overflow-y: auto;", "overflow-x-auto": "overflow-x: auto;",
        "overflow-visible": "overflow: visible;",
        "object-contain": "object-fit: contain;", "object-cover": "object-fit: cover;",
        "cursor-pointer": "cursor: pointer;", "cursor-default": "cursor: default;",
        "cursor-grab": "cursor: grab;", "cursor-move": "cursor: move;",
        "cursor-crosshair": "cursor: crosshair;", "cursor-zoom-in": "cursor: zoom-in;",
        "select-none": "user-select: none;", "pointer-events-none": "pointer-events: none;",
        "text-center": "text-align: center;", "text-left": "text-align: left;",
        "opacity-50": "opacity: 0.5;", "opacity-0": "opacity: 0;",
        "shadow-sm": "box-shadow: 0 1px 2px rgb(0 0 0 / 0.25);",
        "aspect-square": "aspect-ratio: 1 / 1;",
        "col-span-full": "grid-column: 1 / -1;",
        "animate-pulse": "animation: ig-util-pulse 1.6s ease-in-out infinite;",
        "leading-none": "line-height: 1;", "leading-tight": "line-height: 1.25;",
        "leading-snug": "line-height: 1.375;", "tabular-nums": "font-variant-numeric: tabular-nums;",
        "text-xs": "font-size: 12px;", "text-sm": "font-size: 14px;",
        "text-white": "color: #fff;",
        "text-foreground": "color: var(--ui-text-primary);",
        "max-h-full": "max-height: 100%;", "max-w-full": "max-width: 100%;",
        "mt-auto": "margin-top: auto;", "mx-auto": "margin-inline: auto;",
        "opacity-60": "opacity: 0.6;",
        "place-items-center": "place-items: center;",
        "resize-none": "resize: none;",
        "top-0": "top: 0;", "right-0": "right: 0;",
        "w-auto": "width: auto;",
        "line-clamp-1": ("display: -webkit-box; -webkit-line-clamp: 1; -webkit-box-orient: vertical;"
                        " overflow: hidden;"),
        "group": "--ig-group: 1;",
        "underline": "text-decoration: underline;",
    }
    if token in simple:
        return simple[token]

    m = re.fullmatch(r"z-(\d+)", token)
    if m:
        return f"z-index: {m.group(1)};"
    if token == "z-50":
        return "z-index: 50;"

    # --- grid columns with a repeat() expression
    m = re.fullmatch(r"grid-cols-\[(.+)\]", token)
    if m:
        return f"grid-template-columns: {m.group(1).replace('_', ' ')};"

    m = re.fullmatch(r"aspect-\[(.+)\]", token)
    if m:
        return f"aspect-ratio: {m.group(1).replace('_', ' ').replace('/', ' / ')};"

    return None


def used_tokens(source: str) -> list[str]:
    tokens: set[str] = set()
    for match in re.finditer(r"className:\s*'([^']*)'", source):
        tokens.update(match.group(1).split())
    for match in re.finditer(r'className:\s*"([^"]*)"', source):
        tokens.update(match.group(1).split())
    return sorted(tokens)


def escape_class(token: str) -> str:
    """CSS-selector escape for utility names carrying : . ( ) [ ] / etc."""
    return re.sub(r"([:./()\[\]%#])", r"\\\1", token)


def main() -> int:
    source = PLUGIN.read_text(encoding="utf-8")
    tokens = used_tokens(source)
    upstream_css = UPSTREAM_CSS.read_text(encoding="utf-8") if UPSTREAM_CSS.exists() else ""
    if HANDWRITTEN_CSS.exists():
        upstream_css += HANDWRITTEN_CSS.read_text(encoding="utf-8")

    # Names the ported result-board/menu/loading sheets provide. They only mean
    # something under their own roots (.ig-results/.ig-menu/.ig-loading), so they
    # are never rendered into the .ig-root utility sheet — but they must not be
    # reported as dead strings either.
    surface_defined: set[str] = set()
    for name in ("panel-results.css", "panel-menu.css", "panel-loading.css"):
        path = ROOT / "desktop" / name
        if path.exists():
            surface_defined.update(re.findall(r"\.([A-Za-z_][\w-]*)", path.read_text(encoding="utf-8")))

    rules: list[str] = []
    unknown: list[str] = []
    skipped: list[str] = []
    for token in tokens:
        # A class carrying an upstream name is styled by panel.css already.
        if any(part in UPSTREAM for part in re.split(r"\s+", token)):
            skipped.append(token)
            continue
        body = render(token)
        if body is None:
            if f".{token}" in upstream_css or token in surface_defined:
                skipped.append(token)
            else:
                unknown.append(token)
            continue
        if body.startswith("@group-hover"):
            decls = body[len("@group-hover"):]
            selector = f".ig-root .group:hover .{escape_class(token)}"
        elif body.startswith("@hover"):
            decls = body[len("@hover"):]
            selector = f".ig-root .{escape_class(token)}:hover"
        else:
            decls = body
            selector = f".ig-root .{escape_class(token)}"
        rules.append(f"{selector} {{\n  {decls}\n}}\n")

    header = (
        "/* GENERATED by scripts/build_plugin_ui_css.py — do not edit by hand.\n"
        " * The utility vocabulary desktop/plugin.js actually uses, rendered to real\n"
        " * CSS because a plugin's class strings never reach the app's Tailwind build.\n"
        " */\n\n"
        "@keyframes ig-util-pulse {\n  50% { opacity: 0.55; }\n}\n\n"
    )
    OUT.write_text(header + "\n".join(rules), encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(rules)} rules, {len(skipped)} covered by panel.css)")

    if unknown:
        print("classes with no stylesheet definition (dead strings):", file=sys.stderr)
        for token in unknown:
            print(f"  {token}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
