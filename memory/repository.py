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

from .records import MemoryCategory, MemoryRecord, MemorySource


STORE_VERSION = 1
PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_REPOSITORY_PATH = PROJECT_DIR / "runtime" / "companion" / "memory_records.json"


class DuplicateMemoryRecordError(ValueError):
    """Raised when adding a record whose ID already exists."""


class MemoryRepository(ABC):
    """Storage-independent contract for authoritative memory records."""

    @abstractmethod
    def add(self, record: MemoryRecord) -> str:
        """Persist ``record`` and return its ID; reject duplicate IDs."""

    @abstractmethod
    def get(self, record_id: str) -> MemoryRecord | None:
        """Return a record and persist its access timestamp, or return None."""

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
    def update(self, record_id: str, patch: Mapping[str, Any]) -> MemoryRecord:
        """Apply known mutable fields and advance ``updated_ts``."""

    @abstractmethod
    def delete(self, record_id: str) -> bool:
        """Delete a record, returning False when it does not exist."""

    @abstractmethod
    def clear(self) -> int:
        """Delete every record and return the number removed."""

    @abstractmethod
    def all_ids(self) -> set[str]:
        """Return all authoritative record IDs."""

    @abstractmethod
    def export(self) -> dict[str, Any]:
        """Return a complete JSON-compatible snapshot."""

    @abstractmethod
    def import_data(self, data: Mapping[str, Any]) -> int:
        """Validate and merge records, returning the number imported."""


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
    }

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_REPOSITORY_PATH
        self._lock = RLock()
        self._records = self._load()

    def add(self, record: MemoryRecord) -> str:
        if not isinstance(record, MemoryRecord):
            raise TypeError("record must be a MemoryRecord")
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
        normalized_id = self._record_id(record_id)
        with self._lock:
            current = self._records.get(normalized_id)
            if current is None:
                return None
            accessed = replace(
                current,
                last_accessed_ts=max(self._now_ms(), current.last_accessed_ts + 1),
            )
            proposed = dict(self._records)
            proposed[normalized_id] = accessed
            self._persist(proposed)
            self._records = proposed
            return accessed

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

    def update(self, record_id: str, patch: Mapping[str, Any]) -> MemoryRecord:
        normalized_id = self._record_id(record_id)
        if not isinstance(patch, Mapping):
            raise TypeError("patch must be a mapping")
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

    def delete(self, record_id: str) -> bool:
        normalized_id = self._record_id(record_id)
        with self._lock:
            if normalized_id not in self._records:
                return False
            proposed = dict(self._records)
            del proposed[normalized_id]
            self._persist(proposed)
            self._records = proposed
            return True

    def clear(self) -> int:
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

    def import_data(self, data: Mapping[str, Any]) -> int:
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
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

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
