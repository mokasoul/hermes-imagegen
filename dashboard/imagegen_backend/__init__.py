"""Backend for the hermes-imagegen desktop plugin.

Everything here runs inside the Hermes gateway process (mounted by
``dashboard/plugin_api.py`` at ``/api/plugins/hermes-imagegen/``). The layout is
deliberately flat and dependency-free apart from ``httpx`` (already a gateway
dependency):

- ``paths``    — where settings/history/images live (per profile)
- ``models``   — model-family catalog + upstream request shaping
- ``settings`` — channel store, key masking, credential resolution
- ``engine``   — the OpenAI-compatible upstream calls
- ``store``    — SQLite history and on-disk image files
- ``jobs``     — the in-process generation queue

This package is a Python port of the DSH plugin ``dickpy/dsh-imagegen``
(Apache-2.0); see NOTICE for what was ported and what was dropped.
"""

from __future__ import annotations

__all__ = ["paths", "models", "settings", "engine", "store", "jobs"]
