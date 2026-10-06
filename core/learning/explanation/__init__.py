# -*- coding: utf-8 -*-
"""Firefly Learning Mode M0.5 — Explanation Layer（模板规则解释器, 零模型调用）。

只读消费 ``core.learning.simulation`` 的 SimulationResult 协议,
不修改 Runner / Registry。
"""

from core.learning.explanation.explainer import SimulationExplainer
from core.learning.explanation.schema import (
    ExplanationKind,
    ExplanationRequest,
    ExplanationResponse,
)

__all__ = [
    "ExplanationKind",
    "ExplanationRequest",
    "ExplanationResponse",
    "SimulationExplainer",
]
