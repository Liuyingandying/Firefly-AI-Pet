# -*- coding: utf-8 -*-
"""LearnerProfileBuilder — Memory + LearningStore + KG 只读聚合（M4.9 阶段 2）。

构建规则:
- KnowledgeGraph: list_concepts → 全量概念词表 + display_name
- LearningStore: get_concept(mastery) + list_assessments(错误) +
  list_review_items(到期) + list_courses + get_active_session
- Memory: search_memory("learning_event"/"preference"/"project") → 偏好/项目

任何来源失败 → 对应字段为空 + 继续（永不阻断）; 全部只读零写入。
"""

from __future__ import annotations

from datetime import datetime, timezone

from core.learning.knowledge_graph import DEFAULT_NODES, KnowledgeGraph
from core.learning.profile.schema import (
    ActivitySnapshot,
    KnowledgeSnapshot,
    LearnerProfile,
    LearningBehavior,
    ProjectSnapshot,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso_min(due_at: str, now: str) -> bool:
    try:
        return str(due_at).strip() <= now and bool(str(due_at).strip())
    except (TypeError, ValueError):
        return False


class LearnerProfileBuilder:
    """学习者画像构建器（只读聚合, 全来源可注入, 永不抛异常）。"""

    def __init__(
        self,
        *,
        graph: KnowledgeGraph | None = None,
        store=None,                            # LearningStore（只读）
        memory_query=None,                     # callable(query) -> list[dict]
        user_id: str = "default",
    ):
        self._graph = graph if graph is not None else KnowledgeGraph()
        self._store = store
        self._memory_query = memory_query
        self._user_id = user_id

    def build(self, user_id: str | None = None) -> LearnerProfile:
        """聚合学习者画像; 来源故障静默降级, 永不抛异常。"""
        uid = user_id or self._user_id
        now = _now_iso()
        source_summary: list[str] = []

        # ---- 1. 知识掌握（KG + Store） ------------------------------------------
        concept_mastery: dict[str, float] = {}
        strong: list[str] = []
        weak: list[str] = []
        last_studied: list[str] = []
        try:
            concepts = self._graph.list_concepts()
            source_summary.append(f"knowledge_graph({len(concepts)} concepts)")
        except Exception:  # noqa: BLE001
            concepts = []
            source_summary.append("knowledge_graph(unavailable)")

        for cid in concepts:
            try:
                concept = self._store.get_concept(cid)
            except Exception:  # noqa: BLE001
                concept = None
            if concept is None:
                continue
            level = float(getattr(concept, "mastery_level", 0) or 0)
            normalized = round(level / 5.0, 3)
            concept_mastery[cid] = normalized
            if normalized >= 0.6:
                strong.append(cid)
            elif normalized < 0.3:
                weak.append(cid)
            studied = getattr(concept, "last_studied_at", None)
            if studied:
                last_studied.append(cid)

        knowledge = KnowledgeSnapshot(
            concept_mastery=concept_mastery,
            strong_concepts=tuple(strong),
            weak_concepts=tuple(weak),
        )
        source_summary.append(f"store(mastery: {len(concept_mastery)} concepts)")

        # ---- 2. Memory: 偏好 / 项目 / 学习事件 -----------------------------------
        preferences: list[str] = []
        projects: list[str] = []
        research_topics: list[str] = []
        learning_events: list[str] = []
        style = ""
        if self._memory_query is not None:
            for query, target in (
                ("preference", preferences),
                ("project", projects),
                ("learning_event", learning_events),
            ):
                try:
                    items = self._memory_query(query) or []
                except Exception:  # noqa: BLE001
                    items = []
                for item in items:
                    content = ""
                    if isinstance(item, dict):
                        content = str(item.get("memory", "") or item.get("content", "")).strip()
                    elif isinstance(item, str):
                        content = item.strip()
                    if content and content not in target:
                        target.append(content)
            if preferences:
                style = "偏好实践/操作" if any("实验" in p for p in preferences) else "偏好理论讲解"
            source_summary.append(f"memory({len(projects)+len(preferences)+len(learning_events)} facts)")
        else:
            source_summary.append("memory(not injected)")

        # ---- 3. 复习到期（Store review_items） ------------------------------------
        review_due: list[str] = []
        try:
            for item in self._store.list_review_items() or []:
                cid = getattr(item, "concept_id", "")
                if (str(getattr(item, "status", "")) == "pending"
                        and _iso_min(getattr(item, "due_at", ""), now)
                        and cid not in review_due):
                    review_due.append(cid)
        except Exception:  # noqa: BLE001
            pass

        behavior = LearningBehavior(
            learning_preferences=tuple(preferences),
            preferred_explanation_style=style,
        )
        activity = ActivitySnapshot(
            recent_learning_events=tuple(learning_events),
            last_studied_concepts=tuple(last_studied),
            review_due=tuple(review_due),
        )
        projects = ProjectSnapshot(active_projects=tuple(projects), research_topics=())

        return LearnerProfile(
            user_id=uid,
            knowledge=knowledge,
            learning_behavior=behavior,
            activity=activity,
            projects=projects,
            metadata={"generated_at": now, "source_summary": "; ".join(source_summary)},
        )
