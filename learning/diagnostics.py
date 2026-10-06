"""Minimal entry-route diagnostics for the Learning Bridge (Phase 3).

Four stable markers, one line each, no secrets:

    [LEARNING_ENTRY]  source=ability_panel|tray|other
    [LEARNING_ROUTE]  route=legacy_core_learning|learning_bridge
    [LEARNING_BRIDGE] course_id=... action=... context_path=...
    [ZCODE_LAUNCH]    command=... pid=...

Lines go to the standard logger (``firefly.learning_entry``) so tests can
capture them via caplog, and are appended to
``<FireflyData>/logs/learning_entry.log`` so a real GUI click is observable
after the fact. Only ids/paths/argv are logged — never API keys.
"""

from __future__ import annotations

import logging
from threading import Lock

LOGGER_NAME = "firefly.learning_entry"
_MARKER_LOGGER = logging.getLogger(LOGGER_NAME)
_FILE_ATTACHED = False
_FILE_LOCK = Lock()


def _log_file_path():
    try:
        from core.user_paths import get_user_data_paths

        return get_user_data_paths().logs / "learning_entry.log"
    except Exception:  # noqa: BLE001 — diagnostics must never raise
        return None


def _ensure_file_handler() -> None:
    global _FILE_ATTACHED
    with _FILE_LOCK:
        if _FILE_ATTACHED:
            return
        path = _log_file_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(path, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            _MARKER_LOGGER.addHandler(handler)
            _MARKER_LOGGER.setLevel(logging.INFO)
            _FILE_ATTACHED = True
        except OSError:
            pass


def log_marker(marker: str, **fields) -> None:
    """Emit one ``[MARKER] key=value ...`` line (file + logger)."""
    _ensure_file_handler()
    kv = " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
    _MARKER_LOGGER.info(f"[{marker}] {kv}".strip())
