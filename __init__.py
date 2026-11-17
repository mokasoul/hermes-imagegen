"""hermes-imagegen — agent half: image generation tools for chat.

Two halves, one package:

- ``dashboard/plugin_api.py`` serves the desktop workbench's REST namespace
  (``/api/plugins/hermes-imagegen/``);
- this module registers the chat tools, which submit the *same* jobs through
  ``imagegen_backend.runtime.runner()`` so a picture made in chat lands in the
  workbench history, and a picture made in the workbench can be referenced by
  ``edit_image`` without re-uploading.

The tools read the same channel store the UI writes; nothing here touches
config.yaml directly.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)

# `dashboard/` is this package's own subdirectory and is not on sys.path when the
# plugin loader imports this file — the backend package lives inside it.
_DASHBOARD = Path(__file__).resolve().parent / "dashboard"
if str(_DASHBOARD) not in sys.path:
    sys.path.insert(0, str(_DASHBOARD))

TOOLSET = "imagegen"

_DESCRIPTIONS = {
    "generate_image": "Generate an image through a configured channel (text-to-image).",
    "edit_image": "Edit an existing image (image-to-image) from an image_id, path, URL or data URL.",
    "get_image_generation_task": "Check an image-generation task started in background mode.",
    "cancel_image_generation_task": "Cancel a queued or running image-generation task.",
}

_EMOJI = {
    "generate_image": "🎨",
    "edit_image": "🖌️",
    "get_image_generation_task": "🔎",
    "cancel_image_generation_task": "⛔",
}


def register(ctx) -> None:  # noqa: ANN001 - plugin context from the loader
    """Register the four image tools on the agent."""
    from imagegen_backend import agent_tools

    for schema in agent_tools.SCHEMAS:
        name = schema["name"]
        ctx.register_tool(
            name=name,
            toolset=TOOLSET,
            schema=schema,
            handler=agent_tools.HANDLERS[name],
            is_async=True,
            description=_DESCRIPTIONS[name],
            emoji=_EMOJI[name],
        )
    log.info("hermes-imagegen: registered %d image tools", len(agent_tools.SCHEMAS))
