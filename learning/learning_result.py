"""Bridge Result protocol (Return Channel v0.1, Phase 2-3).

One result file per headless task: ``<course>/bridge/result.json``. It is a
ONE-SHOT delivery envelope written by the firefly-learning skill and consumed
by the Firefly result watcher — never a learning-state store. mastery /
misconception / quiz history stay in teach-mcp; any progress figure inside a
result is a display snapshot marked ``source="teach-mcp"``.

Writes are atomic (tmp file → fsync → os.replace) so Firefly can never read
a half-written result.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from learning._storage import BridgeFileError, utc_now_iso
from learning.resource_manager import FORBIDDEN_STATE_KEYS

RESULT_RELATIVE_PATH = Path("bridge") / "result.json"
RESULT_TMP_NAME = "result.json.tmp"
SCHEMA_VERSION = 1

ACTIONS = ("new", "review", "resume", "answer")
STATUSES = ("ok", "waiting_review", "error")
MESSAGE_TYPES = ("lesson", "question", "review", "info", "error")

_REQUIRED = (
    "schema_version",
    "task_id",
    "course_id",
    "action",
    "status",
    "message_type",
    "display_text",
    "created_at",
)
_OPTIONAL = ("question", "opaque_refs", "progress_snapshot")


class ResultError(BridgeFileError):
    pass


def result_path(course_dir: Path | str) -> Path:
    return Path(course_dir) / RESULT_RELATIVE_PATH


def validate_result(data: Any) -> dict:
    """Strict structural validation; raises ResultError on any violation."""
    if not isinstance(data, dict):
        raise ResultError("RESULT_INVALID", "result 必须是 JSON 对象")
    for key in data:
        if key in FORBIDDEN_STATE_KEYS:
            raise ResultError(
                "RESULT_FORBIDDEN_FIELD",
                f"result 出现学习状态权威字段（架构违规）: {key}",
            )
    missing = [k for k in _REQUIRED if k not in data]
    if missing:
        raise ResultError("RESULT_INVALID", f"result 缺少字段: {', '.join(missing)}")
    extra = [k for k in data if k not in _REQUIRED + _OPTIONAL]
    if extra:
        raise ResultError("RESULT_INVALID", f"result 出现未知字段: {', '.join(extra)}")
    if data["schema_version"] != SCHEMA_VERSION:
        raise ResultError(
            "RESULT_VERSION_UNSUPPORTED", f"不支持的 result 版本: {data['schema_version']!r}"
        )
    if data["action"] not in ACTIONS:
        raise ResultError("RESULT_INVALID", f"action 非法: {data['action']!r}")
    if data["status"] not in STATUSES:
        raise ResultError("RESULT_INVALID", f"status 非法: {data['status']!r}")
    if data["message_type"] not in MESSAGE_TYPES:
        raise ResultError("RESULT_INVALID", f"message_type 非法: {data['message_type']!r}")
    for key in ("task_id", "course_id", "display_text"):
        if not str(data[key]).strip():
            raise ResultError("RESULT_INVALID", f"{key} 不能为空")
    if data["message_type"] == "question":
        question = data.get("question")
        if not isinstance(question, dict) or not str(question.get("question_id", "")).strip():
            raise ResultError(
                "RESULT_INVALID",
                "message_type=question 必须携带 question.question_id（teach-mcp 原值）",
            )
    snapshot = data.get("progress_snapshot")
    if snapshot is not None:
        if not isinstance(snapshot, dict) or snapshot.get("source") != "teach-mcp":
            raise ResultError(
                "RESULT_INVALID",
                "progress_snapshot 必须标记 source=teach-mcp（展示快照，非状态）",
            )
    return data


def write_result(course_dir: Path | str, result: dict) -> Path:
    """Validate then atomically publish the result (tmp → fsync → replace)."""
    validate_result(result)
    target = result_path(course_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(RESULT_TMP_NAME)
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)
    return target


def read_result(course_dir: Path | str) -> dict:
    from learning._storage import read_json

    data = read_json(result_path(course_dir))
    return validate_result(data)


def build_result(
    *,
    task_id: str,
    course_id: str,
    action: str,
    status: str,
    message_type: str,
    display_text: str,
    question: dict | None = None,
    session_id: str | None = None,
    progress_snapshot: dict | None = None,
    created_at: str | None = None,
) -> dict:
    """Convenience builder used by the skill protocol examples and tests."""
    result: dict = {
        "schema_version": SCHEMA_VERSION,
        "task_id": task_id,
        "course_id": course_id,
        "action": action,
        "status": status,
        "message_type": message_type,
        "display_text": display_text,
        "created_at": created_at or utc_now_iso(),
    }
    if question is not None:
        result["question"] = question
    if session_id:
        result["opaque_refs"] = {"session_id": session_id}
    if progress_snapshot is not None:
        result["progress_snapshot"] = progress_snapshot
    return validate_result(result)


def display_lines(result: dict) -> list[str]:
    """Human-facing lines derived from a result (teaching text + question).

    Firefly renders exactly these; the chat LLM never rewrites them.
    """
    lines = [str(result["display_text"])]
    question = result.get("question")
    if isinstance(question, dict):
        q_text = str(question.get("text", "")).strip()
        if q_text:
            lines.append(q_text)
        options = question.get("options")
        if isinstance(options, list) and options:
            rendered = []
            for index, option in enumerate(options):
                if isinstance(option, dict):
                    label = option.get("label") or chr(ord("A") + index)
                    body = option.get("text") or option.get("content") or ""
                    rendered.append(f"{label}. {body}".strip())
                else:
                    rendered.append(f"{chr(ord('A') + index)}. {option}")
            lines.append("\n".join(rendered))
    return lines
