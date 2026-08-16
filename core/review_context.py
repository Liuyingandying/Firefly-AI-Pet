"""Deterministic read-only review context builder (Phase 9D.6-H4).

The direct provider has no local file tools, so the Review step must inline the
current contents of the changed files. This module reads only the files listed
in the CHANGED_FILES artifact — never the whole repository — and enforces path
and size safety.

Every path from CHANGED_FILES is treated as untrusted input: relative-only,
resolved to stay inside the workspace root, rejecting ``..``, absolute paths,
drive/UNC escapes, and symlink escapes. Large files are truncated with an
explicit ``[truncated]`` marker; binary files are omitted with
``[binary file omitted]``. Qt-free and stdlib-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Per-file content cap (characters) and total context cap (characters across all
# files). These keep a single huge file — or many files — from overflowing the
# prompt. Truncation is always explicit, never silent.
PER_FILE_CAP = 64 * 1024
TOTAL_CONTEXT_CAP = 256 * 1024

# Byte read headroom per file: enough for the 4-byte UTF-8 worst case while we
# accumulate up to ``PER_FILE_CAP`` characters.
_READ_CHUNK = 64 * 1024


def parse_changed_files(text: str | None) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Parse the CHANGED_FILES artifact into (added, modified, deleted).

    The artifact uses ``## Added`` / ``## Modified`` / ``## Deleted`` sections,
    one ``- path`` bullet per line, with ``- (none)`` as the empty placeholder.
    Paths are workspace-relative POSIX strings.
    """
    added: list[str] = []
    modified: list[str] = []
    deleted: list[str] = []
    current: list[str] | None = None
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            header = line[3:].strip().lower()
            if header == "added":
                current = added
            elif header == "modified":
                current = modified
            elif header == "deleted":
                current = deleted
            else:
                current = None
        elif line.startswith("- ") and current is not None:
            path = line[2:].strip()
            if path and path != "(none)":
                current.append(path)
    return tuple(added), tuple(modified), tuple(deleted)


def _is_unsafe_relative(rel: str) -> bool:
    """True when a path string must not be resolved against the workspace root."""
    if not rel or "\x00" in rel:
        return True
    # Drive-relative / drive-absolute (C:, C:\...) are absolute-ish escapes.
    if len(rel) >= 2 and rel[1] == ":":
        return True
    norm = rel.replace("\\", "/")
    # Leading slash = POSIX absolute or UNC; reject before pathlib treats it
    # as relative-to-current-drive on Windows.
    if norm.startswith("/"):
        return True
    parts = norm.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return True
    return False


def _safe_workspace_path(root_resolved: Path, rel: str) -> Path | None:
    """Resolve a workspace-relative path and prove it stays inside the root.

    Returns None for any path that is unsafe (absolute, drive/UNC, ``..``,
    empty segment) or whose resolved target escapes the root (symlink/junction
    escape included, because :meth:`Path.resolve` follows them).
    """
    if _is_unsafe_relative(rel):
        return None
    try:
        candidate = (root_resolved / Path(rel)).resolve()
    except (OSError, ValueError):
        return None
    try:
        candidate.relative_to(root_resolved)
    except ValueError:
        return None
    return candidate


@dataclass(frozen=True, slots=True)
class ReviewFileEntry:
    """One changed path and its (possibly omitted/truncated) current content."""

    path: str
    kind: str  # "added" | "modified" | "deleted"
    content: str | None
    note: str | None  # "deleted" | "binary file omitted" | "truncated" | "missing" | "unsafe path omitted" | "omitted (context size limit)"


@dataclass(frozen=True, slots=True)
class ReviewContext:
    """Current contents of the changed files, ready to inline into a review prompt."""

    entries: tuple[ReviewFileEntry, ...]
    truncated: bool
    total_chars: int
    total_cap: int

    def render(self) -> str:
        """Render the context as deterministic Markdown for the review prompt."""
        lines = ["## Current changed file contents", ""]
        for entry in self.entries:
            if entry.kind == "deleted":
                lines.append(f"- [{entry.path}] deleted")
                lines.append("")
            elif entry.content is None:
                label = entry.note or "omitted"
                lines.append(f"- {entry.path} [{label}]")
                lines.append("")
            else:
                lines.append(f"### {entry.path}")
                lines.append("```")
                lines.append(entry.content)
                lines.append("```")
                if entry.note == "truncated":
                    lines.append("[truncated]")
                lines.append("")
        return "\n".join(lines).rstrip() + "\n"


def _read_text_capped(path: Path, char_cap: int) -> tuple[str, bool, bool]:
    """Read up to ``char_cap`` characters. Returns (text, truncated, binary)."""
    data = bytearray()
    truncated = False
    try:
        with open(path, "rb") as handle:
            while len(data) < char_cap * 4:
                chunk = handle.read(_READ_CHUNK)
                if not chunk:
                    break
                data.extend(chunk)
            # A further non-empty read means the file is larger than our window.
            if handle.read(1):
                truncated = True
    except OSError:
        return "", False, False

    if b"\x00" in data:
        return "", True, True

    text = bytes(data).decode("utf-8", "replace")
    if len(text) > char_cap:
        text = text[:char_cap]
        truncated = True
    return text, truncated, False


def _entry_for(
    root_resolved: Path, kind: str, rel: str, per_file_cap: int
) -> ReviewFileEntry:
    if kind == "deleted":
        return ReviewFileEntry(rel, kind, None, "deleted")
    path = _safe_workspace_path(root_resolved, rel)
    if path is None:
        return ReviewFileEntry(rel, kind, None, "unsafe path omitted")
    if not path.is_file():
        return ReviewFileEntry(rel, kind, None, "missing")
    text, truncated, binary = _read_text_capped(path, per_file_cap)
    if binary:
        return ReviewFileEntry(rel, kind, None, "binary file omitted")
    return ReviewFileEntry(rel, kind, text, "truncated" if truncated else None)


def build_review_context(
    workspace: Path | str,
    changed_files_text: str | None,
    *,
    per_file_cap: int = PER_FILE_CAP,
    total_cap: int = TOTAL_CONTEXT_CAP,
) -> ReviewContext:
    """Build a ReviewContext from a CHANGED_FILES artifact and the workspace.

    Only Added / Modified files are read from disk; Deleted files are recorded
    as markers. Reads are bounded by ``per_file_cap`` and the total by
    ``total_cap``, each with an explicit marker. A missing workspace root yields
    an empty context (every changed file becomes "missing").
    """
    workspace_path = Path(workspace)
    try:
        root_resolved = workspace_path.resolve()
    except OSError:
        root_resolved = workspace_path

    added, modified, deleted = parse_changed_files(changed_files_text)

    entries: list[ReviewFileEntry] = []
    truncated = False
    total = 0
    for kind, paths in (("added", added), ("modified", modified), ("deleted", deleted)):
        for rel in paths:
            entry = _entry_for(root_resolved, kind, rel, per_file_cap)
            if entry.note == "truncated":
                truncated = True
            if entry.content is not None:
                remaining = total_cap - total
                if remaining <= 0:
                    entry = ReviewFileEntry(
                        entry.path, entry.kind, None, "omitted (context size limit)"
                    )
                    truncated = True
                elif len(entry.content) > remaining:
                    entry = ReviewFileEntry(
                        entry.path, entry.kind, entry.content[:remaining], "truncated"
                    )
                    truncated = True
                    total += remaining
                else:
                    total += len(entry.content)
            entries.append(entry)

    return ReviewContext(
        entries=tuple(entries),
        truncated=truncated,
        total_chars=total,
        total_cap=total_cap,
    )
