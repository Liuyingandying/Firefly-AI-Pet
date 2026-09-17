"""Persistent Suggestion Store (M3B.7) — durable lifecycle for memory
candidates.

Source of truth: ``%LOCALAPPDATA%/FireflyAI/suggestions/memory_suggestions.json``

    {
      "version": 1,
      "suggestions": [
        {
          "id": "<uuid hex>",
          "content": "候选记忆内容",
          "source": "companion_auto | conversation_summary | user_created",
          "confidence": 0.0-1.0,
          "created_at": <epoch ms>,
          "status": "pending | accepted | rejected | expired",
          "metadata": {"category": "...", "reason": "...", "evidence": [...]}
        }
      ]
    }

Boundary (unchanged): suggestions are CANDIDATES.  Nothing in this store
ever writes a MemoryRecord — the only route into long-term memory is the
user confirming a pending suggestion (MemoryService.remember with
trigger="suggestion_confirmed").

Durability rules:
- atomic write (tmp + replace)
- corrupt JSON is quarantined (renamed) and the store restarts empty —
  chat startup is never blocked
- source "explicit" is rejected at add() time (fabricating a user-explicit
  origin for an automatic candidate is forbidden)
"""

from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .records import MemoryCategory
from core.user_paths import get_user_data_paths

STORE_VERSION = 1

ALLOWED_SOURCES = frozenset(
    {"companion_auto", "conversation_summary", "user_created"}
)
ALLOWED_STATUSES = frozenset(
    {"pending", "accepted", "rejected", "expired"}
)

DEFAULT_SUGGESTION_STORE_PATH = (
    get_user_data_paths().suggestions / "memory_suggestions.json"
)


class SuggestionValidationError(ValueError):
    """Raised when a suggestion payload fails validation."""


class SuggestionStore:
    """Thread-safe, Qt-free persistent store for memory suggestions."""

    def __init__(self, path: Path | str | None = None) -> None:
        import os

        self.path = Path(
            path
            if path is not None
            else os.environ.get(
                "FIREFLY_SUGGESTION_STORE_PATH", str(DEFAULT_SUGGESTION_STORE_PATH)
            )
        )
        self._lock = threading.RLock()
        self._records: dict[str, dict[str, Any]] = {}
        self._load()

    # ---------------------------------------------------------------- load

    def _load(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            self._records = {}
            return
        if not self.path.exists():
            self._records = {}
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            quarantine = self.path.with_name(
                f"memory_suggestions.corrupt-{int(time.time())}.json"
            )
            try:
                self.path.rename(quarantine)
            except OSError:
                pass
            self._records = {}
            return
        records: dict[str, dict[str, Any]] = {}
        entries = raw.get("suggestions") if isinstance(raw, dict) else None
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                record_id = entry.get("id")
                if not isinstance(record_id, str) or not record_id:
                    continue
                if record_id in records:
                    continue
                records[record_id] = {
                    "id": record_id,
                    "content": str(entry.get("content", "")),
                    "source": str(entry.get("source", "")),
                    "confidence": _as_float(entry.get("confidence"), 0.5),
                    "created_at": _as_int(entry.get("created_at"), 0),
                    "status": str(entry.get("status", "pending")),
                    "metadata": dict(entry.get("metadata") or {}),
                }
        self._records = records

    def _persist(self) -> None:
        payload = {
            "version": STORE_VERSION,
            "suggestions": list(self._records.values()),
        }
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)

    # ---------------------------------------------------------------- read

    def list_all(self) -> list[dict[str, Any]]:
        """All suggestions, newest created_at first."""
        with self._lock:
            return sorted(
                (dict(r) for r in self._records.values()),
                key=lambda r: r["created_at"],
                reverse=True,
            )

    def get_pending(self) -> list[dict[str, Any]]:
        return [r for r in self.list_all() if r["status"] == "pending"]

    def get(self, suggestion_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._records.get(suggestion_id)
            return dict(record) if record is not None else None

    def count(self) -> int:
        with self._lock:
            return len(self._records)

    # ---------------------------------------------------------------- write

    def add(
        self,
        *,
        content: str,
        source: str,
        confidence: float = 0.5,
        status: str = "pending",
        metadata: dict[str, Any] | None = None,
        created_at: int | None = None,
        suggestion_id: str | None = None,
    ) -> dict[str, Any]:
        text = str(content or "").strip()
        if not text:
            raise SuggestionValidationError("suggestion content is empty")
        if source not in ALLOWED_SOURCES:
            # "explicit" is deliberately NOT a store source: an automatic
            # candidate must never fabricate a user-explicit origin.
            raise SuggestionValidationError(
                f"source {source!r} is not allowed "
                f"(allowed: {sorted(ALLOWED_SOURCES)})"
            )
        if status not in ALLOWED_STATUSES:
            raise SuggestionValidationError(f"unknown status {status!r}")
        confidence_f = _as_float(confidence, 0.5)
        confidence_f = max(0.0, min(1.0, confidence_f))
        record = {
            "id": suggestion_id or uuid.uuid4().hex,
            "content": text,
            "source": source,
            "confidence": confidence_f,
            "created_at": _as_int(created_at, time.time_ns() // 1_000_000),
            "status": status,
            "metadata": dict(metadata or {}),
        }
        with self._lock:
            if record["id"] in self._records:
                raise SuggestionValidationError(
                    f"duplicate suggestion id: {record['id']}"
                )
            self._records[record["id"]] = record
            self._persist()
        return dict(record)

    def update_status(self, suggestion_id: str, status: str) -> dict[str, Any] | None:
        if status not in ALLOWED_STATUSES:
            raise SuggestionValidationError(f"unknown status {status!r}")
        with self._lock:
            record = self._records.get(suggestion_id)
            if record is None:
                return None
            record["status"] = status
            self._persist()
            return dict(record)

    def delete(self, suggestion_id: str) -> bool:
        with self._lock:
            if suggestion_id not in self._records:
                return False
            del self._records[suggestion_id]
            self._persist()
            return True

    def clear(self, *, status: str | None = None) -> int:
        """Remove all suggestions (or all with one status). Returns count."""
        with self._lock:
            if status is None:
                removed = len(self._records)
                self._records = {}
            else:
                doomed = [
                    rid
                    for rid, r in self._records.items()
                    if r["status"] == status
                ]
                removed = len(doomed)
                for rid in doomed:
                    del self._records[rid]
            if removed:
                self._persist()
            return removed


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def category_to_metadata(category: MemoryCategory | str | None) -> str:
    """Normalise a category to its string value for metadata storage."""
    value = getattr(category, "value", category)
    return str(value) if value is not None else ""


__all__ = [
    "ALLOWED_SOURCES",
    "ALLOWED_STATUSES",
    "DEFAULT_SUGGESTION_STORE_PATH",
    "SuggestionStore",
    "SuggestionValidationError",
    "category_to_metadata",
]
