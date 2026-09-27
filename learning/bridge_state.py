"""Bridge orchestration state machine (Phase 8).

These states describe *orchestration* only — where the bridge stands in the
import → process → review → ready → learn lifecycle. Learning facts
(mastery, progress, misconceptions) NEVER live here; they come from teach-mcp
at runtime. Derived, not stored: the only files consulted are the bridge
markers plus course_binding.json.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from learning._storage import atomic_write_json, utc_now_iso
from learning.course_binding import load_binding

RESOURCE_NEW = "RESOURCE_NEW"
PROCESSING = "PROCESSING"
WAITING_REVIEW = "WAITING_REVIEW"
COURSE_READY = "COURSE_READY"
LEARNING_ACTIVE = "LEARNING_ACTIVE"
RESUME = "RESUME"

STATES = (RESOURCE_NEW, PROCESSING, WAITING_REVIEW, COURSE_READY, LEARNING_ACTIVE, RESUME)

PROCESSING_MARKER = Path("bridge") / "processing.json"
ACTIVE_MARKER = Path("bridge") / "active.json"


def _marker_path(course_dir: Path | str, relative: Path) -> Path:
    return Path(course_dir) / relative


def write_processing_marker(course_dir: Path | str, *, now: str | None = None) -> Path:
    return atomic_write_json(
        _marker_path(course_dir, PROCESSING_MARKER),
        {"started_at": now or utc_now_iso()},
    )


def clear_processing_marker(course_dir: Path | str) -> bool:
    return _clear_marker(_marker_path(course_dir, PROCESSING_MARKER))


def write_active_marker(course_dir: Path | str, *, now: str | None = None) -> Path:
    return atomic_write_json(
        _marker_path(course_dir, ACTIVE_MARKER),
        {"started_at": now or utc_now_iso()},
    )


def clear_active_marker(course_dir: Path | str) -> bool:
    return _clear_marker(_marker_path(course_dir, ACTIVE_MARKER))


def _clear_marker(path: Path) -> bool:
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _has_open_session(session_db: Path | str, learner_id: str) -> bool:
    """Read-only peek into teach-mcp's session.db; degrade to False on error.

    This is display-grade orchestration info, never written down and never
    treated as authoritative — the skill still resolves real state via
    teach-mcp tools at runtime.
    """
    resolved = Path(session_db).resolve()
    uri = "file:///" + resolved.as_posix() + "?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            row = con.execute(
                "SELECT 1 FROM learning_sessions WHERE learner_id = ? AND status = 'open'"
                " LIMIT 1",
                (learner_id,),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return False
    return row is not None


def derive_state(
    course_dir: Path | str,
    *,
    learner_id: str | None = None,
    session_db: Path | str | None = None,
) -> str:
    """Recommend the orchestration state from on-disk protocol artifacts.

    Rules (frozen by the bridge spec):
      - processing marker                       -> PROCESSING
      - active marker                           -> LEARNING_ACTIVE
      - no binding                              -> RESOURCE_NEW
      - pipeline_id set, no curriculum_id       -> WAITING_REVIEW
      - zcode_session_id set                     -> RESUME
      - curriculum_id + open teach-mcp session  -> RESUME
      - curriculum_id, no open session          -> COURSE_READY
    """
    course_dir = Path(course_dir)
    if _marker_path(course_dir, PROCESSING_MARKER).is_file():
        return PROCESSING
    if _marker_path(course_dir, ACTIVE_MARKER).is_file():
        return LEARNING_ACTIVE
    binding = load_binding(course_dir)
    if not binding:
        return RESOURCE_NEW
    if binding.get("pipeline_id") and not binding.get("curriculum_id"):
        return WAITING_REVIEW
    # A persisted Z Code conversation is already a learning entry. The skill
    # asks teach-mcp for the real learning state after the user resumes it.
    if binding.get("zcode_session_id"):
        return RESUME
    if binding.get("curriculum_id"):
        if learner_id and session_db and _has_open_session(session_db, learner_id):
            return RESUME
        return COURSE_READY
    return RESOURCE_NEW


def state_summary(course_dir: Path | str, state: str) -> str:
    """Human-facing one-liner per state (UI copy lives here, single source)."""
    labels = {
        RESOURCE_NEW: "新资料：可开始处理",
        PROCESSING: "正在处理学习资料…",
        WAITING_REVIEW: "知识包待人工审核",
        COURSE_READY: "课程就绪：可开始学习",
        LEARNING_ACTIVE: "学习会话进行中",
        RESUME: "有进行中的学习：可继续",
    }
    return labels.get(state, state)


def action_for_state(state: str) -> str | None:
    """Map an orchestration state to the Learning Context action to launch.

    None means launching is not meaningful right now (PROCESSING). COURSE_READY
    uses ``resume``: the skill then asks teach-mcp and either start_learning
    (no open session) or continues the existing one — Firefly never decides
    session matters itself.
    """
    mapping = {
        RESOURCE_NEW: "new",
        PROCESSING: None,
        WAITING_REVIEW: "review",
        COURSE_READY: "resume",
        LEARNING_ACTIVE: "resume",
        RESUME: "resume",
    }
    return mapping.get(state)
