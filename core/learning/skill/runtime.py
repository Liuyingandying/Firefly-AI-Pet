# -*- coding: utf-8 -*-
"""LearningSkillRuntime — 主动学习 Skill 执行器（M4.6）。

流程（任务书目标形态）::

    用户输入
      ↓ LearnerStateBuilder（Memory+Store+KG 只读聚合）
    LearnerState（cold_start → 直接 cold_start_probe, 不问模型）
      ↓ skill context（prompt + 状态摘要）
    TJULLM（model.generate）
      ↓ SkillDecision JSON
    SkillDecisionValidator（action 白名单/概念词表/置信度）
      ↓ 路由映射
    既有能力（QuestionGenerator / ToolRegistry 四工具 / Review）
      ↓
    SkillRunResult（markdown 回复 + 台账, 永不抛异常）

安全: TJULLM 只能**选择**白名单动作; 所有 registry 类工具经 M2.7
ToolRegistry; quiz/probe 题面脱敏（expected_answer/evaluation_rule 不出
服务端）; Memory/Store 零写入。
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from core.learning.agent.context import ContextBuilder
from core.learning.question import QuestionGenerator
from core.learning.skill.decision import SkillDecisionValidator
from core.learning.skill.prompt import build_skill_messages
from core.learning.skill.schema import (
    LearningAction,
    LearnerState,
    SkillDecision,
    SkillRunResult,
)
from core.learning.skill.state_builder import LearnerStateBuilder
from core.learning.tools.schema import ToolResult
from core.learning.tools.tool_registry import ToolRegistry


def _extract_json(content: str) -> dict | None:
    """从模型文本提取 JSON 对象（容忍 ```json 围栏/前后缀）。"""
    text = str(content or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start:end + 1])
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None


class LearningSkillRuntime:
    """主动学习 Skill 运行时（模型决策 + 白名单路由 + 题面脱敏）。"""

    def __init__(
        self,
        *,
        model=None,
        state_builder: LearnerStateBuilder | None = None,
        registry: ToolRegistry | None = None,
        question_generator: QuestionGenerator | None = None,
        graph: KnowledgeGraph | None = None,
        context_builder: ContextBuilder | None = None,
    ):
        from core.learning.knowledge_graph import KnowledgeGraph as _KG

        self._graph = graph if graph is not None else _KG()
        self._model = model
        self._state_builder = state_builder if state_builder is not None else LearnerStateBuilder(
            graph=self._graph
        )
        self._registry = registry if registry is not None else _default_registry()
        self._questions = question_generator if question_generator is not None else QuestionGenerator(
            graph=self._graph
        )
        self._context = context_builder if context_builder is not None else ContextBuilder(
            graph=self._graph
        )
        self._validator = SkillDecisionValidator()

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------

    def start_learning(
        self,
        user_query: str = "",
        *,
        learning_goal: str = "",
    ) -> "SkillRunResult":
        """一次主动学习决策循环; 永不抛异常。"""
        tool_results: list[dict] = []
        warnings: list[str] = []

        try:
            state = self._state_builder.build(
                learning_goal=learning_goal or user_query
            )

            # ---- 1. 决策（模型或规则回退） -------------------------------------
            decision: SkillDecision | None = None
            if not state.cold_start and self._model is not None:
                decision, warnings = self._decide_with_model(state, user_query, warnings)
            if decision is None:
                decision, fallback_warning = self._rule_decision(state)
                if fallback_warning:
                    warnings.append(fallback_warning)

            # ---- 2. 路由到既有能力 ----------------------------------------------
            return self._route(decision, state, tool_results, warnings)
        except Exception as exc:  # noqa: BLE001 - 错误即结果
            return SkillRunResult(
                success=False,
                warnings=tuple(warnings),
                error=f"execution_failed: {type(exc).__name__}: {exc}",
            )

    # ------------------------------------------------------------------
    # 决策
    # ------------------------------------------------------------------

    def _decide_with_model(self, state: LearnerState, user_query: str, warnings: list[str]):
        """调模型获取 SkillDecision; 解析/校验失败 → (None, warnings)。"""
        grounding = self._context.render_grounding().text
        state_block = self._render_state(state)
        messages = build_skill_messages(
            grounding_text=grounding,
            learner_state_text=state_block,
            user_query=user_query,
        )
        request = _model_request(messages)
        response = self._model.generate(request)
        payload = _extract_json(response.content)
        if payload is None:
            warnings.append("model returned no parsable decision JSON; fell back to rules")
            return None, warnings

        check = self._validator.validate(
                payload, available_concepts=self._graph.list_concepts(),
            )
        if not check.ok or check.decision is None:
            warnings.append(f"model decision rejected: {check.error}")
            return None, warnings
        return check.decision, warnings

    def _rule_decision(self, state: LearnerState) -> tuple[SkillDecision, str | None]:
        """规则回退决策（无模型/模型被拒时）。"""
        if state.cold_start:
            return (
                SkillDecision(
                    action=LearningAction.COLD_START_PROBE.value,
                    target_concept=self._root_concept(),
                    reason="cold_start: 无学习记录, 用入门概念探底",
                    confidence=1.0,
                ),
                None,
            )
        if state.review_due:
            return (
                SkillDecision(
                    action=LearningAction.REVIEW.value,
                    target_concept=state.review_due[0],
                    reason="存在到期复习项",
                    confidence=0.9,
                ),
                None,
            )
        if state.weak_concepts:
            target = state.weak_concepts[0].get("id", "") or self._root_concept()
            return (
                SkillDecision(
                    action=LearningAction.QUIZ.value,
                    target_concept=target,
                    reason="存在薄弱概念, 先出题检测",
                    confidence=0.8,
                ),
                None,
            )
        if state.unfinished_sessions:
            return (
                SkillDecision(
                    action=LearningAction.CONTINUE_PROJECT.value,
                    target_concept=self._root_concept(),
                    reason="存在未完成会话",
                    confidence=0.7,
                ),
                None,
            )
        return (
            SkillDecision(
                action=LearningAction.PROBE.value,
                target_concept=self._root_concept(),
                reason="无明确信号, 先探底确认理解",
                confidence=0.6,
            ),
            None,
        )

    # ------------------------------------------------------------------
    # 路由（action → 既有能力）
    # ------------------------------------------------------------------

    def _route(
        self,
        decision: SkillDecision,
        state: LearnerState,
        tool_results: list[dict],
        warnings: list[str],
    ) -> "SkillRunResult":
        action = decision.action
        target = decision.target_concept

        if action in (LearningAction.PROBE.value, LearningAction.QUIZ.value,
                      LearningAction.COLD_START_PROBE.value):
            return self._serve_question(decision, state, tool_results, warnings)

        if action in (LearningAction.TEACH.value,
                      LearningAction.CONTINUE_PROJECT.value):
            tool_result = self._registry.execute(
                "generate_learning_plan", {"concept_id": target}
            )
            tool_results.append(self._tool_record("generate_learning_plan", tool_result))
            if not tool_result.success:
                return self._failed(decision, state, tool_results, warnings, tool_result.error)
            body = json.dumps(tool_result.result, ensure_ascii=False, indent=2)
            return SkillRunResult(
                success=True,
                decision=decision,
                state=state,
                tool_results=tuple(tool_results),
                response=(
                    f"## 学习计划（{target}）\n\n```json\n{body}\n```\n\n"
                    f"决策理由：{decision.reason}"
                ),
                warnings=tuple(warnings),
            )

        if action in (LearningAction.RESEARCH.value, LearningAction.REVIEW.value):
            tool_result = self._registry.execute("query_concept", {"concept_id": target})
            tool_results.append(self._tool_record("query_concept", tool_result))
            if not tool_result.success:
                return self._failed(decision, state, tool_results, warnings, tool_result.error)
            concept = tool_result.result.get("concept", {})
            if action == LearningAction.REVIEW.value:
                # 复习 = 到期概念的复习题（scheduler 写路径不经 Skill, 见报告声明）
                question = self._questions.generate(target)
                q = question.questions[0] if question.questions else None
                q_text = q.question_text if q is not None else "（暂无复习题, 请口头复述该概念要点）"
                return SkillRunResult(
                    success=True,
                    decision=decision,
                    state=state,
                    tool_results=tuple(tool_results),
                    response=(
                        f"## 复习：{concept.get('name', target)}\n\n"
                        f"{q_text}\n\n（回答后可提交判卷; 到期项：{len(state.review_due)} 个）\n\n"
                        f"决策理由：{decision.reason}"
                    ),
                    warnings=tuple(warnings),
                )
            path = tool_result.result.get("learning_path", []) or []
            return SkillRunResult(
                success=True,
                decision=decision,
                state=state,
                tool_results=tuple(tool_results),
                response=(
                    f"## 研究资料：{concept.get('name', target)}\n\n"
                    f"- 章节：{concept.get('chapter', '—')}\n"
                    f"- 学习路径：{' → '.join(path) or '—'}\n\n"
                    f"决策理由：{decision.reason}"
                ),
                warnings=tuple(warnings),
            )

        if action == LearningAction.EXPERIMENT.value:
            experiment_id = self._related_experiment(target)
            if not experiment_id:
                warnings.append(f"概念 {target!r} 无关联实验, 回退为概念查询")
                tool_result = self._registry.execute("query_concept", {"concept_id": target})
                tool_results.append(self._tool_record("query_concept", tool_result))
                if not tool_result.success:
                    return self._failed(decision, state, tool_results, warnings, tool_result.error)
                return SkillRunResult(
                    success=True, decision=decision, state=state,
                    tool_results=tuple(tool_results),
                    response=f"（概念 {target} 无关联实验, 已回退为概念查询）",
                    warnings=tuple(warnings),
                )
            tool_result = self._registry.execute(
                "run_experiment",
                {"experiment_id": experiment_id},
            )
            tool_results.append(self._tool_record("run_experiment", tool_result))
            if not tool_result.success:
                return self._failed(decision, state, tool_results, warnings, tool_result.error)
            artifacts = tool_result.result.get("artifacts", {})
            return SkillRunResult(
                success=True,
                decision=decision,
                state=state,
                tool_results=tuple(tool_results),
                response=(
                    f"## 仿真实验：{experiment_id}\n\n"
                    f"- 状态：{tool_result.result.get('status', '—')}\n"
                    f"- 电场分布图：{artifacts.get('png', '—')}\n"
                    f"- 传播动画：{artifacts.get('gif', '—')}\n\n"
                    f"决策理由：{decision.reason}"
                ),
                warnings=tuple(warnings),
            )

        # 词表外（理论不可达——validator 已拦）: 防御性回退
        warnings.append(f"unknown action {action!r} fell back to probe")
        fallback = dataclasses.replace(
            decision, action=LearningAction.PROBE.value,
            target_concept=target,
        )
        return self._route(fallback, state, tool_results, warnings)

    def _serve_question(
        self,
        decision: SkillDecision,
        state: LearnerState,
        tool_results: list[dict],
        warnings: list[str],
    ) -> "SkillRunResult":
        """probe/quiz/cold_start: 出题（题面脱敏——答案不出服务端）。"""
        target = decision.target_concept or self._root_concept()
        question = self._questions.generate(target)
        if not question.success or not question.questions:
            fallback = SkillDecision(
                action=LearningAction.PROBE.value,
                target_concept=target,
                reason=f"question fallback: {question.error}",
            )
            return self._route(fallback, state, tool_results, warnings)

        q = question.questions[0]
        prefix = {
            LearningAction.QUIZ.value: "测验",
            LearningAction.COLD_START_PROBE.value: "探底",
        }.get(decision.action, "检测")
        return SkillRunResult(
            success=True,
            decision=decision,
            state=state,
            tool_results=tuple(tool_results),
            response=(
                f"## {prefix}：{decision.target_concept}\n\n"
                f"**{q.question_text}**\n\n"
                f"（请直接回答; 回答后会自动判卷并更新学习状态）\n\n"
                f"决策理由：{decision.reason}"
            ),
            warnings=tuple(warnings),
            served_question=q,
        )

    def _related_experiment(self, concept_id: str) -> str:
        node = self._graph.get_concept(concept_id)
        if node is not None and node.related_experiments:
            return node.related_experiments[0]
        return ""

    def _root_concept(self) -> str:
        """冷启动目标: 第一个无前置的概念（入门概念）。"""
        for cid in self._graph.list_concepts():
            if not self._graph.get_prerequisites(cid):
                return cid
        return "em-uniform-plane-wave"

    @staticmethod
    def _render_state(state: LearnerState) -> str:
        d = state.to_dict()
        lines = ["[Learner State]"]
        lines.append(f"cold_start: {state.cold_start}")
        if state.concept_mastery:
            mastery = ", ".join(f"{cid}={v}" for cid, v in state.concept_mastery.items())
            lines.append(f"mastery (0-1): {mastery}")
        if state.weak_concepts:
            weak = ", ".join(w.get("id", "?") for w in state.weak_concepts)
            lines.append(f"weak_concepts: {weak}")
        if state.review_due:
            lines.append(f"review_due: {', '.join(state.review_due)}")
        if state.recent_errors:
            errors = ", ".join(e.get("concept_id", "?") for e in state.recent_errors)
            lines.append(f"recent_errors: {errors}")
        if state.active_projects:
            lines.append(f"active_projects: {', '.join(state.active_projects)}")
        if state.learning_goals:
            lines.append(f"learning_goals: {', '.join(state.learning_goals)}")
        if state.unfinished_sessions:
            lines.append(f"unfinished_sessions: {', '.join(state.unfinished_sessions)}")
        return "\n".join(lines)

    @staticmethod
    def _tool_record(tool: str, result: ToolResult) -> dict:
        return {
            "tool": tool,
            "success": result.success,
            "error": result.error,
            "result": dict(result.result),
        }

    @staticmethod
    def _failed(
        decision: SkillDecision,
        state: LearnerState,
        tool_results: list[dict],
        warnings: list[str],
        reason: str,
    ) -> "SkillRunResult":
        return SkillRunResult(
            success=False,
            decision=decision,
            state=state,
            tool_results=tuple(tool_results),
            warnings=tuple(warnings),
            error=f"tool_failed: {reason}",
        )


def _default_registry() -> ToolRegistry:
    from core.learning.tools.adapters import build_default_registry

    return build_default_registry()


def _model_request(messages):
    from core.learning.agent.model.schema import ModelRequest

    return ModelRequest(messages=messages)
