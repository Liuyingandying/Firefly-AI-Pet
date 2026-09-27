"""Course binding protocol (Phase 6).

teach-mcp has no native course_id, so the bridge keeps ONE thin file per
course — ``bridge/course_binding.json`` — whose only permitted content is the
opaque identifiers teach-mcp itself returned (pipeline_id / curriculum_id).
Any mastery/progress-style field in this file is an architecture violation.
"""

from __future__ import annotations

from pathlib import Path

from learning._storage import BridgeFileError, atomic_write_json, read_json
from learning.resource_manager import FORBIDDEN_STATE_KEYS

BINDING_RELATIVE_PATH = Path("bridge") / "course_binding.json"
ALLOWED_KEYS = ("course_id", "pipeline_id", "curriculum_id", "zcode_session_id")


class BindingError(BridgeFileError):
    pass


def binding_path(course_dir: Path | str) -> Path:
    return Path(course_dir) / BINDING_RELATIVE_PATH


def load_binding(course_dir: Path | str) -> dict | None:
    """Return the binding dict, or None when the course has none yet."""
    path = binding_path(course_dir)
    if not path.is_file():
        return None
    data = read_json(path)
    _validate_binding(data)
    return data


def save_binding(
    course_dir: Path | str,
    course_id: str,
    pipeline_id: str | None = None,
    curriculum_id: str | None = None,
    zcode_session_id: str | None = None,
) -> Path:
    """Persist ONLY opaque ids previously returned by teach-mcp / Z Code.

    ``zcode_session_id`` (Return Channel→Interactive v0.1) is a Z Code UI
    restore reference (from the init run's --json sessionId) — an opaque ref
    for reopening the course's learning session, never learning state.
    """
    payload: dict = {"course_id": str(course_id).strip()}
    if pipeline_id:
        payload["pipeline_id"] = str(pipeline_id).strip()
    if curriculum_id:
        payload["curriculum_id"] = str(curriculum_id).strip()
    if zcode_session_id:
        payload["zcode_session_id"] = str(zcode_session_id).strip()
    _validate_binding(payload)
    return atomic_write_json(binding_path(course_dir), payload)


def _validate_binding(data: object) -> None:
    if not isinstance(data, dict):
        raise BindingError("BINDING_INVALID", "binding 必须是 JSON 对象")
    for key in data:
        if key in FORBIDDEN_STATE_KEYS:
            raise BindingError(
                "BINDING_FORBIDDEN_FIELD",
                f"binding 出现学习状态权威字段（架构违规）: {key}",
            )
    extra = [key for key in data if key not in ALLOWED_KEYS]
    if extra:
        raise BindingError("BINDING_INVALID", f"binding 出现非法字段: {', '.join(extra)}")
    # course_id is optional here: the file lives inside the course directory,
    # which already establishes ownership; the skill may write only the
    # opaque teach-mcp ids (pipeline_id / curriculum_id).
    for key in ALLOWED_KEYS:
        if key in data and not str(data[key]).strip():
            raise BindingError("BINDING_INVALID", f"{key} 不能为空字符串")
