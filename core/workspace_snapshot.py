"""Git-independent workspace change evidence for Phase 9D.4.

Qt-free and stdlib-only. A :class:`Snapshot` is a cheap fingerprint of a
workspace: one record per file (relative path, size, mtime_ns, and a sha256
for small files). A baseline snapshot and a current snapshot diff to a
:class:`ChangeSet` (added / modified / deleted) that never contains file
contents, absolute paths, or secrets.

Git is deliberately not a dependency. No watchdog is used, nothing here spawns
a subprocess, and no full workspace copy is ever kept.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

# Huge / generated / transient directories that are always skipped when
# scanning a workspace. ``runtime`` is excluded because the Firefly project
# itself can be a target workspace and its runtime/* trees (artifacts,
# metrics, state) must never appear as "changes" caused by a workflow.
EXCLUDED_DIR_NAMES = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        "build",
        "dist",
        "runtime",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".idea",
        ".vscode",
    }
)

# Files at or below this size get a sha256 so a same-size / same-mtime rewrite
# is still detected. Larger files fall back to size + mtime_ns only (no
# unbounded hashing).
HASH_SIZE_LIMIT = 5 * 1024 * 1024

_READ_CHUNK = 64 * 1024


class WorkspaceSnapshotError(Exception):
    """The workspace cannot be scanned at all (missing / unreadable root)."""


@dataclass(frozen=True, slots=True)
class FileRecord:
    relative_path: str  # POSIX-style, workspace-relative
    size: int
    mtime_ns: int
    sha256: str | None  # None for large files (size_mtime strategy)
    strategy: str  # "hash" | "size_mtime"


@dataclass(frozen=True, slots=True)
class Snapshot:
    root: Path
    records: dict  # relative_path -> FileRecord
    skipped: tuple[str, ...] = ()  # files not scanned (controlled I/O failures)

    def __len__(self) -> int:
        return len(self.records)


@dataclass(frozen=True, slots=True)
class ChangeSet:
    added: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.modified or self.deleted)


def capture(root: Path | str) -> Snapshot:
    """Scan ``root`` into a Snapshot of per-file fingerprints.

    Raises :class:`WorkspaceSnapshotError` when the root itself cannot be
    scanned. Per-file I/O failures are contained: the file is recorded in
    ``Snapshot.skipped`` and the rest of the workspace still scans.
    """
    root_path = Path(root)
    if not root_path.exists() or not root_path.is_dir():
        raise WorkspaceSnapshotError(f"workspace is not a directory: {root_path}")
    root_resolved = root_path.resolve()
    records: dict[str, FileRecord] = {}
    skipped: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root_resolved):
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in EXCLUDED_DIR_NAMES and _safe_subdir(root_resolved, dirpath, d)
        )
        for fname in filenames:
            full = Path(dirpath, fname)
            rel = _relative(root_resolved, dirpath, fname)
            try:
                resolved = full.resolve()
            except OSError:
                skipped.append(rel)
                continue
            if not _within(root_resolved, resolved):
                # A symlink/junction whose target escapes the workspace: skip.
                skipped.append(rel)
                continue
            record = _record_file(full, rel)
            if record is None:
                skipped.append(rel)
            else:
                records[rel] = record
    return Snapshot(root=root_resolved, records=records, skipped=tuple(sorted(skipped)))


def diff(baseline: Snapshot, current: Snapshot) -> ChangeSet:
    """Classify files present in baseline/current into a stable ChangeSet."""
    added = [p for p in current.records if p not in baseline.records]
    deleted = [p for p in baseline.records if p not in current.records]
    modified = [
        p
        for p in baseline.records
        if p in current.records and not _records_equal(baseline.records[p], current.records[p])
    ]
    return ChangeSet(
        added=tuple(sorted(added)),
        modified=tuple(sorted(modified)),
        deleted=tuple(sorted(deleted)),
    )


def changed_files_text(changes: ChangeSet) -> str:
    """Deterministic markdown artifact for a ChangeSet.

    Workspace-relative POSIX paths only: never absolute paths, never file
    contents, never secrets.
    """
    out = ["# Changed Files", ""]
    for header, paths in (
        ("## Added", changes.added),
        ("## Modified", changes.modified),
        ("## Deleted", changes.deleted),
    ):
        out.append(header)
        if paths:
            out.extend(f"- {p}" for p in paths)
        else:
            out.append("- (none)")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _records_equal(a: FileRecord, b: FileRecord) -> bool:
    if a.sha256 is not None and b.sha256 is not None:
        return a.sha256 == b.sha256
    return a.size == b.size and a.mtime_ns == b.mtime_ns


def _safe_subdir(root_resolved: Path, dirpath: str, name: str) -> bool:
    """True only when ``dirpath/name`` may be descended into.

    Rejects symlink/junction directories outright (never followed) and any
    directory whose resolved target escapes the workspace root.
    """
    candidate = Path(dirpath, name)
    if candidate.is_symlink():
        return False
    try:
        resolved = candidate.resolve()
    except OSError:
        return False
    return _within(root_resolved, resolved)


def _within(root_resolved: Path, resolved: Path) -> bool:
    if resolved == root_resolved:
        return True
    try:
        resolved.relative_to(root_resolved)
        return True
    except ValueError:
        return False


def _relative(root_resolved: Path, dirpath: str, fname: str) -> str:
    try:
        rel = Path(dirpath).relative_to(root_resolved) / fname
    except ValueError:
        return fname
    return rel.as_posix()


def _record_file(full: Path, rel: str) -> FileRecord | None:
    try:
        st = full.stat()
    except OSError:
        return None
    size = st.st_size
    mtime_ns = st.st_mtime_ns
    if size <= HASH_SIZE_LIMIT:
        digest = _sha256(full)
        if digest is None:
            return None
        return FileRecord(rel, size, mtime_ns, digest, "hash")
    return FileRecord(rel, size, mtime_ns, None, "size_mtime")


def _sha256(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(_READ_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()
