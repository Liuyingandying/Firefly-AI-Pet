# -*- coding: utf-8 -*-
"""Tutor session schema — 学习会话执行状态协议（M4.4）。

设计约束:
- 状态机封闭词表: created → running → waiting_answer → completed / failed;
- frozen + `status_history` 扩展记录全部迁移（验收"状态转换正确"）;
- ``question``/``evaluation``/``evidence`` 持有 M4.1/M4.2/M4.3 对象;
- 全部 JSON 原生类型（对象字段经 to_dict 投影）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 状态机词表（封闭, 迁移顺序固定）
# ---------------------------------------------------------------------------

STATUS_CREATED = "created"
STATUS_RUNNING = "running"
STATUS_WAITING_ANSWER = "waiting_answer"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

#: 执行步名（executor 的可执行语义, 非计划模板）
STEP_LESSON = "lesson"
STEP_EXPERIMENT = "experiment"
STEP_QUESTION = "question"
STEP_ANSWER = "answer"
STEP_JUDGE = "judge"
STEP_EVIDENCE = "evidence"


@dataclass(frozen=True)
class TutorSessionState:
    """一次 Tutor 会话的完整执行状态。"""

    session_id: str
    concept_id: str
    current_step: str = ""
    completed_steps: tuple[str, ...] = ()
    question: object | None = None         # M4.1 LearningQuestion
    evaluation: object | None = None       # M4.2 LearningEvaluation
    evidence: object | None = None         # M0.6 LearningEvidence | None（错误/空 → None）
    status: str = STATUS_CREATED
    warnings: tuple[str, ...] = ()
    # ---- 扩展字段（审计/回显） ----
    plan_steps: tuple[str, ...] = ()       # 既有计划 steps 原样回显
    session_type: str = ""                 # 计划 session_type
    related_experiment: str = ""
    experiment_result: dict = field(default_factory=dict)
    answer: str = ""                       # 学生提交的回答
    status_history: tuple[str, ...] = ()   # 状态迁移史（验收断言用）

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "concept_id": self.concept_id,
            "current_step": self.current_step,
            "completed_steps": list(self.completed_steps),
            "question": self.question.to_dict() if self.question is not None else None,
            "evaluation": self.evaluation.to_dict() if self.evaluation is not None else None,
            "evidence": self.evidence.to_dict() if self.evidence is not None else None,
            "status": self.status,
            "warnings": list(self.warnings),
            "plan_steps": list(self.plan_steps),
            "session_type": self.session_type,
            "related_experiment": self.related_experiment,
            "experiment_result": dict(self.experiment_result),
            "answer": self.answer,
            "status_history": list(self.status_history),
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)
