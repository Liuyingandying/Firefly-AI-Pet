# -*- coding: utf-8 -*-
"""TutorChatRouter — 聊天输入的 Tutor/Skill 路由与渲染（M4.5/M4.10 宿主接线）。

职责（宿主接线层, 不含 Tutor 核心逻辑）::

    用户文本
      ├─ tutor 会话 waiting_answer    → submit_answer → 渲染评价/建议
      ├─ 学习意图（QUIZ/学习/教我）   → 构造计划 → executor.start → 渲染题目
      ├─ 诊断查询（薄弱/学习状态）    → LearnerProfile → 画像摘要
      └─ 其余                         → passthrough（交给既有 learning/模型管线）

本模块刻意**不导入 PySide6**——纯路由/渲染逻辑, 便于无头测试;
console 只做薄委托。
"""

from __future__ import annotations

from dataclasses import dataclass

from core.learning.knowledge_graph import DEFAULT_NODES, KnowledgeGraph
from core.learning.intents import (LearningIntent, parse_learning_intent,
                                  is_teaching_request, is_learning_diagnostic)
from core.learning.profile import LearnerProfile, LearnerProfileBuilder
from core.learning.session.schema import STEPS_LESSON, LearningSessionPlan
from core.learning.tutor import TutorSessionExecutor, TutorSessionState
from core.learning.tutor.schema import STATUS_COMPLETED, STATUS_FAILED, STATUS_WAITING_ANSWER

# ---------------------------------------------------------------------------
# 概念关键词 → concept_id（宿主路由用封闭映射; 与知识图种子对齐）
# ---------------------------------------------------------------------------

_CONCEPT_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("te极化", "te 极化", "te模式", "te 模式"), "em-te-polarization"),
    (("tm极化", "tm 极化", "tm模式", "tm 模式"), "em-tm-polarization"),
    (("均匀平面波", "平面波", "plane wave"), "em-uniform-plane-wave"),
    (("麦克斯韦", "maxwell", "maxwell方程"), "em-uniform-plane-wave"),
    (("电磁波", "电磁场"), "em-uniform-plane-wave"),
)
def _target_concept(text: str) -> str | None:
    lowered = text.casefold()
    for keywords, concept_id in _CONCEPT_KEYWORDS:
        if any(kw in lowered for kw in keywords):
            return concept_id
    return None


def _related_experiment(graph: KnowledgeGraph, concept_id: str) -> str:
    node = graph.get_concept(concept_id)
    if node is not None and node.related_experiments:
        return node.related_experiments[0]
    return ""


# ---------------------------------------------------------------------------
# 路由决策
# ---------------------------------------------------------------------------

ACTION_RENDER = "render"            # tutor 专属回合（宿主渲染气泡后 return）
ACTION_PASSTHROUGH = "passthrough"  # 非 tutor 回合（进入既有管线）


@dataclass(frozen=True)
class TutorRouteDecision:
    action: str                                    # ACTION_RENDER / ACTION_PASSTHROUGH
    state: TutorSessionState | None = None
    kind: str = ""                                 # tutor_started / answer_submitted / diagnostic / passthrough
    bubble_text: str = ""                          # kind=diagnostic 时直接提供的回复文本


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class TutorChatRouter:
    """聊天文本 → Tutor 会话动作（宿主持有, 进程内会话句柄）。

    M4.10 扩展: 支持学习请求（"我想学习X"）与诊断查询（"薄弱/学习状态"）;
    可选注入 LearnerProfileBuilder 用于画像增强回答。
    """

    def __init__(
        self,
        executor: TutorSessionExecutor,
        *,
        graph: KnowledgeGraph | None = None,
        profile_builder: LearnerProfileBuilder | None = None,
    ):
        self._executor = executor
        self._graph = graph if graph is not None else KnowledgeGraph(DEFAULT_NODES)
        self._profile_builder = profile_builder
        self.waiting_session_id: str | None = None   # 当前等待回答的会话

    def _render_diagnostic(self) -> str:
        """从 LearnerProfile 渲染学习诊断摘要（画像回答模式）。"""
        if self._profile_builder is None:
            return (
                "我目前记录到的学习证据还不够多。\n"
                "如果你愿意，我可以先用两三个问题帮你快速诊断。"
            )
        profile = self._profile_builder.build()
        return render_profile_summary(profile)

    def route(self, text: str) -> TutorRouteDecision:
        """路由一条聊天文本; 永不抛异常（失败路由为 render failed 气泡）。"""
        text = str(text or "").strip()

        # ---- 1. waiting_answer: 用户输入即作答（最高优先级） -------------------
        if self.waiting_session_id:
            state = self._executor.get_session(self.waiting_session_id)
            if state is not None and state.status == STATUS_WAITING_ANSWER:
                new_state = self._executor.submit_answer(state.session_id, text)
                if new_state.status in (STATUS_COMPLETED, STATUS_FAILED):
                    self.waiting_session_id = None    # 会话收尾, 释放路由
                return TutorRouteDecision(
                    action=ACTION_RENDER, state=new_state, kind="answer_submitted",
                )
            self.waiting_session_id = None            # 会话已消失/收尾 → 继续意图路由

        # ---- 2. 诊断查询（画像回答模式） --------------------------------------
        if is_learning_diagnostic(text):
            bubble = self._render_diagnostic()
            return TutorRouteDecision(
                action=ACTION_RENDER, state=None, kind="diagnostic",
                bubble_text=bubble,
            )

        # ---- 3. 学习意图 → 启动 Tutor 会话 --------------------------------------
        parsed = parse_learning_intent(text)
        is_learn_kw = is_teaching_request(text)
        if parsed.intent is LearningIntent.QUIZ_REQUEST or is_learn_kw:
            concept_id = _target_concept(text)
            if concept_id is None:
                return TutorRouteDecision(action=ACTION_PASSTHROUGH)
            plan = LearningSessionPlan(
                session_type="lesson",
                target_concept=concept_id,
                related_experiment=_related_experiment(self._graph, concept_id),
                steps=STEPS_LESSON,
            )
            state = self._executor.start(plan)
            if state.status == STATUS_WAITING_ANSWER:
                self.waiting_session_id = state.session_id
            return TutorRouteDecision(
                action=ACTION_RENDER, state=state, kind="tutor_started",
            )

        # ---- 3. 非 tutor 回合 ---------------------------------------------------
        return TutorRouteDecision(action=ACTION_PASSTHROUGH)


# ---------------------------------------------------------------------------
# 渲染（TutorSessionState → 聊天气泡文本; 纯函数）
# ---------------------------------------------------------------------------

def render_tutor_state(state: TutorSessionState) -> str:
    """状态 → 气泡文本（waiting/completed/failed 三形态）。"""
    if state.status == STATUS_WAITING_ANSWER and state.question is not None:
        lines = ["我们先回答一个问题：", state.question.question_text]
        if state.related_experiment:
            lines.append(f"（可配合实验「{state.related_experiment}」观察）")
        return "\n".join(lines)

    if state.status == STATUS_COMPLETED:
        if state.evaluation is None:
            return "（会话已结束，但没有生成评价。）"
        verdict = "✅ 正确" if state.evaluation.correct else "❌ 错误"
        lines = [
            "回答评价：",
            verdict,
            f"类型：{state.evaluation.error_type}",
            f"得分：{state.evaluation.score}",
            f"反馈：{state.evaluation.feedback}",
        ]
        if state.evidence is not None:
            lines.append(
                f"已生成学习证据：{state.evidence.kind}"
                f"（confidence={state.evidence.confidence}）"
            )
        else:
            reason = "错误回答不产生掌握度证据"
            for w in state.warnings:
                if "no mastery evidence" in w:
                    reason = w.split(": ", 1)[-1]
                    break
            lines.append(f"本次回答不产生掌握度证据（{reason}）")
        lines.append(f"下一步建议：{_next_suggestion(state)}")
        return "\n".join(lines)

    if state.status == STATUS_FAILED:
        lines = ["Tutor 会话失败："]
        lines.extend(f"- {w}" for w in state.warnings)
        return "\n".join(lines)

    return "（Tutor 会话状态未知。）"


def render_profile_summary(profile) -> str:
    """LearnerProfile → 人读画像摘要（诊断查询回答）。"""
    k = profile.knowledge
    b = profile.learning_behavior
    a = profile.activity
    p = profile.projects
    lines: list[str] = []
    has_content = False

    if k.strong_concepts:
        lines.append(f"✅ 已掌握: {', '.join(k.strong_concepts)}")
        has_content = True
    if k.weak_concepts:
        lines.append(f"⚠ 薄弱概念: {', '.join(k.weak_concepts)}")
        has_content = True
    if a.review_due:
        lines.append(f"📅 到期复习: {', '.join(a.review_due)}")
        has_content = True
    if b.common_error_patterns:
        lines.append(f"📌 常见错误: {'; '.join(b.common_error_patterns)}")
        has_content = True
    if p.active_projects:
        lines.append(f"🔬 活跃项目: {', '.join(p.active_projects)}")
        has_content = True

    if not has_content:
        return (
            "我目前记录到的学习证据还不够多。\n"
            "如果你愿意，我可以先用两三个问题帮你快速诊断。"
        )
    lines.append("\n需要我针对薄弱概念出题检测，还是深入讲解某个概念？")
    return "\n".join(lines)


def _next_suggestion(state: TutorSessionState) -> str:
    graph = KnowledgeGraph(DEFAULT_NODES)
    path = graph.find_learning_path(state.concept_id)
    if len(path) >= 2:
        return f"沿学习路径继续：{' → '.join(path)}"
    if state.evaluation is not None and state.evaluation.correct:
        return "掌握良好，可以尝试更高难度的题目或运行仿真观察波形。"
    return "重新复习该概念后，再次作答巩固。"
