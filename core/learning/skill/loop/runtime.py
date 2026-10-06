# -*- coding: utf-8 -*-
"""MultiTurnSkillLoop — 持续学习循环（M4.7 阶段 2）。

每轮六段::

    Observe   LearnerStateBuilder.build（M4.6 聚合, 只读）
    Decide    规则决策（薄弱→quiz / 到期→review / 项目→research / 其余→teach）
    Act       既有能力路由（出题/查询/计划/仿真）
    Evaluate  quiz → Judge（正确=success / 错误=weak_point）; 其余动作记录完成状态
    Remember  M4.3 证据适配 + LearningMemoryWriter（add_memory 单一收口, 失败仅告警）
    Next      预算内回 Observe; 预算耗尽 → completed（安全停止）

预算: run(goal, max_steps=5)。失败全部结构化（status=failed + warnings）,
永不抛异常。Memory/Store/图零写入旁路——Remember 只经 LearningMemoryWriter
（add_memory 单一收口）; quiz/probe 题面脱敏（答案不出服务端）。
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timezone

from core.learning.evidence_answer import AnswerEvidenceBuilder
from core.learning.judge import AnswerJudge
from core.learning.knowledge_graph import DEFAULT_NODES, KnowledgeGraph
from core.learning.question import QuestionGenerator
from core.learning.skill.decision import SkillDecisionValidator
from core.learning.skill.loop.memory_writer import LearningMemoryWriter
from core.learning.skill.loop.schema import (
    SkillLoopResult,
    SkillLoopState,
    SkillStepRecord,
)
from core.learning.skill.prompt import build_skill_messages
from core.learning.skill.schema import LearnerState, SkillDecision
from core.learning.skill.state_builder import LearnerStateBuilder
from core.learning.tools.tool_registry import ToolRegistry


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _default_registry() -> ToolRegistry:
    from core.learning.tools.adapters import build_default_registry

    return build_default_registry()


class MultiTurnSkillLoop:
    """持续学习循环（Observe→Decide→Act→Evaluate→Remember→Next）。"""

    def __init__(
        self,
        *,
        model=None,                            # LearningModel（M3.1）; None=规则决策
        brain=None,                            # M4.8 TJULLMLearningBrain; None=规则决策
        state_builder: LearnerStateBuilder | None = None,
        registry: ToolRegistry | None = None,
        question_generator: QuestionGenerator | None = None,
        judge: AnswerJudge | None = None,
        evidence_builder: AnswerEvidenceBuilder | None = None,
        memory_writer: LearningMemoryWriter | None = None,
        graph: KnowledgeGraph | None = None,
        answer_provider=None,
        max_steps: int = 5,
    ):
        self._graph = graph if graph is not None else KnowledgeGraph()
        self._model = model
        self._brain = brain                    # M4.8: 优先 brain.decide()，失败回退规则
        self._state_builder = (
            state_builder if state_builder is not None
            else LearnerStateBuilder(graph=self._graph)
        )
        self._registry = registry if registry is not None else _default_registry()
        self._questions = (
            question_generator if question_generator is not None
            else QuestionGenerator(graph=self._graph)
        )
        self._judge = judge if judge is not None else AnswerJudge()
        self._evidence = (
            evidence_builder if evidence_builder is not None else AnswerEvidenceBuilder()
        )
        self._memory_writer = (
            memory_writer if memory_writer is not None else LearningMemoryWriter()
        )
        self._answer_provider = answer_provider
        self._max_steps = max(1, int(max_steps))

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------

    def run(self, goal: str, *, max_steps: int | None = None) -> SkillLoopResult:
        """跑一次持续学习循环; 任何失败都以结构化结果返回, 永不抛异常。"""
        goal = str(goal or "").strip()
        if not goal:
            failed = SkillLoopState(
                session_id=self._new_session_id(), goal="",
                status="failed", status_history=("created", "failed"),
            )
            return SkillLoopResult(
                success=False, state=failed,
                error="empty_goal: goal is empty",
            )
        self._loop_goal = goal
        limit = self._max_steps if max_steps is None else max(1, int(max_steps))

        session_id = self._new_session_id()
        state = SkillLoopState(
            session_id=session_id,
            goal=goal,
            status="observing",
            status_history=("created", "observing"),
        )
        warnings: list[str] = []
        tool_results: list[dict] = []
        final_message = ""
        last_action = ""

        try:
            while state.step_count < limit:
                # ---- 1. Observe ------------------------------------------------
                state = dataclasses.replace(state, status="observing")
                learner_state = self._state_builder.build(learning_goal=goal)
                state = dataclasses.replace(state, learner_state=learner_state)

                # ---- 2. Decide（规则决策, 无 LLM） ---------------------------------
                state = dataclasses.replace(state, status="deciding")
                decision, done, round_warnings = self._decide(learner_state)
                warnings.extend(round_warnings)
                state = dataclasses.replace(
                    state, decision=decision, current_action=decision.action,
                )

                # 规则判定 done → 本步执行完后收尾（探底题交付等）
                round_done = done

                # 模型 metadata.done → 不执行, 直接给最终回答并收尾
                if decision.metadata.get("done"):
                    state = dataclasses.replace(
                        state,
                        status="completed",
                        status_history=state.status_history + ("completed",),
                    )
                    return SkillLoopResult(
                        success=True,
                        state=state,
                        final_message=decision.reason or "学习循环完成。",
                        warnings=tuple(warnings),
                    )

                # ---- 3. Act ------------------------------------------------------
                state = dataclasses.replace(state, status="executing")
                outcome = self._execute_action(decision)
                tool_results.extend(outcome.get("tools", []))

                # ---- 4. Evaluate --------------------------------------------------
                state = dataclasses.replace(state, status="evaluating")
                evaluation = outcome.get("evaluation")
                evidence = outcome.get("evidence")
                if evaluation is not None:
                    if evaluation.correct:
                        observation = (
                            f"success: {decision.target_concept} "
                            f"score={evaluation.score}"
                        )
                    else:
                        observation = (
                            f"weak_point: {decision.target_concept} "
                            f"error_type={evaluation.error_type} "
                            f"score={evaluation.score}"
                        )
                        warnings.append(observation)
                else:
                    observation = outcome.get(
                        "observation", f"{decision.action} completed"
                    )

                # ---- 5. Remember ----------------------------------------------------
                state = dataclasses.replace(state, status="remembering")
                memory_record = self._memory_writer.write_learning_event(
                    concept_id=decision.target_concept,
                    content=(
                        f"用户围绕「{decision.target_concept}」进行了"
                        f"{decision.action}学习。{observation}"
                    ),
                    outcome=observation,
                )
                if not memory_record.ok:
                    warnings.append(f"memory write failed: {memory_record.error}")

                # ---- 6. 台账 + 轮数 ---------------------------------------------------
                state = dataclasses.replace(
                    state,
                    history=state.history + (SkillStepRecord(
                        step_id=f"step-{state.step_count + 1}",
                        action=decision.action,
                        target=decision.target_concept,
                        result={
                            "tools": list(tool_results[-1:]),
                            "correct": (
                                evaluation.correct if evaluation is not None else None
                            ),
                            "evidence_kind": (
                                evidence.kind if evidence is not None else None
                            ),
                            "memory_ok": memory_record.ok,
                        },
                        observation=observation,
                        timestamp=_now_iso(),
                    ),),
                    observations=state.observations + (observation,),
                    step_count=state.step_count + 1,
                    status_history=state.status_history + ("remembering",),
                )
                last_action = decision.action

                # 本轮收尾（探底交付 / 规则 done / 模型 metadata.done）→ 完成
                if round_done or outcome.get("done"):
                    state = dataclasses.replace(
                        state,
                        status="completed",
                        status_history=state.status_history + ("completed",),
                    )
                    msg = (
                        outcome.get("final_message")
                        or decision.reason
                        or "本轮学习完成。"
                    )
                    return SkillLoopResult(
                        success=True,
                        state=state,
                        final_message=msg,
                        warnings=tuple(warnings),
                    )

            # ---- 轮数预算耗尽（安全停止, 非失败） ---------------------------------
            state = dataclasses.replace(
                state,
                status="completed",
                status_history=state.status_history + ("completed",),
            )
            warnings.append(f"stopped after max steps ({limit})")
            return SkillLoopResult(
                success=True,
                state=state,
                final_message=(
                    f"已在 {limit} 轮内完成学习循环"
                    f"（最近动作: {last_action or '—'}）。"
                ),
                warnings=tuple(warnings),
            )
        except Exception as exc:  # noqa: BLE001 - 错误即结果
            failed = dataclasses.replace(
                state,
                status="failed",
                status_history=state.status_history + ("failed",),
            )
            return SkillLoopResult(
                success=False,
                state=failed,
                warnings=tuple(warnings),
                error=f"execution_failed: {type(exc).__name__}: {exc}",
            )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _execute_action(self, decision: SkillDecision) -> dict:
        action = decision.action
        target = decision.target_concept

        if action == "quiz":
            return self._quiz_round(decision, target)
        if action == "cold_start_probe" or action == "probe":
            return self._probe_round(decision, target)
        if action == "experiment":
            return self._experiment_round(decision, target)
        if action == "review":
            return self._review_round(decision, target)
        if action == "research":
            return self._research_round(decision, target)
        if action == "teach":
            return self._teach_round(decision, target)
        # continue_project / 未知动作 → 结构化完成（无可执行体）
        return self._outcome(
            tools=[], observation=f"action {action!r} has no executor",
            outcome="completed", final_message=f"动作 {action} 无执行体, 跳过。",
        )

    def _root_concept(self) -> str:
        """冷启动目标: 第一个无前置的概念（入门概念）。"""
        for cid in self._graph.list_concepts():
            if not self._graph.get_prerequisites(cid):
                return cid
        return "em-uniform-plane-wave"

    def _decide(self, learner_state: LearnerState) -> tuple[SkillDecision, bool, list[str]]:
        """决策: 先安全门 (Learner State Gate), 再 brain/规则。"""
        gate_action, gate_reason = self._gate_check(learner_state)
        if self._brain is not None:
            return self._brain_decide(learner_state, gate_action, gate_reason)
        return self._rule_fallback(learner_state)

    def _gate_check(self, ls: LearnerState) -> tuple[str | None, str]:
        """Learner State Gate: 安全先验 (非替代 Brain)。None = 无限制。"""
        if ls.cold_start:
            return ("probe", "cold_start: 无学习记录, 先探底")
        if ls.review_due:
            return ("review", "存在到期复习项")
        if ls.weak_concepts:
            return ("quiz", "存在薄弱概念")
        return (None, "无明确安全信号, 由 brain 自主决策")

    def _brain_decide(
        self,
        learner_state: LearnerState,
        gate_action: str | None,
        gate_reason: str,
    ) -> tuple[SkillDecision, bool, list[str]]:
        """M4.10: TJULLMLearningBrain 决策（含安全先验）; 失败回退规则。"""
        try:
            decision, done = self._brain.decide(
                learner_state, goal=self._loop_goal or "",
            )
            # 安全门校验: brain 决策不得违反 gate 硬约束
            if gate_action and decision.action not in (
                gate_action, "none", "cold_start_probe",
            ):
                decision = dataclasses.replace(
                    decision, action=gate_action,
                    reason=f"[gate override: {gate_reason}] {decision.reason}",
                )
            return decision, done, []
        except Exception as exc:  # noqa: BLE001 - brain 故障降级
            decision, done, round_warnings = self._rule_fallback(learner_state)
            return decision, done, list(round_warnings) + [f"brain degraded: {exc}"]

    def _rule_fallback(self, learner_state: LearnerState) -> tuple[SkillDecision, bool]:
        """规则决策回退（原 M4.7 逻辑, 零改动）。"""
        if learner_state.cold_start:
            return (
                SkillDecision(
                    action="cold_start_probe",
                    target_concept=self._root_concept(),
                    reason="cold_start: 无学习记录, 用入门概念探底",
                    confidence=1.0,
                ),
                True,   # 探底完成
                [],
            )
        if learner_state.review_due:
            return (
                SkillDecision(
                    action="review",
                    target_concept=learner_state.review_due[0],
                    reason="存在到期复习项",
                    confidence=0.9,
                ),
                False,
                [],
            )
        if learner_state.weak_concepts:
            target = learner_state.weak_concepts[0].get("id", "") or self._root_concept()
            return (
                SkillDecision(
                    action="quiz",
                    target_concept=target,
                    reason="存在薄弱概念, 出题检测",
                    confidence=0.8,
                ),
                False,
                [],
            )
        if learner_state.unfinished_sessions:
            return (
                SkillDecision(
                    action="continue_project",
                    target_concept=self._root_concept(),
                    reason="存在未完成会话",
                    confidence=0.7,
                ),
                False,
                [],
            )
        return (
            SkillDecision(
                action="probe",
                target_concept=self._root_concept(),
                reason="无明确信号, 探底确认理解",
                confidence=0.6,
            ),
            True,
            [],
        )

    def _probe_round(self, decision: SkillDecision, target: str) -> dict:
        generated = self._questions.generate(target)
        if not generated.success or not generated.questions:
            return self._outcome(
                tools=[],
                observation=f"question generation failed: {generated.error}",
                outcome="failed", final_message=generated.error,
            )
        question = generated.questions[0]
        served = {
            "question_id": question.question_id,
            "question_type": question.question_type,
            "question_text": question.question_text,
        }
        return self._outcome(
            tools=[{"served_question": served}],
            observation=f"probe question served: {question.question_text}",
            outcome="completed",
            final_message=f"探底完成, 等待学习者在聊天中回答。",
        )

    def _quiz_round(self, decision: SkillDecision, target: str) -> dict:
        generated = self._questions.generate(target)
        if not generated.success or not generated.questions:
            return self._outcome(
                tools=[],
                observation=f"question generation failed: {generated.error}",
                outcome="failed", final_message=generated.error,
            )
        question = generated.questions[0]
        served = {
            "question_id": question.question_id,
            "question_type": question.question_type,
            "question_text": question.question_text,
        }
        answer = None
        if self._answer_provider is not None:
            try:
                answer = self._answer_provider(question)
            except Exception as exc:  # noqa: BLE001 - 作答故障不阻断
                return self._outcome(
                    tools=[{"served_question": served}],
                    observation=f"answer provider failed: {exc}",
                    outcome="failed", final_message=str(exc),
                )
        evaluation = self._judge.evaluate(question, str(answer))
        evidence_result = self._evidence.build(question, evaluation)
        evidence = evidence_result.evidence
        tools = [{"served_question": served}, {"judge": evaluation.to_dict()}]
        if evidence is not None:
            tools.append({"evidence_id": evidence.evidence_id,
                          "kind": evidence.kind})
        if evaluation.correct:
            observation = f"success: score={evaluation.score}"
        else:
            observation = f"weak_point: error_type={evaluation.error_type}"
        return self._outcome(
            tools=tools, evaluation=evaluation, evidence=evidence,
            observation=observation,
            outcome="completed" if evaluation.correct else "weak_point",
        )

    def _experiment_round(self, decision: SkillDecision, target: str) -> dict:
        experiment_id = self._related_experiment(target)
        if not experiment_id:
            fallback = self._registry.execute("query_concept", {"concept_id": target})
            fallback_record = self._tool_record("query_concept", fallback)
            return self._outcome(
                tools=[fallback_record],
                observation=f"concept {target!r} has no related experiment; "
                            f"fell back to concept query",
                outcome="completed",
                final_message=f"概念 {target} 无关联实验。",
            )
        tool_result = self._registry.execute(
            "run_experiment", {"experiment_id": experiment_id}
        )
        tool_record = self._tool_record("run_experiment", tool_result)
        if not tool_result.success:
            return self._outcome(
                tools=[tool_record],
                observation=f"experiment failed: {tool_result.error}",
                outcome="failed", final_message=tool_result.error,
            )
        return self._outcome(
            tools=[tool_record],
            observation=f"experiment completed: {experiment_id}",
            outcome="completed",
            final_message=f"实验 {experiment_id} 完成。",
        )

    def _review_round(self, decision: SkillDecision, target: str) -> dict:
        generated = self._questions.generate(target)
        question = generated.questions[0] if generated.questions else None
        observation = (
            f"复习题: {question.question_text}" if question is not None
            else f"复习「{target}」（暂无复习题, 请口头复述要点）"
        )
        return self._outcome(
            tools=[], observation=observation, outcome="completed",
            final_message=f"复习「{target}」完成。",
        )

    def _research_round(self, decision: SkillDecision, target: str) -> dict:
        fallback = self._registry.execute("query_concept", {"concept_id": target})
        fallback_record = self._tool_record("query_concept", fallback)
        return self._outcome(
            tools=[fallback_record],
            observation=f"concept queried: {target}",
            outcome="completed",
            final_message=f"「{target}」资料查询完成。",
        )

    def _teach_round(self, decision: SkillDecision, target: str) -> dict:
        return self._outcome(
            tools=[],
            observation=f"teach {target}",
            outcome="completed",
            final_message=f"「{target}」教学完成。",
        )

    @staticmethod
    def _outcome(
        *,
        tools: list[dict],
        observation: str,
        outcome: str,
        evaluation=None,
        evidence=None,
        done: bool = False,
        final_message: str = "",
    ) -> dict:
        return {
            "tools": tools,
            "observation": observation,
            "outcome": outcome,
            "evaluation": evaluation,
            "evidence": evidence,
            "done": done,
            "final_message": final_message,
        }

    def _related_experiment(self, concept_id: str) -> str:
        node = self._graph.get_concept(concept_id)
        if node is not None and node.related_experiments:
            return node.related_experiments[0]
        return ""

    @staticmethod
    def _tool_record(tool: str, result) -> dict:
        return {
            "tool": tool,
            "success": result.success,
            "error": result.error,
            "result": dict(result.result),
        }

    def _new_session_id(self) -> str:
        return f"skill-loop-{uuid.uuid4().hex[:12]}"
