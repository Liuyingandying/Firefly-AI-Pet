"""学习任务规划器：画像 + 图谱 -> 带优先级的任务序列。

设计参考 LearningMAP 的加权思想（自研实现）：
- 未见过节点 -> diagnose（先测后教，建立基线）
- 有未解决误解 -> teach（先讲后考，教学门控）
- 已见但掌握度 < 0.6 -> teach/quiz
- 复习到期（可提取性 < 阈值）-> review
- 已稳定节点不进入队列（已掌握不重讲）

误解目标解析：错误描述中提到某个知识点（如"无法理解零极点"），
教学目标即该知识点（零极点），而非误解来源节点本身（传递函数）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..learner.profile import LearnerProfile
from ..learner.state import REVIEW_RETRIEVABILITY_THRESHOLD, StateEngine, now_iso
from .knowledge_graph import KnowledgeGraph

Action = Literal["teach", "diagnose", "quiz", "review", "reinforce"]

# 动作基础优先级（同动作内再按掌握度升序、拓扑序排列）
_ACTION_PRIORITY: dict[Action, float] = {
    "teach": 10.0,
    "diagnose": 8.0,
    "quiz": 6.0,
    "review": 4.0,
    "reinforce": 2.0,
}


@dataclass
class PlanItem:
    node_id: str
    action: Action
    reason: str
    priority: float
    # 实际教学目标（可能不同于误解来源节点，见误解目标解析）
    target_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "action": self.action,
            "reason": self.reason,
            "priority": round(self.priority, 3),
            "target_id": self.target_id,
        }


def summarize_state(profile: LearnerProfile, graph: KnowledgeGraph) -> dict:
    """输出当前学习状态摘要（供宿主展示）。"""
    mastered, weak, unseen, reviewing = [], [], [], []
    for node in graph.nodes.values():
        st = profile.get_state(node.id)
        if st.session_count == 0:
            unseen.append(node.name)
        elif st.mastery >= 0.8 and st.confidence >= 0.6:
            mastered.append(node.name)
        elif st.mastery < 0.6:
            weak.append(node.name)
        elif st.status == "review_due":
            reviewing.append(node.name)
    return {
        "mastered": mastered,
        "weak": weak,
        "unseen": unseen,
        "review_due": reviewing,
    }


class LearningPlanner:
    """把「画像 + 图谱」翻译成带优先级的学习任务序列。"""

    def __init__(self, engine: StateEngine | None = None):
        self.engine = engine or StateEngine()

    # ---- 误解目标解析 ----

    def _teaching_target(self, graph: KnowledgeGraph, node_id: str, error_text: str) -> str:
        """错误描述中提到图谱中存在的知识点时，教学目标是该知识点。"""
        for nid, node in graph.nodes.items():
            if nid != node_id and (node.name in error_text or nid in error_text):
                return nid
        return node_id

    # ---- 规划 ----

    def plan(
        self,
        profile: LearnerProfile,
        graph: KnowledgeGraph,
        now: str | None = None,
    ) -> list[PlanItem]:
        now = now or now_iso()
        items: list[PlanItem] = []

        for node in graph.nodes.values():
            st = profile.get_state(node.id)
            reasons = []

            # 1) 误解门控：未解决误解 -> 先讲后考
            misconceptions = profile.unresolved_misconceptions(node.id)
            if misconceptions:
                desc = misconceptions[0].description
                target = self._teaching_target(graph, node.id, desc)
                reasons.append(f"存在未解决误解: {desc}")
                items.append(
                    PlanItem(
                        node_id=node.id,
                        action="teach",
                        reason="; ".join(reasons),
                        priority=self._priority("teach", st.mastery, graph, node.id),
                        target_id=target,
                    )
                )
                continue

            # 2) 未见过 -> 诊断（先测后教）
            if st.session_count == 0:
                reasons.append("尚未接触，先诊断基线")
                items.append(
                    PlanItem(
                        node_id=node.id,
                        action="diagnose",
                        reason="; ".join(reasons),
                        priority=self._priority("diagnose", st.mastery, graph, node.id),
                        target_id=node.id,
                    )
                )
                continue

            # 3) 复习到期
            if st.status == "review_due" or self._is_review_due(st, now):
                reasons.append("可提取性低于阈值，需要复习")
                items.append(
                    PlanItem(
                        node_id=node.id,
                        action="review",
                        reason="; ".join(reasons),
                        priority=self._priority("review", st.mastery, graph, node.id),
                        target_id=node.id,
                    )
                )
                continue

            # 4) 已见但薄弱 -> 教学/测验
            if st.mastery < 0.6:
                action: Action = "quiz" if 0.4 <= st.mastery < 0.6 else "teach"
                reasons.append(f"掌握度 {st.mastery:.2f}，需{'测验巩固' if action == 'quiz' else '再讲解'}")
                items.append(
                    PlanItem(
                        node_id=node.id,
                        action=action,
                        reason="; ".join(reasons),
                        priority=self._priority(action, st.mastery, graph, node.id),
                        target_id=node.id,
                    )
                )
                continue

            # 5) 接近稳定但未完全掌握 -> 巩固
            if st.mastery < 0.85:
                reasons.append("接近掌握，安排巩固")
                items.append(
                    PlanItem(
                        node_id=node.id,
                        action="reinforce",
                        reason="; ".join(reasons),
                        priority=self._priority("reinforce", st.mastery, graph, node.id),
                        target_id=node.id,
                    )
                )

        # 排序：动作优先级降序，同动作按掌握度升序（最弱的先处理）
        items.sort(key=lambda it: (-it.priority, profile.get_state(it.node_id).mastery))
        return items

    def choose_next(self, items: list[PlanItem]) -> PlanItem | None:
        """取最高优先级任务（规划器对外主入口）。"""
        return items[0] if items else None

    # ---- 内部 ----

    def _priority(self, action: Action, mastery: float, graph: KnowledgeGraph, node_id: str) -> float:
        base = _ACTION_PRIORITY[action]
        # 前置未掌握则略微加权（KST 思想：先补前置）
        prerequisites = graph.prerequisites_of(node_id)
        if any(p for p in prerequisites if mastery < 0.6):
            base += 0.5
        return base

    def _is_review_due(self, st, now: str) -> bool:
        if not st.last_seen_at or st.stability_days <= 0:
            return False
        return self.engine.retrievability(st.last_seen_at, st.stability_days, now) < REVIEW_RETRIEVABILITY_THRESHOLD
