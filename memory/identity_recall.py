"""Narrow, read-only user-name recall from authoritative MemoryRecords.

This is not an intent classifier or a second identity store. The caller owns
the records. Bare legacy migrated preferences are ambiguous: a migration run
tag does not preserve the analyzer's nickname rule, so only an owner-confirmed
``identity_kind`` or an explicit self-name statement can identify them here.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable

from .records import MemoryCategory, MemoryRecord


_QUESTIONS = frozenset({
    "我是谁", "我是谁呀", "我叫什么", "我叫什么名字", "我的名字是什么",
    "你叫我什么", "你平时怎么称呼我", "你平时怎么叫我",
    "还记得怎么叫我吗", "还记得怎么称呼我吗",
})
_PRIORITY = {"name": 0, "preferred_name": 1}
_CATEGORIES = frozenset({MemoryCategory.PREFERENCE, MemoryCategory.USER_FACT})
# A compact literal name, not a sentence, quotation, instruction or list.
_VALUE = r"[\w\u3400-\u9fff·'\-]+(?: [\w\u3400-\u9fff·'\-]+)*"
_BARE_VALUE = re.compile(_VALUE)
_LEGACY_PATTERNS = (
    ("preferred_name", re.compile(rf"(?:我的)?昵称\s*[:：]\s*(?P<value>{_VALUE})")),
    ("preferred_name", re.compile(rf"(?:请)?(?:叫我|称呼我|喊我)\s*(?P<value>{_VALUE})")),
    ("name", re.compile(rf"我叫(?!你|他|她|它)\s*(?P<value>{_VALUE})")),
    ("name", re.compile(rf"我的名字(?:是|叫)\s*(?P<value>{_VALUE})")),
    ("name", re.compile(rf"(?:我的)?(?:名字|姓名)\s*[:：]\s*(?P<value>{_VALUE})")),
)


def is_user_identity_recall(query: str) -> bool:
    """Match only an entire supported question, with optional final punctuation."""
    if not isinstance(query, str):
        return False
    text = query.strip()
    if text.endswith(("?", "？", "。")):
        text = text[:-1]
    return text in _QUESTIONS


def _literal_name(value: str) -> str | None:
    text = value.strip()
    return text if 0 < len(text) <= 40 and _BARE_VALUE.fullmatch(text) else None


def _legacy_identity(content: str) -> tuple[str, str] | None:
    text = content.strip()
    if text.endswith(("。", ".")):
        text = text[:-1]
    if is_user_identity_recall(text):
        return None
    for kind, pattern in _LEGACY_PATTERNS:
        match = pattern.fullmatch(text)
        if match:
            value = _literal_name(match.group("value"))
            if value is not None:
                return kind, value
    return None


@dataclass(frozen=True)
class _Candidate:
    record: MemoryRecord
    kind: str
    value: str


def _candidate(record: MemoryRecord) -> _Candidate | None:
    if (not isinstance(record, MemoryRecord)
            or record.lifecycle_status != "active"
            or record.category not in _CATEGORIES):
        return None
    marked_kind = getattr(record, "identity_kind", None)
    if marked_kind is not None:
        if marked_kind not in _PRIORITY:
            return None
        # The Memory owner already confirmed this slot. Nicknames may contain
        # emoji or punctuation; do not reinterpret or format-gate that fact.
        value = record.content.strip()
        return _Candidate(record, marked_kind, value) if value else None
    legacy = _legacy_identity(record.content)
    return _Candidate(record, *legacy) if legacy else None


def select_user_identity_records(records: Iterable[MemoryRecord]) -> list[MemoryRecord]:
    """Return at most one existing record, without changing its content or state.

    Preferred name takes precedence over a name. Within the same kind, use
    updated_ts then created_ts. Different values at identical timestamps fail
    closed; record IDs and vector scores never break a factual conflict.
    """
    candidates = [candidate for record in records if (candidate := _candidate(record))]
    if not candidates:
        return []
    priority = max(_PRIORITY[c.kind] for c in candidates)
    same_kind = [c for c in candidates if _PRIORITY[c.kind] == priority]
    newest = max((c.record.updated_ts, c.record.created_ts) for c in same_kind)
    latest = [c for c in same_kind if (c.record.updated_ts, c.record.created_ts) == newest]
    if len({c.value for c in latest}) != 1:
        return []
    # Equal-value ties express the same fact; keep the caller's first original
    # record rather than inventing an ID-based notion of truth.
    return [latest[0].record]


__all__ = ["is_user_identity_recall", "select_user_identity_records"]
