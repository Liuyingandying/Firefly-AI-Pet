# -*- coding: utf-8 -*-
"""Firefly Learning Mode M4.4 — Tutor Session Executor（学习会话执行器）。

消费既有 LearningSessionPlan（M1.5, 只读）, 产出可执行 Tutor 会话:
lesson → experiment → question → waiting_answer → submit_answer →
judge → evidence → completed;
状态机封闭词表 + status_history 迁移史 + 结构化失败（禁止异常逃逸）;
不修改 Session Planner/Agent Runtime/Rule Engine/EvidenceBridge/TJULLM Adapter。
"""

from core.learning.tutor.executor import TutorSessionExecutor
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

__all__ = [
    "TutorSessionExecutor",
    "TutorSessionState",
    "STATUS_CREATED",
    "STATUS_RUNNING",
    "STATUS_WAITING_ANSWER",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STEP_LESSON",
    "STEP_EXPERIMENT",
    "STEP_QUESTION",
    "STEP_ANSWER",
    "STEP_JUDGE",
    "STEP_EVIDENCE",
]
