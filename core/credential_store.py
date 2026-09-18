"""Per-machine credential storage for provider API keys.

Provider Manager Phase 1 (docs/Provider_Manager_Architecture_Report.md).

Storage lives under the canonical Firefly user data root (``core.user_paths``)
at ``<root>/credentials/credentials.json``.  The root respects
``FIREFLY_USER_DATA_DIR``, so tests and the packaged app can isolate storage;
by construction the file is outside the repository tree and can never be
tracked by Git.  This module never touches ``.env``, ``runtime/`` or
``config/``.

Contract:
- stdlib only (no Qt, no third-party dependencies)
- values are secrets: :meth:`CredentialStore.list_sources` returns key names
  only, never values; bulk value export is deliberately not provided
- writes are atomic replacements (unique tmp file + ``os.replace`` with a
  bounded WinError 5 retry), so a crash can never leave a half-written store
- a corrupt store file is quarantined (renamed aside) and treated as empty
  instead of crashing callers; unreadable files raise so a save can never
  silently wipe existing credentials
- file permissions are tightened best-effort (``0o600``); on Windows this
  only clears the read-only bit — DPAPI value encryption is a planned
  follow-up (rc3) and does not change this contract
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from core.user_paths import get_user_data_paths

_STORE_VERSION = 1
_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_REPLACE_RETRY_DELAYS = (0.05, 0.1, 0.2)


def default_store_path() -> Path:
    """Return the canonical store path under the Firefly user data root."""
    return get_user_data_paths().credentials / "credentials.json"


def _validate_key(provider_key: str) -> str:
    key = (provider_key or "").strip()
    if not _KEY_PATTERN.match(key):
        raise ValueError(
            "credential key must be an UPPER_SNAKE environment-style name"
        )
    return key


def _validate_value(value: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise ValueError("credential value must be non-empty")
    if any(ch in cleaned for ch in ("\n", "\r")):
        raise ValueError("credential value must not contain line breaks")
    return cleaned


class CredentialStore:
    """JSON-backed key/value store for provider credentials.

    The class is process-safe via an internal lock and crash-safe via atomic
    replacement; concurrent Firefly processes serialize through the OS
    ``os.replace`` semantics.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else default_store_path()
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def save(self, provider_key: str, value: str) -> None:
        """Insert or update one credential (atomic; creates the store)."""
        key = _validate_key(provider_key)
        cleaned = _validate_value(value)
        with self._lock:
            data = self._read()
            credentials = data["credentials"]
            credentials[key] = {
                "value": cleaned,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
            self._write(data)

    def get(self, provider_key: str) -> str | None:
        """Return one credential value, or ``None`` when not stored."""
        key = _validate_key(provider_key)
        with self._lock:
            entry = self._read()["credentials"].get(key)
        if not isinstance(entry, dict):
            return None
        value = entry.get("value")
        return value if isinstance(value, str) and value.strip() else None

    def delete(self, provider_key: str) -> None:
        """Remove one credential; deleting an absent key is a no-op."""
        key = _validate_key(provider_key)
        with self._lock:
            data = self._read()
            credentials = data["credentials"]
            if key not in credentials:
                return
            del credentials[key]
            self._write(data)

    def list_sources(self) -> list[str]:
        """Return the stored credential key names (never their values)."""
        with self._lock:
            names = sorted(self._read()["credentials"])
        return names

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------
    def _read(self) -> dict[str, Any]:
        """Load the store, quarantining corrupt files instead of crashing."""
        fresh: dict[str, Any] = {"version": _STORE_VERSION, "credentials": {}}
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return fresh
        except OSError:
            # Unreadable (locked/ACL): fail loudly so save() can never wipe
            # existing credentials on a transient read problem.
            raise
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            self._quarantine()
            return fresh
        if not isinstance(data, dict) or not isinstance(
            data.get("credentials"), dict
        ):
            self._quarantine()
            return fresh
        return data

    def _quarantine(self) -> None:
        try:
            self.path.rename(
                self.path.with_name(
                    f"credentials.corrupt-{time.strftime('%Y%m%d_%H%M%S')}"
                    f"-{uuid.uuid4().hex[:6]}.json"
                )
            )
        except OSError:
            pass

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        tmp_path = self.path.with_name(
            f"credentials.{uuid.uuid4().hex}.tmp"
        )
        try:
            with open(tmp_path, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            for delay in _REPLACE_RETRY_DELAYS:
                try:
                    os.replace(tmp_path, self.path)
                    break
                except PermissionError:
                    # WinError 5: another process holds the destination briefly.
                    time.sleep(delay)
            else:
                os.replace(tmp_path, self.path)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass


def default_store() -> CredentialStore:
    """Return the process default store bound to the user data root."""
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        _DEFAULT_STORE = CredentialStore()
    return _DEFAULT_STORE


_DEFAULT_STORE: CredentialStore | None = None
