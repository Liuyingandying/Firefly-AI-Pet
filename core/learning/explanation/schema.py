# -*- coding: utf-8 -*-
"""Explanation schema — 教学解释层的请求/响应协议（M0.5）。

设计约束：
- 只读消费 ``core.learning.simulation.runner.SimulationResult``（不反向依赖、
  不修改 Runner/Registry）；
- 第一版解释由模板规则生成（``generated_by="template_rules_v1"``），
  接口形状为未来 LLM 解释器预留（同一 Request/Response 协议可换后端）；
- ``ExplanationResponse`` 必须 JSON 可序列化（Path/枚举不出现在 to_dict 输出）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum

from core.learning.simulation.runner import SimulationResult


class ExplanationKind(str, Enum):
    """三种解释场景（与验收矩阵一一对应）。"""

    RESULT = "result"              # 实验结果解释：completed 产物 → 物理量解读
    PARAM_CHANGE = "param_change"  # 参数变化解释：before/after 差异 → 影响规则
    ERROR = "error"                # 错误原因解释：失败终态 → 原因 + 修复建议


@dataclass(frozen=True)
class ExplanationRequest:
    """解释请求。

    kind=result/error 时 ``result`` 必填；kind=param_change 时
    ``params_before``/``params_after`` 必填（``result`` 可选，用于附带
    after 运行的产物上下文）。
    """

    kind: ExplanationKind
    result: SimulationResult | None = None
    params_before: dict | None = None
    params_after: dict | None = None
    #: 预留受众字段（v1 模板规则不分支，未来 LLM 后端可用）
    audience: str = "undergraduate"

    def echo(self) -> dict:
        """请求摘要（进 response.request_echo，JSON 安全）。"""
        return {
            "kind": self.kind.value if isinstance(self.kind, ExplanationKind) else str(self.kind),
            "has_result": self.result is not None,
            "status": getattr(self.result, "status", "") or "",
            "experiment_id": getattr(self.result, "experiment_id", "") or "",
            "params_before": dict(self.params_before) if self.params_before else None,
            "params_after": dict(self.params_after) if self.params_after else None,
        }


@dataclass(frozen=True)
class ExplanationResponse:
    """解释响应（全部字段为 JSON 原生类型或可 str 化）。"""

    ok: bool = True
    kind: str = ""
    generated_by: str = "template_rules_v1"
    title: str = ""
    #: markdown 正文（可直接渲染进学习会话卡片）
    summary: str = ""
    key_points: tuple[str, ...] = ()
    suggestions: tuple[str, ...] = ()
    request_echo: dict = field(default_factory=dict)
    #: ok=False 时的机器可读原因（如 "missing_result"）
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "kind": self.kind,
            "generated_by": self.generated_by,
            "title": self.title,
            "summary": self.summary,
            "key_points": list(self.key_points),
            "suggestions": list(self.suggestions),
            "request_echo": dict(self.request_echo),
            "error": self.error,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)
