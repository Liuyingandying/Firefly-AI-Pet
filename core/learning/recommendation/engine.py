# -*- coding: utf-8 -*-
"""RecommendationEngine — MasterySignal + KnowledgeGraph → 学习推荐（M1.3）。

纯确定性规则引擎（零 LLM/零 UI/零存储）::

    MasterySignal(M1.0)      ──信号──▶  ┐
    KnowledgeGraph(M1.2)     ──知识──▶  ├─▶ LearningRecommendation
                                        ┘

决策优先级（先到先得, 全部安全返回不抛异常）::

    非法输入 / 空 concept → none
    未知 concept         → none（target 回显原概念 id, 便于追踪）
    1. 存在 prerequisite → prerequisite（优先级 1: 先修前置, 教学上先补地基）
    2. 低强度信号        → review（优先级 2: 掌握不足先复习）
    3. 存在关联实验      → simulation（优先级 3: 去实验中观察）
    都不满足             → none（no_recommendation）

"低强度"阈值可注入（默认 0.6, 低于 M0.6 打点的运行置信度 0.7——
即一次正常运行为 0.7 不算弱, 人工降权/多信号衰减后才会触发 review）。
strength 仅与阈值比较, **不做任何换算**。
"""

from __future__ import annotations

import math

from core.learning.knowledge_graph.graph import DEFAULT_NODES, KnowledgeGraph
from core.learning.mastery_adapter.schema import MasterySignal
from core.learning.recommendation.schema import (
    ACTION_NONE,
    ACTION_PREREQUISITE,
    ACTION_REVIEW,
    ACTION_SIMULATION,
    PRIORITY_NONE,
    PRIORITY_PREREQUISITE,
    PRIORITY_REVIEW,
    PRIORITY_SIMULATION,
    LearningRecommendation,
)


class RecommendationEngine:
    """学习推荐引擎（确定性规则, 零数值换算）。"""

    #: 低强度信号阈值（< threshold 视为掌握不足）
    DEFAULT_WEAK_THRESHOLD = 0.6

    def __init__(
        self,
        graph: KnowledgeGraph | None = None,
        *,
        weak_threshold: float = DEFAULT_WEAK_THRESHOLD,
    ):
        self._graph = graph if graph is not None else KnowledgeGraph(DEFAULT_NODES)
        self._weak_threshold = float(weak_threshold)

    def recommend(self, signal: MasterySignal | None) -> LearningRecommendation:
        """为一条掌握度信号生成至多一条推荐; 永不抛异常。"""
        # ---- 非法输入（规则 4 的防御面） ------------------------------------
        if not isinstance(signal, MasterySignal):
            return self._none("", "invalid_input")
        concept_id = signal.concept_id
        if not isinstance(concept_id, str) or not concept_id.strip():
            return self._none(
                concept_id if isinstance(concept_id, str) else "", "empty_concept"
            )
        try:
            strength = float(signal.strength)
        except (TypeError, ValueError):
            return self._none(concept_id, "invalid_strength")
        if not (0.0 <= strength <= 1.0) or strength != strength:
            return self._none(concept_id, "invalid_strength")

        # ---- 规则 4：未知概念安全返回 ---------------------------------------
        node = self._graph.get_concept(concept_id)
        if node is None:
            return self._none(concept_id, "unknown_concept")

        # ---- 规则 2（优先）：存在 prerequisite → 推荐前置概念 ----------------
        prereqs = tuple(p for p in node.prerequisites if isinstance(p, str) and p.strip())
        if prereqs:
            target = prereqs[0]
            target_node = self._graph.get_concept(target)
            label = target_node.name if target_node is not None else target
            experiment = self._first_experiment(target)
            return LearningRecommendation(
                action=ACTION_PREREQUISITE,
                target_concept=target,
                reason=f"先修前置概念「{label}」后再学「{node.name}」",
                related_experiment=experiment,
                priority=PRIORITY_PREREQUISITE,
            )

        # ---- 规则 1：低强度信号 → review -------------------------------------
        if strength < self._weak_threshold:
            return LearningRecommendation(
                action=ACTION_REVIEW,
                target_concept=concept_id,
                reason=(
                    f"掌握强度 {strength:.2f} 低于阈值 {self._weak_threshold:.2f}，"
                    f"建议先复习「{node.name}」"
                ),
                priority=PRIORITY_REVIEW,
            )

        # ---- 规则 3：存在关联实验 → simulation -------------------------------
        experiment = self._first_experiment(concept_id)
        if experiment:
            return LearningRecommendation(
                action=ACTION_SIMULATION,
                target_concept=concept_id,
                reason=f"掌握良好，建议在实验「{experiment}」中观察「{node.name}」",
                related_experiment=experiment,
                priority=PRIORITY_SIMULATION,
            )

        return self._none(concept_id, "no_recommendation")

    def recommend_all(
        self, signals: list[MasterySignal] | tuple[MasterySignal, ...]
    ) -> tuple[LearningRecommendation, ...]:
        """批量推荐（逐条安全返回, 永不抛异常）。"""
        return tuple(self.recommend(signal) for signal in signals or ())

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _first_experiment(self, concept_id: str) -> str:
        experiments = self._graph.get_related_experiments(concept_id)
        return experiments[0] if experiments else ""

    @staticmethod
    def _none(target_concept: str, reason: str) -> LearningRecommendation:
        return LearningRecommendation(
            action=ACTION_NONE,
            target_concept=target_concept,
            reason=reason,
            priority=PRIORITY_NONE,
        )
