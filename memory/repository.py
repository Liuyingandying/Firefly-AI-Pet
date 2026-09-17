"""Versioned local persistence for authoritative Firefly memory records.

The repository is a pure data boundary: it has no dependency on Mem0, Qt,
``core``, or ``ui``. JSON is the source of truth; vector storage is not.
"""

from __future__ import annotations

import json
import os
import time
from abc import ABC, abstractmethod
from dataclasses import replace
from pathlib import Path
from threading import RLock
from typing import Any, Mapping

from .access_mode import (
    MemoryAccessMode, SAFE_WRITE_PATCH_FIELDS, check_access,
)
from .records import MemoryCategory, MemoryRecord, MemorySource
from core.user_paths import get_user_data_paths


STORE_VERSION = 1
DEFAULT_REPOSITORY_PATH = get_user_data_paths().memory / "memory_records.json"


def _mode(access_mode: MemoryAccessMode | str | None) -> MemoryAccessMode:
    """Coerce a caller-supplied mode; ``None`` = legacy permissive handle."""
    if access_mode is None:
        return MemoryAccessMode.CONFIRMED_WRITE
    if isinstance(access_mode, MemoryAccessMode):
        return access_mode
    return MemoryAccessMode(str(access_mode))


class DuplicateMemoryRecordError(ValueError):
    """Raised when adding a record whose ID already exists."""


class MemoryRepository(ABC):
    """Storage-independent contract for authoritative memory records."""

    @abstractmethod
    def add(self, record: MemoryRecord, *,
            access_mode: MemoryAccessMode | str | None = None) -> str:
        """Persist ``record`` and return its ID; reject duplicate IDs.

        Requires at least SAFE_WRITE. ``access_mode=None`` keeps the legacy
        permissive behaviour for direct storage users; the service layer
        always forwards its handle's mode.
        """

    @abstractmethod
    def get(self, record_id: str) -> MemoryRecord | None:
        """Pure read: return the record or None. Never writes."""

    @abstractmethod
    def list(
        self,
        *,
        category: MemoryCategory | str | None = None,
        source: MemorySource | str | None = None,
        before_ts: int | None = None,
    ) -> list[MemoryRecord]:
        """Return filtered records ordered by newest creation time first."""

    @abstractmethod
    def update(self, record_id: str, patch: Mapping[str, Any], *,
               access_mode: MemoryAccessMode | str | None = None) -> MemoryRecord:
        """Apply known mutable fields and advance ``updated_ts``.

        Requires at least SAFE_WRITE; a SAFE_WRITE handle may only patch
        creation/supersede bookkeeping fields.
        """

    @abstractmethod
    def delete(self, record_id: str, *,
               access_mode: MemoryAccessMode | str | None = None) -> bool:
        """Delete a record, returning False when it does not exist.

        Requires CONFIRMED_WRITE.
        """

    @abstractmethod
    def clear(self, *,
              access_mode: MemoryAccessMode | str | None = None) -> int:
        """Delete every record and return the number removed.

        Requires CONFIRMED_WRITE.
        """

    @abstractmethod
    def all_ids(self) -> set[str]:
        """Return all authoritative record IDs."""

    @abstractmethod
    def export(self) -> dict[str, Any]:
        """Return a complete JSON-compatible snapshot."""

    @abstractmethod
    def import_data(self, data: Mapping[str, Any], *,
                    access_mode: MemoryAccessMode | str | None = None) -> int:
        """Validate and merge records, returning the number imported.

        Requires CONFIRMED_WRITE.
        """


class JsonMemoryRepository(MemoryRepository):
    """Thread-safe MemoryRepository backed by one atomic JSON file."""

    _MUTABLE_FIELDS = {
        "category",
        "content",
        "source",
        "trigger",
        "permission",
        "weight",
        "last_accessed_ts",
        "retention_half_life_days",
        "vector_id",
        "lifecycle_status",
        "superseded_by",
        "supersede_reason",
    }

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_REPOSITORY_PATH
        self._lock = RLock()
        self._records = self._load()

    def add(self, record: MemoryRecord, *,
            access_mode: MemoryAccessMode | str | None = None) -> str:
        if not isinstance(record, MemoryRecord):
            raise TypeError("record must be a MemoryRecord")
        check_access(_mode(access_mode), MemoryAccessMode.SAFE_WRITE,
                     "repository.add")
        with self._lock:
            if record.id in self._records:
                raise DuplicateMemoryRecordError(
                    f"memory record ID already exists: {record.id}"
                )
            proposed = dict(self._records)
            proposed[record.id] = record
            self._persist(proposed)
            self._records = proposed
            return record.id

    def get(self, record_id: str) -> MemoryRecord | None:
        """Pure read (M3B.1: no last_accessed_ts bump, no persist).

        Retrieval, UI viewing, and benchmarking resolve records through this
        method; a read must never write.  Recording an access — if ever
        needed — is an explicit ``update`` by a CONFIRMED_WRITE entry.
        """
        normalized_id = self._record_id(record_id)
        with self._lock:
            return self._records.get(normalized_id)

    def list(
        self,
        *,
        category: MemoryCategory | str | None = None,
        source: MemorySource | str | None = None,
        before_ts: int | None = None,
    ) -> list[MemoryRecord]:
        normalized_category = None if category is None else MemoryCategory(category)
        normalized_source = None if source is None else MemorySource(source)
        if before_ts is not None and (
            isinstance(before_ts, bool) or not isinstance(before_ts, int)
        ):
            raise ValueError("before_ts must be an integer timestamp")

        with self._lock:
            records = list(self._records.values())
        return sorted(
            (
                record
                for record in records
                if (normalized_category is None or record.category == normalized_category)
                and (normalized_source is None or record.source == normalized_source)
                and (before_ts is None or record.created_ts < before_ts)
            ),
            key=lambda record: (record.created_ts, record.id),
            reverse=True,
        )

    def update(self, record_id: str, patch: Mapping[str, Any], *,
               access_mode: MemoryAccessMode | str | None = None) -> MemoryRecord:
        normalized_id = self._record_id(record_id)
        if not isinstance(patch, Mapping):
            raise TypeError("patch must be a mapping")
        # SAFE_WRITE handles may only touch creation/supersede bookkeeping;
        # content edits, timestamps and weights need CONFIRMED_WRITE.
        check_access(
            _mode(access_mode), MemoryAccessMode.SAFE_WRITE,
            "repository.update",
            patch_keys=frozenset(patch),
            allowed_patch_fields=SAFE_WRITE_PATCH_FIELDS,
        )
        with self._lock:
            current = self._records.get(normalized_id)
            if current is None:
                raise KeyError(normalized_id)
            changes = {
                key: value for key, value in patch.items() if key in self._MUTABLE_FIELDS
            }
            changes["updated_ts"] = max(self._now_ms(), current.updated_ts + 1)
            updated = replace(current, **changes)
            proposed = dict(self._records)
            proposed[normalized_id] = updated
            self._persist(proposed)
            self._records = proposed
            return updated

    def delete(self, record_id: str, *,
               access_mode: MemoryAccessMode | str | None = None) -> bool:
        check_access(_mode(access_mode), MemoryAccessMode.CONFIRMED_WRITE,
                     "repository.delete")
        normalized_id = self._record_id(record_id)
        with self._lock:
            if normalized_id not in self._records:
                return False
            proposed = dict(self._records)
            del proposed[normalized_id]
            self._persist(proposed)
            self._records = proposed
            return True

    def clear(self, *,
              access_mode: MemoryAccessMode | str | None = None) -> int:
        check_access(_mode(access_mode), MemoryAccessMode.CONFIRMED_WRITE,
                     "repository.clear")
        with self._lock:
            count = len(self._records)
            if count == 0:
                return 0
            self._persist({})
            self._records = {}
            return count

    def all_ids(self) -> set[str]:
        with self._lock:
            return set(self._records)

    def export(self) -> dict[str, Any]:
        with self._lock:
            records = self._ordered(self._records)
            return {
                "version": STORE_VERSION,
                "records": [record.to_dict() for record in records],
            }

    def import_data(self, data: Mapping[str, Any], *,
                    access_mode: MemoryAccessMode | str | None = None) -> int:
        check_access(_mode(access_mode), MemoryAccessMode.CONFIRMED_WRITE,
                     "repository.import_data")
        candidates = self._parse_payload(data)
        if not candidates:
            return 0
        with self._lock:
            proposed = dict(self._records)
            imported = 0
            for record in candidates:
                if record.id in proposed:
                    continue
                proposed[record.id] = record
                imported += 1
            if imported:
                self._persist(proposed)
                self._records = proposed
            return imported

    # A normal ``repo.import(...)`` expression is invalid Python syntax.
    # These aliases make the operation explicit while allowing dynamic callers
    # to use ``getattr(repo, "import")(data)`` when mirroring the design table.
    import_records = import_data

    def _load(self) -> dict[str, MemoryRecord]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            return {}
        return {record.id: record for record in self._parse_payload(data)}

    @staticmethod
    def _parse_payload(data: Any) -> list[MemoryRecord]:
        if not isinstance(data, Mapping):
            return []
        version = data.get("version")
        if isinstance(version, bool) or not isinstance(version, int):
            return []
        if version > STORE_VERSION or version < 1:
            return []
        raw_records = data.get("records")
        if not isinstance(raw_records, list):
            return []

        records: list[MemoryRecord] = []
        seen: set[str] = set()
        for item in raw_records:
            try:
                record = MemoryRecord.from_dict(item)
            except (TypeError, ValueError):
                continue
            if record.id in seen:
                continue
            seen.add(record.id)
            records.append(record)
        return records

    def _persist(self, records: Mapping[str, MemoryRecord]) -> None:
        payload = {
            "version": STORE_VERSION,
            "records": [record.to_dict() for record in self._ordered(records)],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            self._atomic_replace(temporary, self.path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _atomic_replace(src: Path, dst: Path) -> None:
        """Atomic replace with Windows WinError 5 retry."""
        import time

        max_retries = 5
        for attempt in range(max_retries):
            try:
                os.replace(src, dst)
                return
            except PermissionError:
                if attempt < max_retries - 1:
                    time.sleep(0.1 * (attempt + 1))
                else:
                    raise

    @staticmethod
    def _ordered(records: Mapping[str, MemoryRecord]) -> list[MemoryRecord]:
        return sorted(
            records.values(),
            key=lambda record: (record.created_ts, record.id),
            reverse=True,
        )

    @staticmethod
    def _record_id(value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("record_id must be a non-empty string")
        return value.strip()

    @staticmethod
    def _now_ms() -> int:
        return time.time_ns() // 1_000_000


setattr(MemoryRepository, "import", MemoryRepository.import_data)
setattr(JsonMemoryRepository, "import", JsonMemoryRepository.import_data)


__all__ = [
    "DEFAULT_REPOSITORY_PATH",
    "STORE_VERSION",
    "DuplicateMemoryRecordError",
    "JsonMemoryRepository",
    "MemoryRepository",
]
