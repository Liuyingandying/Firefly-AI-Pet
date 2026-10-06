# -*- coding: utf-8 -*-
"""LearningMemoryWriter — 学习事实写回既有 Memory（M4.7）。

单一收口: 已注入的 ``SuggestionService.suggest_memory``（待确认，不建新库）。
词表对齐（实测）: MemoryCategory 只有 relationship/shared_experience/
emotion/preference/user_fact/project——**"episodic"/"learning_event" 均非
合法 category**, 会抛 ValueError; 故 category 固定 ``user_fact``（对用户
的学习事实语义正确）, "learning_event" 分类标记放 ``metadata.kind``。

失败结构化返回（ok=False + error）, **不阻断循环**。
"""

from __future__ import annotations

from datetime import datetime, timezone

from core.learning.skill.loop.schema import MemoryWriteRecord


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class LearningMemoryWriter:
    """学习观察 → 既有待确认队列；用户接受后才能成为长期事实。"""

    def __init__(self, *, memory_manager=None):
        self._memory_manager = memory_manager   # 未注入时拒绝写入，不另建 owner

    def write_learning_event(
        self,
        *,
        concept_id: str,
        content: str,
        outcome: str,
        extra: dict | None = None,
    ) -> MemoryWriteRecord:
        """写入一条学习事实; 失败返回 ok=False, 永不抛异常。"""
        manager = self._memory_manager
        if manager is None:
            return MemoryWriteRecord(ok=False, content=content,
                                     error="pending suggestion sink is not configured")
        timestamp = _now_iso()
        metadata = {
            "category": "user_fact",
            "kind": "learning_event",
            "concept_id": concept_id,
            "outcome": outcome,
            "timestamp": timestamp,
            **(extra or {}),
        }
        try:
            result = manager.suggest_memory(content, metadata)
        except Exception as exc:  # noqa: BLE001 - 失败不阻断循环
            return MemoryWriteRecord(ok=False, content=content, error=str(exc))
        memory_id = ""
        results = result.get("results", []) if isinstance(result, dict) else []
        if results and isinstance(results[0], dict):
            memory_id = str(results[0].get("id", ""))
        return MemoryWriteRecord(
            ok=bool(memory_id), memory_id=memory_id, content=content,
            error="" if memory_id else "suggestion was not accepted by pending sink",
        )
