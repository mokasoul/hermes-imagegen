"""Process-wide singletons shared by the desktop API routes and the agent tools.

The gateway loads ``dashboard/plugin_api.py`` for the REST routes and the package
``__init__.py`` for the agent tools — two import paths into the same process. They
MUST share one ``Settings`` cache, one history store and one job registry, or a
job started from chat would be invisible to the workbench (and the settings file
would be cached twice with different contents).

Locking rule: never call another ``*()`` accessor while holding ``_LOCK`` — it is
a plain (non-reentrant) lock and that deadlocks the request that took it.
"""

from __future__ import annotations

import threading

from .jobs import JobRunner
from .settings import Settings
from .store import HistoryStore

_LOCK = threading.Lock()
_settings: Settings | None = None
_store: HistoryStore | None = None
_runner: JobRunner | None = None


def settings() -> Settings:
    global _settings
    with _LOCK:
        if _settings is None:
            _settings = Settings()
        return _settings


def store() -> HistoryStore:
    global _store
    with _LOCK:
        if _store is None:
            _store = HistoryStore()
        return _store


def runner() -> JobRunner:
    global _runner
    if _runner is not None:
        return _runner
    settings_store = settings()
    history_store = store()
    with _LOCK:
        if _runner is None:
            _runner = JobRunner(settings_store, history_store)
        return _runner


def reset_for_tests() -> None:
    """Drop every cached singleton (tests move HERMES_IMAGEGEN_DATA between cases)."""
    global _settings, _store, _runner
    with _LOCK:
        _settings = None
        _store = None
        _runner = None
