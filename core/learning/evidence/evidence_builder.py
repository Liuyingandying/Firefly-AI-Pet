# -*- coding: utf-8 -*-
"""EvidenceBuilder — SimulationResult → LearningEvidence 适配器（M0.6）。

分层复用（全部只读, 零修改上游）::

    SimulationRunner(M0.4)  ──事实──▶  SimulationResult
    ExplanationLayer(M0.5)  ──解读──▶  key_points（解释失败有本地回退）
    EvidenceBuilder(M0.6)   ──证据──▶  LearningEvidence

关键规则:
- 仅 ``ok=True`` 且 ``status=completed`` 的结果生成证据;
- failed/timeout/param_error/security_rejected **一律返回 None**——
  错误不生成掌握记录（避免失败拉低掌握度评估）;
- concept_ids 解析顺序: 显式参数 > 注册表查找（只读） > 构造缺省;
- 纯内存操作, 不触数据库/UI/LLM。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Iterable

from core.learning.evidence.schema import (
    KIND_COMPARISON,
    KIND_RUN,
    LearningEvidence,
)
from core.learning.simulation.result import SimulationStatus
from core.learning.simulation.runner import SimulationResult

#: 参数键归一（同时接受 schema 键与 payload 键; 本地实现, 不 import 私有符号）
_KEY_ALIASES = {
    "frequency": "frequency",
    "frequency_ghz": "frequency",
    "medium": "medium",
    "polarization": "polarization",
    "polarization_deg": "polarization",
    "amplitude": "amplitude",
}

_PARAM_LABELS = {
    "frequency": "频率 f",
    "medium": "介质",
    "polarization": "极化角 ψ",
    "amplitude": "振幅 E0",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm_params(raw: dict | None) -> dict:
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for key, value in raw.items():
        canon = _KEY_ALIASES.get(str(key).strip().lower())
        if canon is not None:
            out[canon] = value
    return out


class EvidenceBuilder:
    """LearningEvidence 工厂（M0 常数置信度, M1 可演进为可学习置信度）。"""

    #: 单次成功运行的证据强度
    RUN_CONFIDENCE = 0.7
    #: 参数比较证据强度（有明确的受控差异, 比单次运行更强）
    COMPARISON_CONFIDENCE = 0.9

    def __init__(
        self,
        *,
        registry=None,
        default_concept_ids: Iterable[str] = ("em-uniform-plane-wave",),
        explainer=None,
    ):
        self._registry = registry
        self._default_concepts = tuple(default_concept_ids)
        self._explainer = explainer

    # ------------------------------------------------------------------
    # 1. success 实验 → 运行证据
    # ------------------------------------------------------------------

    def from_result(
        self,
        result: SimulationResult | None,
        *,
        concept_ids: Iterable[str] | None = None,
        actions: Iterable[str] = (),
        observations: Iterable[str] = (),
    ) -> LearningEvidence | None:
        """completed 结果 → 运行证据；失败/超时 → None（无掌握记录）。"""
        if result is None or not result.ok:
            return None
        if str(result.status) != SimulationStatus.COMPLETED.value:
            return None

        exp_id = result.experiment_id or ""
        payload = dict(result.params_payload or {})
        norm = _norm_params(payload)

        run_actions = list(actions)
        run_actions.append(
            "完成仿真运行：{label}（f = {f} GHz, medium = {m}, ψ = {p}°, E0 = {a}）".format(
                label=result.display_name or exp_id or "uniform_plane_wave 直连运行",
                f=norm.get("frequency", "—"),
                m=norm.get("medium", "—"),
                p=norm.get("polarization", "—"),
                a=norm.get("amplitude", "—"),
            )
        )
        if result.png_path and result.gif_path:
            run_actions.append("获取产物：electric_field.png + wave.gif（12 帧传播动画）")

        run_observations = tuple(observations) + self._explain_observations(result, norm)

        return LearningEvidence(
            evidence_id=uuid.uuid4().hex[:12],
            experiment_id=exp_id,
            concept_ids=self._resolve_concepts(exp_id, concept_ids),
            actions=tuple(run_actions),
            observations=tuple(run_observations),
            confidence=self.RUN_CONFIDENCE,
            timestamp=_now_iso(),
            kind=KIND_RUN,
        )

    # ------------------------------------------------------------------
    # 2. 参数变化 → 比较证据
    # ------------------------------------------------------------------

    def from_param_change(
        self,
        *,
        params_before: dict | None,
        params_after: dict | None,
        result: SimulationResult | None = None,
        experiment_id: str = "",
        concept_ids: Iterable[str] | None = None,
    ) -> LearningEvidence | None:
        """受控参数变化 → 比较证据；无差异/空字典 → None（无信号不造证据）。"""
        before = _norm_params(params_before)
        after = _norm_params(params_after)
        if not before or not after:
            return None

        changed = [
            key for key in ("frequency", "medium", "polarization", "amplitude")
            if key in before and key in after and before[key] != after[key]
        ]
        if not changed:
            return None

        cmp_actions = [
            f"调整{_PARAM_LABELS[key]}：{before[key]} → {after[key]}" for key in changed
        ]
        cmp_observations = self._comparison_observations(before, after, changed)
        if result is not None and result.ok:
            cmp_observations = cmp_observations + self._explain_observations(
                result, _norm_params(dict(result.params_payload or {}))
            )
        exp_id = experiment_id or (result.experiment_id if result is not None else "") or ""

        return LearningEvidence(
            evidence_id=uuid.uuid4().hex[:12],
            experiment_id=exp_id,
            concept_ids=self._resolve_concepts(exp_id, concept_ids),
            actions=tuple(cmp_actions),
            observations=tuple(cmp_observations),
            confidence=self.COMPARISON_CONFIDENCE,
            timestamp=_now_iso(),
            kind=KIND_COMPARISON,
        )

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _resolve_concepts(
        self, experiment_id: str, explicit: Iterable[str] | None
    ) -> tuple[str, ...]:
        """显式参数 > 注册表只读查找 > 构造缺省。"""
        if explicit is not None:
            concepts = tuple(explicit)
        elif experiment_id:
            concepts = self._lookup_concepts(experiment_id)
        else:
            concepts = self._default_concepts
        return tuple(c for c in concepts if c)

    def _lookup_concepts(self, experiment_id: str) -> tuple[str, ...]:
        try:
            from core.learning.simulation.registry import ExperimentRegistry

            registry = self._registry
            if registry is None:
                registry = ExperimentRegistry()
            meta = registry.get_experiment(experiment_id)
            if meta is not None and meta.concept_ids:
                return tuple(meta.concept_ids)
        except Exception:  # noqa: BLE001 - 证据生成不因注册表异常中断
            pass
        return self._default_concepts

    def _explain_observations(self, result: SimulationResult, norm: dict) -> tuple[str, ...]:
        """优先复用 M0.5 解释层的解读（单一解释源）, 异常时本地回退。"""
        try:
            if self._explainer is None:
                from core.learning.explanation import SimulationExplainer

                self._explainer = SimulationExplainer()
            resp = self._explainer.explain_result(result)
            if resp.ok and resp.key_points:
                return tuple(resp.key_points)
        except Exception:  # noqa: BLE001
            pass
        # 本地回退：最小组观察集
        return (
            f"波长 λ ≈ {norm.get('wavelength_mm', '—')} mm（λ = c/(n·f)）"
            if norm.get("wavelength_mm") is not None
            else "仿真完成, 产物已生成",
        )

    def _comparison_observations(
        self, before: dict, after: dict, changed: list[str]
    ) -> tuple[str, ...]:
        """比较观察：优先解释层 param_change, 异常时本地差分。"""
        try:
            if self._explainer is None:
                from core.learning.explanation import SimulationExplainer

                self._explainer = SimulationExplainer()
            resp = self._explainer.explain_param_change(before, after)
            if resp.ok and resp.key_points:
                return tuple(resp.key_points)
        except Exception:  # noqa: BLE001
            pass
        return tuple(
            f"{_PARAM_LABELS[key]}：{before[key]} → {after[key]}" for key in changed
        )
