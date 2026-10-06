"""Learning Context protocol (Phase 5).

A Learning Context is a one-shot handoff note, atomically (re)written by
Firefly at every launch: *who* this is, *which* course, *what* to do. It is
not a learning database — mastery/progress/misconception fields are schema
errors here.
"""

from __future__ import annotations

from pathlib import Path

from learning._storage import BridgeFileError, atomic_write_json, read_json, utc_now_iso
from learning.course_binding import load_binding
from learning.resource_manager import MANIFEST_NAME, FORBIDDEN_STATE_KEYS

SCHEMA_VERSION = 1
CONTEXT_RELATIVE_PATH = Path("workspace") / ".firefly" / "learning_context.json"
ACTIONS = ("new", "resume", "review", "answer")

_REQUIRED_KEYS = (
    "schema_version",
    "source",
    "learner_id",
    "course_id",
    "manifest_path",
    "action",
    "created_at",
)
_OPTIONAL_OPAQUE_KEYS = ("curriculum_id", "pipeline_id")
_OPTIONAL_KEYS = _OPTIONAL_OPAQUE_KEYS + ("task_id", "delivery_mode")


class ContextError(BridgeFileError):
    pass


def build_learning_context(
    course_dir: Path | str,
    action: str,
    learner_id: str,
    *,
    manifest: dict | None = None,
    now: str | None = None,
) -> dict:
    """Build a schema-v1 context from the course's manifest + binding.

    Opaque binding ids (curriculum_id / pipeline_id) are echoed verbatim when
    the binding file carries them; they are never invented here.
    """
    from learning.resource_manager import ResourceManager

    course_dir = Path(course_dir)
    if action not in ACTIONS:
        raise ContextError(
            "ACTION_INVALID", f"action 必须是 {ACTIONS} 之一，收到: {action!r}"
        )
    learner_id = (learner_id or "").strip()
    if not learner_id:
        raise ContextError("LEARNER_ID_REQUIRED", "learner_id 不能为空")
    if manifest is None:
        manifest = read_json(course_dir / MANIFEST_NAME)
    ResourceManager().validate_manifest(manifest)

    context: dict = {
        "schema_version": SCHEMA_VERSION,
        "source": "firefly",
        "learner_id": learner_id,
        "course_id": manifest["course_id"],
        "manifest_path": str((course_dir / MANIFEST_NAME).resolve()),
        "action": action,
        "created_at": now or utc_now_iso(),
    }
    binding = load_binding(course_dir)
    if binding is not None:
        for key in _OPTIONAL_OPAQUE_KEYS:
            value = binding.get(key)
            if value:
                context[key] = str(value)
    _validate_context(context)
    return context


def build_answer_context(
    course_dir: Path | str,
    learner_id: str,
    *,
    task_id: str,
    manifest: dict | None = None,
    now: str | None = None,
) -> dict:
    """Answer-action context (Return Channel): identical to a normal context
    except action=answer and the Firefly-generated task_id rides along. The
    question_id / student_answer payload travels separately in
    bridge/learning_action.json (see learning_action.schema.json)."""
    context = build_learning_context(
        course_dir, "answer", learner_id, manifest=manifest, now=now
    )
    context["task_id"] = task_id
    _validate_context(context)
    return context


def context_path(course_dir: Path | str) -> Path:
    return Path(course_dir) / CONTEXT_RELATIVE_PATH


def write_learning_context(course_dir: Path | str, context: dict) -> Path:
    """Atomically write the context into the course workspace."""
    _validate_context(context)
    return atomic_write_json(context_path(course_dir), context)


def read_learning_context(path: Path | str) -> dict:
    """Read + validate a context file (used by tests and guard tooling)."""
    data = read_json(Path(path))
    _validate_context(data)
    return data


def _validate_context(context: object) -> None:
    if not isinstance(context, dict):
        raise ContextError("CONTEXT_INVALID", "context 必须是 JSON 对象")
    for key in context:
        if key in FORBIDDEN_STATE_KEYS:
            raise ContextError(
                "CONTEXT_FORBIDDEN_FIELD",
                f"context 出现学习状态权威字段（架构违规）: {key}",
            )
    missing = [key for key in _REQUIRED_KEYS if key not in context]
    if missing:
        raise ContextError("CONTEXT_INVALID", f"context 缺少字段: {', '.join(missing)}")
    extra = [key for key in context if key not in _REQUIRED_KEYS + _OPTIONAL_KEYS]
    if extra:
        raise ContextError("CONTEXT_INVALID", f"context 出现未知字段: {', '.join(extra)}")
    if context["schema_version"] != SCHEMA_VERSION:
        raise ContextError(
            "CONTEXT_VERSION_UNSUPPORTED", f"不支持的 context 版本: {context['schema_version']!r}"
        )
    if context["source"] != "firefly":
        raise ContextError("CONTEXT_INVALID", f"source 必须是 firefly，收到: {context['source']!r}")
    if context["action"] not in ACTIONS:
        raise ContextError("ACTION_INVALID", f"action 非法: {context['action']!r}")
    if not str(context["learner_id"]).strip():
        raise ContextError("CONTEXT_INVALID", "learner_id 不能为空")
    if not str(context["course_id"]).strip():
        raise ContextError("CONTEXT_INVALID", "course_id 不能为空")
    if not str(context["manifest_path"]).strip():
        raise ContextError("CONTEXT_INVALID", "manifest_path 不能为空")
    if "delivery_mode" in context and context["delivery_mode"] not in ("interactive", "embedded"):
        raise ContextError("CONTEXT_INVALID", f"delivery_mode 非法: {context['delivery_mode']!r}")
