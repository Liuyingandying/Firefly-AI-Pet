"""Canonical per-user storage paths and non-destructive legacy migration.

All mutable Firefly data belongs below ``%LOCALAPPDATA%/FireflyAI`` on
Windows.  The path object is intentionally independent from Qt and the
business stores: callers may still inject explicit paths exactly as before.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MIGRATION_VERSION = 1
_INITIALIZE_LOCK = threading.RLock()


def _default_root() -> Path:
    override = os.environ.get("FIREFLY_USER_DATA_DIR")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "FireflyAI"
    return Path.home() / ".config" / "FireflyAI"


@dataclass(frozen=True, slots=True)
class UserDataPaths:
    """One immutable description of Firefly's mutable-data layout."""

    root: Path

    def __init__(self, root: Path | str | None = None) -> None:
        object.__setattr__(self, "root", Path(root) if root is not None else _default_root())

    @property
    def runtime(self) -> Path:
        return self.root / "runtime"

    @property
    def memory(self) -> Path:
        return self.root / "memory"

    @property
    def conversation(self) -> Path:
        return self.root / "conversation"

    @property
    def scratchpad(self) -> Path:
        return self.root / "scratchpad"

    @property
    def suggestions(self) -> Path:
        return self.root / "suggestions"

    @property
    def learning(self) -> Path:
        return self.root / "learning"

    @property
    def models(self) -> Path:
        return self.root / "models"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def plugins(self) -> Path:
        return self.root / "plugins"

    @property
    def credentials(self) -> Path:
        return self.root / "credentials"

    @property
    def migration_report(self) -> Path:
        return self.root / "migration_report.json"

    def directories(self) -> tuple[Path, ...]:
        return (
            self.runtime,
            self.memory,
            self.conversation,
            self.scratchpad,
            self.suggestions,
            self.learning,
            self.models,
            self.logs,
            self.plugins,
        )

    def ensure_layout(self) -> None:
        for directory in (self.root, *self.directories()):
            directory.mkdir(parents=True, exist_ok=True)


DEFAULT_USER_PATHS = UserDataPaths()


def get_user_data_paths() -> UserDataPaths:
    """Return the process default paths without performing migration."""

    return DEFAULT_USER_PATHS


def initialize_user_data(
    *,
    legacy_project_dir: Path | str | None = None,
    paths: UserDataPaths | None = None,
) -> dict[str, Any]:
    """Create the user layout and copy legacy data once, without deletion.

    Existing destinations are never overwritten.  Each copied file is first
    written beside its destination and atomically replaced into place.  A
    completed report is the idempotency marker used by later starts.
    """

    selected = paths or get_user_data_paths()
    legacy_root = (
        Path(legacy_project_dir)
        if legacy_project_dir is not None
        else Path(__file__).resolve().parent.parent
    )
    with _INITIALIZE_LOCK:
        selected.ensure_layout()
        previous = _read_completed_report(selected.migration_report)
        if previous is not None:
            return previous

        started_at = int(time.time() * 1000)
        entries: list[dict[str, Any]] = []
        errors: list[str] = []
        legacy_runtime = legacy_root / "runtime"
        legacy_found = legacy_runtime.is_dir()

        try:
            if legacy_found:
                mappings = _legacy_mappings(legacy_root, selected)
                for source, destination, excluded in mappings:
                    _copy_path(source, destination, entries, errors, excluded=excluded)
        except Exception as exc:  # last-resort startup containment
            errors.append(f"migration aborted: {type(exc).__name__}: {exc}")

        validation = _validate_domains(selected)
        if validation.get("errors"):
            errors.extend(str(item) for item in validation["errors"])
        report: dict[str, Any] = {
            "version": MIGRATION_VERSION,
            "status": "completed" if not errors else "completed_with_errors",
            "started_at_ms": started_at,
            "finished_at_ms": int(time.time() * 1000),
            "legacy_project_dir": str(legacy_root),
            "legacy_runtime_found": legacy_found,
            "destination_root": str(selected.root),
            "source_deleted": False,
            "entries": entries,
            "validation": validation,
            "errors": errors,
        }
        _atomic_write_json(selected.migration_report, report)
        return report


def _legacy_mappings(
    project: Path, paths: UserDataPaths
) -> tuple[tuple[Path, Path, frozenset[str]], ...]:
    runtime = project / "runtime"
    companion = runtime / "companion"
    return (
        # Generic runtime state stays under the new runtime domain.  Domain
        # stores below are excluded and copied into their dedicated roots.
        (runtime, paths.runtime, frozenset({"companion", "memory", "learning"})),
        (companion / "memory_records.json", paths.memory / "memory_records.json", frozenset()),
        (companion / "backups", paths.memory / "backups", frozenset()),
        (companion / "migration", paths.memory / "migration", frozenset()),
        (runtime / "memory", paths.memory, frozenset({"models"})),
        (runtime / "memory" / "models", paths.models, frozenset()),
        (companion / "conversation.json", paths.conversation / "conversation.json", frozenset()),
        (companion / "bond_state.json", paths.conversation / "bond_state.json", frozenset()),
        (companion / "scratchpad", paths.scratchpad, frozenset()),
        (companion / "memory_suggestions.json", paths.suggestions / "memory_suggestions.json", frozenset()),
        (runtime / "learning", paths.learning, frozenset()),
        (project / "logs", paths.logs, frozenset()),
    )


def _copy_path(
    source: Path,
    destination: Path,
    entries: list[dict[str, Any]],
    errors: list[str],
    *,
    excluded: frozenset[str],
) -> None:
    if not source.exists():
        return
    if source.is_file():
        _copy_file(source, destination, entries, errors)
        return
    for item in sorted(source.rglob("*")):
        relative = item.relative_to(source)
        if relative.parts and relative.parts[0] in excluded:
            continue
        target = destination / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            _copy_file(item, target, entries, errors)


def _copy_file(
    source: Path,
    destination: Path,
    entries: list[dict[str, Any]],
    errors: list[str],
) -> None:
    entry: dict[str, Any] = {"source": str(source), "destination": str(destination)}
    try:
        source_hash = _sha256(source)
        entry["sha256"] = source_hash
        entry["bytes"] = source.stat().st_size
        if destination.exists():
            entry["status"] = (
                "already_present" if destination.is_file() and _sha256(destination) == source_hash
                else "conflict_skipped"
            )
            if entry["status"] == "conflict_skipped":
                errors.append(
                    f"destination conflict (not overwritten): {destination}"
                )
            entries.append(entry)
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".migration-tmp")
        try:
            shutil.copy2(source, temporary)
            if _sha256(temporary) != source_hash:
                raise OSError("copied file checksum mismatch")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        entry["status"] = "copied"
    except Exception as exc:
        entry["status"] = "error"
        entry["error"] = f"{type(exc).__name__}: {exc}"
        errors.append(f"{source}: {entry['error']}")
    entries.append(entry)


def _validate_domains(paths: UserDataPaths) -> dict[str, Any]:
    result: dict[str, Any] = {"errors": []}
    memory_file = paths.memory / "memory_records.json"
    if memory_file.exists():
        try:
            payload = json.loads(memory_file.read_text(encoding="utf-8"))
            records = payload.get("records") if isinstance(payload, dict) else None
            if not isinstance(records, list):
                raise ValueError("records must be a list")
            result["memory"] = {
                "record_count": len(records),
                "sha256": _sha256(memory_file),
                "valid": True,
            }
        except (OSError, UnicodeError, ValueError) as exc:
            result["errors"].append(f"memory validation failed: {exc}")

    conversation_file = paths.conversation / "conversation.json"
    if conversation_file.exists():
        try:
            payload = json.loads(conversation_file.read_text(encoding="utf-8"))
            sessions = payload.get("sessions") if isinstance(payload, dict) else None
            if not isinstance(sessions, dict):
                raise ValueError("sessions must be an object")
            result["conversation"] = {"session_count": len(sessions), "valid": True}
        except (OSError, UnicodeError, ValueError) as exc:
            result["errors"].append(f"conversation validation failed: {exc}")

    items_file = paths.scratchpad / "items.json"
    if items_file.exists():
        try:
            payload = json.loads(items_file.read_text(encoding="utf-8"))
            items = payload.get("items") if isinstance(payload, dict) else None
            if not isinstance(items, list):
                raise ValueError("items must be a list")
            missing_assets = []
            for item in items:
                relative = item.get("asset_path") if isinstance(item, dict) else None
                if relative and not (paths.scratchpad / relative).is_file():
                    missing_assets.append(str(relative))
            if missing_assets:
                raise ValueError(f"missing assets: {missing_assets}")
            result["scratchpad"] = {"item_count": len(items), "valid": True}
        except (OSError, UnicodeError, ValueError) as exc:
            result["errors"].append(f"scratchpad validation failed: {exc}")

    databases = sorted(paths.learning.glob("*.sqlite3"))
    if databases:
        db_result = []
        for database in databases:
            try:
                connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
                try:
                    status = connection.execute("PRAGMA integrity_check").fetchone()[0]
                finally:
                    connection.close()
                if status != "ok":
                    raise ValueError(status)
                db_result.append({"path": str(database), "integrity_check": "ok"})
            except (OSError, sqlite3.Error, ValueError) as exc:
                result["errors"].append(f"learning validation failed for {database}: {exc}")
        result["learning"] = db_result
    return result


def _read_completed_report(path: Path) -> dict[str, Any] | None:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, ValueError):
        return None
    if (
        isinstance(report, dict)
        and report.get("version") == MIGRATION_VERSION
        and report.get("status") == "completed"
    ):
        return report
    return None


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


__all__ = [
    "DEFAULT_USER_PATHS",
    "MIGRATION_VERSION",
    "UserDataPaths",
    "get_user_data_paths",
    "initialize_user_data",
]
