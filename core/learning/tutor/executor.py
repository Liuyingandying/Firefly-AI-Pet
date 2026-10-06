# -*- coding: utf-8 -*-
"""TutorSessionExecutor — 学习会话执行器（M4.4）。

消费既有 LearningSessionPlan（M1.5, 只读, 不产生计划）, 按会话类型解释
出自己的可执行序列::

    start(plan)
      → lesson（教学记录） → experiment（仿真, 若有） → question（出题）
      → waiting_answer
    submit_answer(session_id, answer)          # 模拟用户回答（无 GUI）
      → judge（M4.2 评价） → evidence（M4.3 适配, 错误/空 → None）
      → completed

失败语义: 任何一步失败 → status=failed + warnings 记录 + 结构化返回,
顶层兜底, 禁止异常逃逸。会话存储为进程内 dict（无数据库）。
"""

from __future__ import annotations

import dataclasses
import tempfile
import uuid
from pathlib import Path

from core.learning.judge import AnswerJudge
from core.learning.question import QuestionGenerator
from core.learning.evidence_answer import AnswerEvidenceBuilder
from core.learning.session.schema import LearningSessionPlan
from core.learning.simulation.runner import SimulationRunner
from core.learning.tutor.schema import (
    STATUS_COMPLETED,
    STATUS_CREATED,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_WAITING_ANSWER,
    STEP_ANSWER,
    STEP_EVIDENCE,
    STEP_EXPERIMENT,
    STEP_JUDGE,
    STEP_LESSON,
    STEP_QUESTION,
    TutorSessionState,
)


class TutorSessionExecutor:
    """Tutor 会话执行器（解释既有计划, 只新增执行层）。"""

    def __init__(
        self,
        *,
        question_generator: QuestionGenerator | None = None,
        judge: AnswerJudge | None = None,
        evidence_builder: AnswerEvidenceBuilder | None = None,
        experiment_runner=None,               # callable(experiment_id) -> dict
    ):
        self._questions = question_generator if question_generator is not None else QuestionGenerator()
        self._judge = judge if judge is not None else AnswerJudge()
        self._evidence = evidence_builder if evidence_builder is not None else AnswerEvidenceBuilder()
        self._experiment_runner = (
            experiment_runner if experiment_runner is not None else _default_experiment_runner
        )
        self._sessions: dict[str, TutorSessionState] = {}

    # ------------------------------------------------------------------
    # 会话查询
    # ------------------------------------------------------------------

    def get_session(self, session_id: str) -> TutorSessionState | None:
        return self._sessions.get(str(session_id or ""))

    # ------------------------------------------------------------------
    # 启动会话: lesson → experiment → question → waiting_answer
    # ------------------------------------------------------------------

    def start(self, plan) -> TutorSessionState:
        """执行既有计划的启动段; 任何失败返回 failed 状态, 永不抛异常。"""
        if not isinstance(plan, LearningSessionPlan):
            return self._failed_state(
                session_id="", concept_id="",
                warnings=("invalid_input: plan is not a LearningSessionPlan",),
            )
        if plan.session_type in ("", "none"):
            return self._failed_state(
                session_id=self._new_session_id(),
                concept_id=str(plan.target_concept or ""),
                warnings=(f"session_type={plan.session_type!r} has no tutor flow",),
                plan_steps=tuple(plan.steps),
                session_type=plan.session_type,
            )

        session_id = self._new_session_id()
        concept_id = str(plan.target_concept or "").strip()
        state = TutorSessionState(
            session_id=session_id,
            concept_id=concept_id,
            current_step=STEP_LESSON,
            plan_steps=tuple(plan.steps),
            session_type=plan.session_type,
            related_experiment=str(plan.related_experiment or ""),
            status_history=(STATUS_CREATED, STATUS_RUNNING),
            status=STATUS_RUNNING,
        )

        # ---- lesson: 教学步骤记录（解释层组装属演示/宿主职责, 此处记完成） ----
        state = self._advance(
            state, STEP_LESSON,
            f"教学环节（概念：{concept_id}）",
        )

        # ---- experiment: 仿真（若有关联实验） ---------------------------------
        if state.related_experiment:
            state = self._run_experiment_step(state)
            if state.status == STATUS_FAILED:
                return self._store(state)

        # ---- question: 出题 → waiting_answer ----------------------------------
        return self._store(self._run_question_step(state))

    # ------------------------------------------------------------------
    # 提交回答: judge → evidence → completed
    # ------------------------------------------------------------------

    def submit_answer(self, session_id: str, answer: str) -> TutorSessionState:
        """提交学生回答（模拟外部输入）; 完成评价与证据两步。"""
        state = self._sessions.get(str(session_id or ""))
        if state is None:
            return self._failed_state(
                session_id=str(session_id or ""), concept_id="",
                warnings=(f"unknown session: {session_id!r}",),
                status_history=(STATUS_FAILED,),
            )
        if state.status != STATUS_WAITING_ANSWER:
            failed = dataclasses.replace(
                state,
                status=STATUS_FAILED,
                warnings=state.warnings
                + (f"submit_answer called in status {state.status!r}",),
                status_history=state.status_history + (STATUS_FAILED,),
            )
            return self._store(failed)

        # ---- judge: M4.2 评价 --------------------------------------------------
        evaluation = self._judge.evaluate(state.question, answer)
        state = dataclasses.replace(
            state,
            answer=str(answer if isinstance(answer, str) else ""),
            evaluation=evaluation,
            current_step=STEP_JUDGE,
            completed_steps=state.completed_steps + (STEP_ANSWER, STEP_JUDGE),
        )
        if evaluation.error:
            failed = dataclasses.replace(
                state, status=STATUS_FAILED,
                warnings=state.warnings + (f"judge failed: {evaluation.error}",),
                status_history=state.status_history + (STATUS_FAILED,),
            )
            return self._store(failed)

        # ---- evidence: M4.3 适配（错误/空回答 → 无证据, 会话仍完成） ------------
        state = dataclasses.replace(state, current_step=STEP_EVIDENCE)
        evidence_result = self._evidence.build(state.question, evaluation)
        evidence = evidence_result.evidence
        warning = None
        if evidence is None:
            warning = f"no mastery evidence: {evidence_result.reason}"
            warnings = state.warnings + (warning,)
        else:
            warnings = state.warnings
        state = dataclasses.replace(
            state,
            evidence=evidence,
            completed_steps=state.completed_steps + (STEP_EVIDENCE,),
            current_step="completed",
            status_history=state.status_history + (STATUS_COMPLETED,),
            status=STATUS_COMPLETED,
            warnings=warnings,
        )
        return self._store(state)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _run_experiment_step(self, state: TutorSessionState) -> TutorSessionState:
        """experiment 步: 经注入的 runner 执行关联实验。"""
        try:
            payload = self._experiment_runner(state.related_experiment)
        except Exception as exc:  # noqa: BLE001 - 错误即状态
            failed = dataclasses.replace(
                state, status=STATUS_FAILED,
                warnings=state.warnings + (f"experiment failed: {exc}",),
                current_step=STEP_EXPERIMENT,
                status_history=state.status_history + (STATUS_FAILED,),
            )
            return failed
        if not isinstance(payload, dict) or not payload.get("ok", True):
            error = str((payload or {}).get("error", "experiment returned no data"))
            return dataclasses.replace(
                state, status=STATUS_FAILED,
                warnings=state.warnings + (f"experiment failed: {error}",),
                current_step=STEP_EXPERIMENT,
                experiment_result=dict(payload or {}),
                status_history=state.status_history + (STATUS_FAILED,),
            )
        return dataclasses.replace(
            state,
            completed_steps=state.completed_steps + (STEP_EXPERIMENT,),
            current_step=STEP_QUESTION,
            experiment_result=dict(payload),
        )

    def _run_question_step(self, state: TutorSessionState) -> TutorSessionState:
        """question 步: 出题 → waiting_answer（等待外部输入）。"""
        result = self._questions.generate(state.concept_id)
        if not result.success or not result.questions:
            failed = dataclasses.replace(
                state, status=STATUS_FAILED,
                warnings=state.warnings + (f"question generation failed: {result.error}",),
                current_step=STEP_QUESTION,
                status_history=state.status_history + (STATUS_FAILED,),
            )
            return failed
        question = result.questions[0]
        return dataclasses.replace(
            state,
            question=question,
            completed_steps=state.completed_steps + (STEP_QUESTION,),
            current_step="waiting_answer",
            status_history=state.status_history + (STATUS_WAITING_ANSWER,),
            status=STATUS_WAITING_ANSWER,
        )

    def _advance(
        self, state: TutorSessionState, step: str, action: str
    ) -> TutorSessionState:
        return dataclasses.replace(
            state,
            completed_steps=state.completed_steps + (step,),
            current_step=step,
        )

    def _store(self, state: TutorSessionState) -> TutorSessionState:
        if state.session_id:
            self._sessions[state.session_id] = state
        return state

    @staticmethod
    def _new_session_id() -> str:
        return f"tutor-{uuid.uuid4().hex[:12]}"

    @staticmethod
    def _failed_state(
        *,
        session_id: str,
        concept_id: str,
        warnings: tuple[str, ...] | list[str],
        plan_steps: tuple[str, ...] = (),
        session_type: str = "",
        status_history: tuple[str, ...] = (),
    ) -> TutorSessionState:
        return TutorSessionState(
            session_id=session_id,
            concept_id=concept_id,
            status=STATUS_FAILED,
            warnings=tuple(warnings),
            plan_steps=plan_steps,
            session_type=session_type,
            status_history=status_history or (STATUS_CREATED, STATUS_FAILED),
        )


def _default_experiment_runner(experiment_id: str) -> dict:
    """默认实验执行: M0.4 SimulationRunner（真实仿真, ~2s）。"""
    runner = SimulationRunner(timeout_s=30)
    result = runner.run_experiment(
        experiment_id, out_dir=Path(tempfile.mkdtemp(prefix="firefly_tutor_exp_"))
    )
    if result.ok:
        return {
            "ok": True,
            "status": result.status,
            "elapsed_ms": result.elapsed_ms,
            "artifacts": {
                "png": str(result.png_path),
                "gif": str(result.gif_path),
                "markdown": str(result.md_path),
            },
            "params": dict(result.params_payload),
        }
    return {"ok": False, "error": result.reason}
