"""Filesystem locations for the plugin's own data.

Data is per profile because ``get_hermes_home()`` resolves to the profile home
(``~/.hermes/profiles/<name>``) under a named profile. ``HERMES_IMAGEGEN_DATA``
overrides the root — the smoke test and CI point it at a temp dir so they never
touch a real library.
"""

from __future__ import annotations

import os
from pathlib import Path


def hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home  # type: ignore[import-not-found]

        return Path(get_hermes_home())
    except Exception:  # pragma: no cover - only when imported outside Hermes
        env = os.environ.get("HERMES_HOME")
        return Path(env) if env else Path.home() / ".hermes"


def data_root() -> Path:
    override = os.environ.get("HERMES_IMAGEGEN_DATA", "").strip()
    root = Path(override) if override else hermes_home() / "imagegen"
    root.mkdir(parents=True, exist_ok=True)
    return root


def settings_file() -> Path:
    return data_root() / "settings.json"


def history_db() -> Path:
    return data_root() / "history.sqlite3"


def files_root() -> Path:
    root = data_root() / "files"
    root.mkdir(parents=True, exist_ok=True)
    return root
