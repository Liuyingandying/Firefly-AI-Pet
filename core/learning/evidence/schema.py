# -*- coding: utf-8 -*-
"""Evidence schema — Learning Evidence 协议（M0.6）。

设计约束：
- 只读消费 ``core.learning.simulation.runner.SimulationResult`` 与
  ``core.learning.explanation`` 的解释能力（不修改任何上游模块）；
- 纯内存数据结构, 不接数据库/LLM/UI——持久化到 Learning Store 属 M1+；
- ``LearningEvidence`` 全字段 JSON 原生类型（tuple→list 由 to_dict 转换,
  timestamp 为 UTC-ISO 字符串, confidence 为 0–1 浮点）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

#: 证据类型：单次成功运行
KIND_RUN = "experiment_run"
#: 证据类型：参数变化比较
KIND_COMPARISON = "param_comparison"


@dataclass(frozen=True)
class LearningEvidence:
    """一条学习证据：记录学习者在实验中"做了什么 / 观察到什么"。

    规约字段（M0.6 验收口径）+ 一个类型扩展字段 ``kind``。
    failed/timeout 结果**不产生**本记录（builder 返回 None）,
    即错误不会污染掌握度评估。
    """

    evidence_id: str
    experiment_id: str = ""
    concept_ids: tuple[str, ...] = ()
    #: 学习者/系统的动作描述（"将频率从 2.4 调整到 5.8 GHz"）
    actions: tuple[str, ...] = ()
    #: 被观察到的现象/物理量（"λ ≈ 124.9 mm"、"f↑ → λ 缩短"）
    observations: tuple[str, ...] = ()
    #: 证据强度 0–1（M0 为固定常数: 运行 0.7 / 比较 0.9, M1 可学习化）
    confidence: float = 0.0
    #: UTC-ISO8601 字符串（与 Learning Store 时间戳口径一致）
    timestamp: str = ""
    #: 扩展字段: KIND_RUN / KIND_COMPARISON
    kind: str = KIND_RUN

    def __post_init__(self) -> None:
        if not self.evidence_id:
            raise ValueError("evidence_id 不能为空")
        conf = float(self.confidence)
        if not (0.0 <= conf <= 1.0):
            raise ValueError(f"confidence 必须在 [0, 1], 得到 {self.confidence}")
        object.__setattr__(self, "confidence", conf)

    def to_dict(self) -> dict:
        return {
            "evidence_id": self.evidence_id,
            "experiment_id": self.experiment_id,
            "concept_ids": list(self.concept_ids),
            "actions": list(self.actions),
            "observations": list(self.observations),
            "confidence": self.confidence,
            "timestamp": self.timestamp,
            "kind": self.kind,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)
