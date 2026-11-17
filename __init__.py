"""Hermes agent half of hermes-imagegen.

The agent-side surface is intentionally empty: everything the plugin does is
served by the backend router in ``dashboard/plugin_api.py`` (mounted at
``/api/plugins/hermes-imagegen/``) and rendered by the desktop half in
``desktop/plugin.js``. This file exists so Hermes plugin discovery
(``_iter_plugin_dirs`` walks child dirs that hold ``__init__.py``) can see the
package at all — ``hermes plugins install/update/list`` need it, a UI-only
package registers no tools.
"""

from __future__ import annotations


def register(ctx):  # noqa: ANN001 - plugin context, unused on purpose
    """No agent tools/hooks in this version; the router does the work."""
    return None
