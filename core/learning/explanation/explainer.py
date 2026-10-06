# -*- coding: utf-8 -*-
"""SimulationExplainer — 解释层门面（M0.5）。

统一入口：``explain(request) → ExplanationResponse``，按 ``request.kind``
分派到模板规则解释器（v1）。未来接入 LLM 后端时，只需在同一分发点替换
后端实现——Request/Response 协议与调用方零改动。

门面负责请求合法性校验（缺失 result / 缺失参数字典 → ``ok=False`` 的
错误响应而非抛异常），规则函数只处理合法输入。
"""

from __future__ import annotations

from core.learning.explanation.schema import (
    ExplanationKind,
    ExplanationRequest,
    ExplanationResponse,
)
from core.learning.explanation import template_explainer as _rules
from core.learning.simulation.runner import SimulationResult


class SimulationExplainer:
    """SimulationResult → 教学解释 的第一版闭环入口。"""

    VERSION = "template_rules_v1"

    def explain(self, request: ExplanationRequest) -> ExplanationResponse:
        """按 kind 分派；非法请求返回 ok=False 响应（不抛异常）。"""
        if not isinstance(request.kind, ExplanationKind):
            return self._reject(request, "unknown_kind")

        if request.kind is ExplanationKind.PARAM_CHANGE:
            if not request.params_before or not request.params_after:
                return self._reject(request, "missing_params")
            return _rules.explain_param_change(request)

        if request.result is None:
            return self._reject(request, "missing_result")

        if request.kind is ExplanationKind.RESULT:
            if not request.result.ok:
                return self._reject(request, "result_not_ok_use_error_kind")
            return _rules.explain_result(request)

        # ERROR
        return _rules.explain_error(request)

    # -- 便捷入口（对应三种场景, 内部构造 request） ----------------------------

    def explain_result(self, result: SimulationResult) -> ExplanationResponse:
        """completed 结果 → 物理量解读。"""
        return self.explain(ExplanationRequest(kind=ExplanationKind.RESULT, result=result))

    def explain_param_change(
        self,
        params_before: dict,
        params_after: dict,
        result: SimulationResult | None = None,
    ) -> ExplanationResponse:
        """参数变化 → 影响规则解读（参数字典接受 schema 键或 payload 键）。"""
        return self.explain(
            ExplanationRequest(
                kind=ExplanationKind.PARAM_CHANGE,
                result=result,
                params_before=params_before,
                params_after=params_after,
            )
        )

    def explain_error(self, result: SimulationResult) -> ExplanationResponse:
        """失败终态 → 原因 + 修复建议。"""
        return self.explain(ExplanationRequest(kind=ExplanationKind.ERROR, result=result))

    # -- 内部 -------------------------------------------------------------------

    @staticmethod
    def _reject(request: ExplanationRequest, error: str) -> ExplanationResponse:
        return ExplanationResponse(
            ok=False,
            kind=request.kind.value if isinstance(request.kind, ExplanationKind) else str(request.kind),
            generated_by=SimulationExplainer.VERSION,
            error=error,
            request_echo=request.echo(),
        )
