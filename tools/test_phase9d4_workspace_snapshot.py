"""Phase 9D.4 — Git-independent WorkspaceSnapshot tests.

Covers the 20 required scenarios: baseline capture, unchanged, added,
modified, deleted, multiple changes, relative paths only, UTF-8 filename,
Unicode paths, nested dirs, excluded .git / .venv / node_modules / __pycache__,
symlink directory not followed, root escape prevented, small-file hash detects
a same-size change, large-file size+mtime fallback, missing workspace error,
and controlled permission/I/O failure.

Pure stdlib + tempfile tests: Qt-free, subprocess-free, no online calls, and
the real project tree is never scanned or modified.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import core.workspace_snapshot as ws_mod
from core.workspace_snapshot import (
    EXCLUDED_DIR_NAMES,
    HASH_SIZE_LIMIT,
    WorkspaceSnapshotError,
    capture,
    changed_files_text,
    diff,
)


def _ws() -> tuple[Path, Path]:
    td = tempfile.mkdtemp(prefix="fap9d4_ws_")
    return Path(td), Path(td)


def _write(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding=encoding)


# -- 1-2. baseline capture / unchanged --------------------------------------

def test_baseline_capture() -> None:
    root, _ = _ws()
    _write(root / "a.py", "print(1)\n")
    _write(root / "sub" / "b.txt", "hello")
    snap = capture(root)
    assert len(snap) == 2
    assert "a.py" in snap.records
    assert "sub/b.txt" in snap.records
    assert snap.records["a.py"].relative_path == "a.py"
    assert snap.records["a.py"].size == (root / "a.py").stat().st_size
    assert snap.records["a.py"].mtime_ns > 0
    assert snap.records["a.py"].sha256 is not None
    assert snap.root == root.resolve()


def test_unchanged() -> None:
    root, _ = _ws()
    _write(root / "a.py", "x = 1")
    baseline = capture(root)
    current = capture(root)
    assert diff(baseline, current).is_empty


# -- 3-6. added / modified / deleted / multiple -----------------------------

def test_added() -> None:
    root, _ = _ws()
    _write(root / "a.py", "x")
    baseline = capture(root)
    _write(root / "new.py", "y")
    changes = diff(baseline, capture(root))
    assert changes.added == ("new.py",)
    assert changes.modified == () and changes.deleted == ()


def test_modified() -> None:
    root, _ = _ws()
    _write(root / "a.py", "one")
    baseline = capture(root)
    _write(root / "a.py", "two")
    changes = diff(baseline, capture(root))
    assert changes.modified == ("a.py",)
    assert changes.added == () and changes.deleted == ()


def test_deleted() -> None:
    root, _ = _ws()
    _write(root / "a.py", "x")
    baseline = capture(root)
    (root / "a.py").unlink()
    changes = diff(baseline, capture(root))
    assert changes.deleted == ("a.py",)
    assert changes.added == () and changes.modified == ()


def test_multiple_changes() -> None:
    root, _ = _ws()
    _write(root / "a.py", "keep")
    _write(root / "b.py", "old")
    _write(root / "c.py", "gone")
    baseline = capture(root)
    _write(root / "a.py", "keep")  # unchanged
    _write(root / "b.py", "new content")  # modified
    _write(root / "d.py", "added")  # added
    (root / "c.py").unlink()  # deleted
    changes = diff(baseline, capture(root))
    assert changes.added == ("d.py",)
    assert changes.modified == ("b.py",)
    assert changes.deleted == ("c.py",)
    assert "a.py" not in changes.added + changes.modified + changes.deleted


# -- 7-10. paths -------------------------------------------------------------

def test_relative_paths_only() -> None:
    root, _ = _ws()
    _write(root / "a.py", "x")
    snap = capture(root)
    for rel in snap.records:
        assert not Path(rel).is_absolute(), f"must be relative: {rel}"
        assert ".." not in rel
    assert root.resolve() not in [snap.records[p].relative_path for p in snap.records]


def test_utf8_filename() -> None:
    root, _ = _ws()
    name = "模块测试.py"
    _write(root / name, "内容")
    baseline = capture(root)
    _write(root / name, "不同内容")
    changes = diff(baseline, capture(root))
    assert changes.modified == (name,)
    assert name in capture(root).records


def test_unicode_paths() -> None:
    root, _ = _ws()
    _write(root / "中文" / "文件 🎉.txt", "emoji path")
    snap = capture(root)
    assert "中文/文件 🎉.txt" in snap.records


def test_nested_dirs() -> None:
    root, _ = _ws()
    _write(root / "a" / "b" / "c" / "deep.py", "deep")
    snap = capture(root)
    assert "a/b/c/deep.py" in snap.records
    assert snap.records["a/b/c/deep.py"].size == 4


# -- 11-14. exclusions -------------------------------------------------------

def _excluded_case(root: Path, dirname: str) -> None:
    _write(root / "keep.py", "keep")
    _write(root / dirname / "junk.py", "junk")
    snap = capture(root)
    assert "keep.py" in snap.records
    assert not any(p.startswith(dirname) for p in snap.records), (
        f"{dirname} must be excluded"
    )


def test_excluded_git() -> None:
    root, _ = _ws()
    _excluded_case(root, ".git")


def test_excluded_venv() -> None:
    root, _ = _ws()
    _excluded_case(root, ".venv")


def test_excluded_node_modules() -> None:
    root, _ = _ws()
    _excluded_case(root, "node_modules")


def test_excluded_pycache() -> None:
    root, _ = _ws()
    _excluded_case(root, "__pycache__")


def test_exclusion_set_contains_required() -> None:
    for required in (".git", ".venv", "venv", "node_modules", "__pycache__", "build", "dist", "runtime"):
        assert required in EXCLUDED_DIR_NAMES, f"missing exclusion: {required}"


# -- 15-16. symlink / escape safety -----------------------------------------

def test_within_guard_rejects_escapes() -> None:
    root, _ = _ws()
    _write(root / "a.py", "x")
    outside = tempfile.mkdtemp(prefix="fap9d4_out_")
    root_resolved = root.resolve()
    assert ws_mod._within(root_resolved, Path(outside).resolve()) is False
    assert ws_mod._within(root_resolved, (root / "..").resolve()) is False
    assert ws_mod._within(root_resolved, root_resolved) is True
    assert ws_mod._within(root_resolved, (root / "a.py").resolve()) is True


def _try_make_symlink_dir(link: Path, target: Path) -> bool:
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        return False


def test_symlink_directory_not_followed() -> None:
    root, _ = _ws()
    outside = Path(tempfile.mkdtemp(prefix="fap9d4_sym_"))
    _write(root / "a.py", "a")
    _write(outside / "secret.py", "s")
    link = root / "link"
    if not _try_make_symlink_dir(link, outside):
        return  # environment cannot create symlinks; guard covered above
    snap = capture(root)
    assert "a.py" in snap.records
    assert not any("secret" in p for p in snap.records)
    assert not any(p.startswith("link") for p in snap.records), "symlink dir must not be followed"


def test_escape_directory_skipped_via_resolve_guard() -> None:
    # Simulate a real directory whose resolved target escapes the root (the
    # stand-in for a junction that os.walk would otherwise descend into).
    root, _ = _ws()
    outside = Path(tempfile.mkdtemp(prefix="fap9d4_esc_"))
    _write(root / "a.py", "a")
    (root / "sub").mkdir()
    _write(outside / "secret.py", "s")
    real_resolve = Path.resolve

    def fake_resolve(self, *args, **kwargs):
        if str(self).endswith("sub"):
            return real_resolve(Path(outside) / "sub", *args, **kwargs)
        return real_resolve(self, *args, **kwargs)

    with patch.object(Path, "resolve", fake_resolve):
        snap = capture(root)
    assert "a.py" in snap.records
    assert not any("secret" in p for p in snap.records), "escaped dir must be skipped"


def test_file_escape_skipped_via_resolve_guard() -> None:
    root, _ = _ws()
    outside = Path(tempfile.mkdtemp(prefix="fap9d4_fesc_"))
    _write(root / "a.py", "a")
    (root / "evil.txt").write_text("x", encoding="utf-8")
    _write(outside / "real.txt", "sensitive")
    real_resolve = Path.resolve

    def fake_resolve(self, *args, **kwargs):
        if str(self).endswith("evil.txt"):
            return real_resolve(Path(outside) / "real.txt", *args, **kwargs)
        return real_resolve(self, *args, **kwargs)

    with patch.object(Path, "resolve", fake_resolve):
        snap = capture(root)
    assert "a.py" in snap.records
    assert "evil.txt" not in snap.records, "escaping file must be skipped"


# -- 17-18. hashing strategy -------------------------------------------------

def test_small_file_hash_detects_same_size_change() -> None:
    root, _ = _ws()
    _write(root / "a.py", "hello")
    baseline = capture(root)
    _write(root / "a.py", "jello")  # same size, different content
    changes = diff(baseline, capture(root))
    assert changes.modified == ("a.py",)
    assert baseline.records["a.py"].strategy == "hash"


def test_large_file_fallback() -> None:
    root, _ = _ws()
    big = root / "big.bin"
    payload = os.urandom(1024 * 1024)  # 1 MiB chunk
    with open(big, "wb") as handle:
        for _ in range(6):
            handle.write(payload)  # 6 MiB > HASH_SIZE_LIMIT
    assert big.stat().st_size > HASH_SIZE_LIMIT
    baseline = capture(root)
    rec = baseline.records["big.bin"]
    assert rec.sha256 is None, "large file must not be hashed"
    assert rec.strategy == "size_mtime"
    # Same content: unchanged even though a full diff runs.
    assert diff(baseline, capture(root)).is_empty
    # Size change is detected via the size_mtime fallback.
    with open(big, "ab") as handle:
        handle.write(b"more")
    assert diff(baseline, capture(root)).modified == ("big.bin",)


def test_mtime_only_change_detected_for_large_file() -> None:
    root, _ = _ws()
    big = root / "big.bin"
    payload = os.urandom(1024 * 1024)
    with open(big, "wb") as handle:
        for _ in range(6):
            handle.write(payload)
    baseline = capture(root)
    new_mtime = baseline.records["big.bin"].mtime_ns + 1_000_000_000
    os.utime(big, ns=(new_mtime, new_mtime))
    changes = diff(baseline, capture(root))
    assert changes.modified == ("big.bin",), "mtime_ns must be compared for large files"


# -- 19-20. error handling ---------------------------------------------------

def test_missing_workspace_error() -> None:
    missing = Path(tempfile.mkdtemp(prefix="fap9d4_miss_")) / "nope"
    try:
        capture(missing)
    except WorkspaceSnapshotError:
        return
    raise AssertionError("a missing workspace must raise WorkspaceSnapshotError")


def test_workspace_root_is_file_error() -> None:
    root, _ = _ws()
    f = root / "file.txt"
    _write(f, "x")
    try:
        capture(f)
    except WorkspaceSnapshotError:
        return
    raise AssertionError("a file as root must raise WorkspaceSnapshotError")


def test_io_failure_skips_file_controlled() -> None:
    root, _ = _ws()
    _write(root / "a.py", "a")
    _write(root / "b.py", "b")
    real_record_file = ws_mod._record_file

    def flaky_record(full: Path, rel: str):
        if full.name == "b.py":
            return None  # simulated unreadable file
        return real_record_file(full, rel)

    with patch("core.workspace_snapshot._record_file", side_effect=flaky_record):
        snap = capture(root)
    assert "a.py" in snap.records
    assert "b.py" not in snap.records
    assert "b.py" in snap.skipped, "a failed file must be recorded as skipped"
    # The rest of the workspace still scanned; nothing raised.


# -- artifact text -----------------------------------------------------------

def test_changed_files_text_relative_only() -> None:
    changes = ws_mod.ChangeSet(
        added=("a.py",), modified=("sub/b.txt",), deleted=("gone.py",)
    )
    text = changed_files_text(changes)
    assert text.startswith("# Changed Files")
    assert "## Added\n- a.py" in text
    assert "## Modified\n- sub/b.txt" in text
    assert "## Deleted\n- gone.py" in text
    for line in text.splitlines():
        if line.startswith("- "):
            path = line[2:]
            assert not Path(path).is_absolute()
            assert ".." not in path


def test_changed_files_text_empty_sections_none() -> None:
    text = changed_files_text(ws_mod.ChangeSet(added=("a.py",)))
    assert "## Modified\n- (none)" in text
    assert "## Deleted\n- (none)" in text


def test_changed_files_text_no_content_or_secrets() -> None:
    changes = ws_mod.ChangeSet(added=("a.py",))
    text = changed_files_text(changes)
    assert "print" not in text  # never file contents
    assert "api_key" not in text and "secret" not in text.lower()


def main() -> None:
    tests = [
        test_baseline_capture,
        test_unchanged,
        test_added,
        test_modified,
        test_deleted,
        test_multiple_changes,
        test_relative_paths_only,
        test_utf8_filename,
        test_unicode_paths,
        test_nested_dirs,
        test_excluded_git,
        test_excluded_venv,
        test_excluded_node_modules,
        test_excluded_pycache,
        test_exclusion_set_contains_required,
        test_within_guard_rejects_escapes,
        test_symlink_directory_not_followed,
        test_escape_directory_skipped_via_resolve_guard,
        test_file_escape_skipped_via_resolve_guard,
        test_small_file_hash_detects_same_size_change,
        test_large_file_fallback,
        test_mtime_only_change_detected_for_large_file,
        test_missing_workspace_error,
        test_workspace_root_is_file_error,
        test_io_failure_skips_file_controlled,
        test_changed_files_text_relative_only,
        test_changed_files_text_empty_sections_none,
        test_changed_files_text_no_content_or_secrets,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception:
            failed += 1
            print(f"FAIL  {fn.__name__}")
            raise
    print(f"Phase 9D.4 workspace snapshot tests passed ({len(tests)} tests).")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
