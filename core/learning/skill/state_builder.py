# -*- coding: utf-8 -*-
"""LearnerStateBuilder — Memory + LearningStore + KnowledgeGraph 只读聚合（M4.6）。

聚合规则（任务书阶段 1）:
1. 无任何学习记录 → ``cold_start=True``（runtime 走 cold_start_probe, 不问模型）;
2. 有 mastery → 读 ``store.get_concept(cid).mastery_level``（归一 /5）;
3. 有 review_items → 到期（pending 且 due_at ≤ now）填 ``review_due``;
4. 有失败 assessment（score < 0.5）或低掌握（≤1）→ 填 ``weak_concepts``。

所有来源**独立降级**（异常/缺来源 → 对应字段为空, 永不阻断 Agent）;
全部只读——不写 Store、不写 Memory、不改图。
"""

from __future__ import annotations

from datetime import datetime, timezone

from core.learning.knowledge_graph import DEFAULT_NODES, KnowledgeGraph
from core.learning.skill.schema import LearnerState


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _due(due_at: str, now: str) -> bool:
    """ISO 字符串比较（同格式下字典序即时间序）; 解析失败视为未到期。"""
    try:
        return str(due_at) <= now and bool(str(due_at).strip())
    except (TypeError, ValueError):
        return False


class LearnerStateBuilder:
    """学习者状态构建器（只读聚合, 全来源可注入/可缺省）。"""

    def __init__(
        self,
        *,
        graph: KnowledgeGraph | None = None,
        store=None,                            # M1A LearningStore（只读; None=跳过）
        course_id: str | None = None,          # 课程范围（None=扫描全部课程）
        memory_query=None,                     # callable(query) -> list[dict]（None=跳过）
        user_id: str = "default",
    ):
        self._graph = graph if graph is not None else KnowledgeGraph(DEFAULT_NODES)
        self._store = store
        self._course_id = course_id
        self._memory_query = memory_query
        self._user_id = user_id

    def build(self, *, learning_goal: str = "") -> LearnerState:
        """聚合学习者状态; 来源故障静默降级, 永不抛异常。"""
        warnings: list[str] = []

        # ---- 1. 概念词表（KnowledgeGraph） -----------------------------------
        try:
            concepts = list(self._graph.list_concepts())
        except Exception:  # noqa: BLE001
            concepts = []
            warnings.append("knowledge graph unavailable")

        # ---- 2. LearningStore: mastery / weak / recent_errors / review / 会话 --
        concept_mastery: dict[str, float] = {}
        weak: list[dict] = []
        weak_seen: set[str] = set()
        recent_errors: list[dict] = []
        review_due: list[str] = []
        review_seen: set[str] = set()
        unfinished: list[str] = []
        unfinished_seen: set[str] = set()
        store = self._store

        if store is not None and concepts:
            now = _now_iso()
            for cid in concepts:
                try:
                    concept = store.get_concept(cid)
                except Exception:  # noqa: BLE001
                    concept = None
                if concept is None:
                    continue                    # seed 概念无 Store 行 → cold start 语义
                level = float(getattr(concept, "mastery_level", 0) or 0)
                concept_mastery[cid] = round(level / 5.0, 3)

                try:
                    assessments = store.list_assessments(cid) or []
                except Exception:  # noqa: BLE001
                    assessments = []
                failed = [a for a in assessments
                          if float(getattr(a, "score", 1.0) or 0.0) < 0.5]

                # 弱因优先级: 有答错记录 → wrong_answer, 否则低掌握 → low_mastery
                if failed and cid not in weak_seen:
                    weak_seen.add(cid)
                    weak.append({"id": cid, "reason": "wrong_answer"})
                elif level <= 1 and cid not in weak_seen:
                    weak_seen.add(cid)
                    weak.append({"id": cid, "reason": "low_mastery"})

                latest_fail = failed[-1] if failed else None
                if latest_fail is not None:
                    recent_errors.append({
                        "concept_id": cid,
                        "score": float(getattr(latest_fail, "score", 0.0) or 0.0),
                        "at": str(getattr(latest_fail, "created_at", "") or ""),
                    })

            try:
                for item in store.list_review_items() or []:
                    cid = getattr(item, "concept_id", "")
                    if (str(getattr(item, "status", "")) == "pending"
                            and _due(getattr(item, "due_at", ""), now)
                            and cid not in review_seen):
                        review_seen.add(cid)
                        review_due.append(cid)
            except Exception:  # noqa: BLE001
                warnings.append("review items unavailable")

            try:
                for course in store.list_courses() or []:
                    course_id = getattr(course, "id", "")
                    session = store.get_active_session(course_id)
                    if session is not None:
                        sid = getattr(session, "id", "")
                        if sid and sid not in unfinished_seen:
                            unfinished_seen.add(sid)
                            unfinished.append(sid)
            except Exception:  # noqa: BLE001
                warnings.append("sessions unavailable")

        # ---- 3. Memory: 项目 / 目标（经注入的查询函数） ------------------------
        active_projects: list[str] = []
        learning_goals: list[str] = []
        if self._memory_query is not None:
            for query, bucket in (("project", active_projects), ("goal", learning_goals)):
                try:
                    items = self._memory_query(query) or []
                except Exception:  # noqa: BLE001
                    items = []
                for item in items:
                    content = str(item.get("content", "") if isinstance(item, dict) else item).strip()
                    if content and content not in bucket:
                        bucket.append(content)

        cold_start = not (
            concept_mastery or weak or recent_errors or review_due
            or unfinished or active_projects or learning_goals
        )
        if str(learning_goal or "").strip():
            goal = str(learning_goal).strip()
            if goal not in learning_goals:
                learning_goals.insert(0, goal)

        return LearnerState(
            user_id=self._user_id,
            learning_goals=tuple(learning_goals),
            active_projects=tuple(active_projects),
            concept_mastery=concept_mastery,
            weak_concepts=tuple(weak),
            recent_errors=tuple(recent_errors),
            review_due=tuple(review_due),
            unfinished_sessions=tuple(unfinished),
            cold_start=cold_start,
        )
