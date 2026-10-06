# -*- coding: utf-8 -*-
"""M0.5 Explanation Layer 验收测试。

覆盖：success 结果解释 / invalid_params 解释 / timeout 解释 / JSON 序列化,
另加参数变化解释与非法请求拒绝。
隔离：真实 runner 只跑一次（success 场景）, 其余直接构造 SimulationResult;
不触真实用户数据、不调用任何模型。
"""

from __future__ import annotations

import json

import pytest

from core.learning.explanation import (
    ExplanationKind,
    ExplanationRequest,
    SimulationExplainer,
)
from core.learning.simulation.result import SimulationStatus
from core.learning.simulation.runner import SimulationParams, SimulationResult, SimulationRunner


@pytest.fixture(scope="module")
def runner():
    return SimulationRunner(timeout_s=20)


@pytest.fixture(scope="module")
def explainer():
    return SimulationExplainer()


# ---------------------------------------------------------------------------
# 1. success 结果解释
# ---------------------------------------------------------------------------

def test_explain_success_result(runner, explainer, tmp_path):
    result = runner.run(SimulationParams(), out_dir=tmp_path)
    assert result.ok is True

    resp = explainer.explain_result(result)
    assert resp.ok is True
    assert resp.kind == ExplanationKind.RESULT.value
    assert resp.generated_by == "template_rules_v1"
    assert "均匀平面波" in resp.title
    # 参数值回读：默认 2.4 GHz / vacuum / λ ≈ 124.9 mm
    assert "2.40" in resp.summary
    assert "vacuum" in resp.summary
    assert resp.key_points and resp.suggestions
    assert any("λ" in p for p in resp.key_points)
    assert resp.request_echo["status"] == "completed"


def test_explain_result_rejects_failed_result(explainer):
    """ok=False 的结果误入 result 场景 → 引导改用 error 场景。"""
    bad = SimulationResult(ok=False, status="param_error", reason="param_error: x")
    resp = explainer.explain(ExplanationRequest(kind=ExplanationKind.RESULT, result=bad))
    assert resp.ok is False
    assert resp.error == "result_not_ok_use_error_kind"


# ---------------------------------------------------------------------------
# 2. invalid_params 解释
# ---------------------------------------------------------------------------

def test_explain_invalid_params(runner, explainer, tmp_path):
    result = runner.run(SimulationParams(frequency=999), out_dir=tmp_path)
    assert result.ok is False
    assert result.status == SimulationStatus.PARAM_ERROR.value

    resp = explainer.explain_error(result)
    assert resp.ok is True
    assert resp.kind == ExplanationKind.ERROR.value
    assert "frequency" in resp.summary
    assert "0.1–10.0 GHz" in resp.summary         # 合法范围进入解释文本
    assert any("[0.1, 10.0]" in s for s in resp.suggestions)  # 修复建议含可执行范围
    assert resp.request_echo["status"] == "param_error"


def test_explain_unregistered_experiment(explainer):
    result = SimulationResult(
        ok=False, status="param_error",
        reason="experiment_id='ghost-experiment' 未注册",
    )
    resp = explainer.explain_error(result)
    assert resp.ok is True
    assert "未注册" in resp.summary or "注册表" in resp.summary
    assert any("experiment_id" in s for s in resp.suggestions)


# ---------------------------------------------------------------------------
# 3. timeout 解释
# ---------------------------------------------------------------------------

def test_explain_timeout(explainer):
    result = SimulationResult(
        ok=False, status=SimulationStatus.TIMEOUT.value,
        reason="timeout after 15s", error_detail="timeout after 15s",
    )
    resp = explainer.explain_error(result)
    assert resp.ok is True
    assert "超时" in resp.title or "timeout" in resp.summary
    assert any("timeout_s" in s for s in resp.suggestions)


def test_explain_execution_failed_includes_stderr_tail(explainer):
    result = SimulationResult(
        ok=False, status=SimulationStatus.EXECUTION_FAILED.value,
        reason="template exit 2", error_detail="TEMPLATE_ERROR: params missing key",
    )
    resp = explainer.explain_error(result)
    assert resp.ok is True
    assert "TEMPLATE_ERROR" in resp.summary
    assert any("Agg" in s or "error_detail" in s for s in resp.suggestions)


# ---------------------------------------------------------------------------
# 4. 参数变化解释
# ---------------------------------------------------------------------------

def test_explain_param_change_frequency(explainer):
    before = {"frequency_ghz": 2.4, "medium": "vacuum",
              "polarization_deg": 0.0, "amplitude": 0.8}
    after = {"frequency_ghz": 5.8, "medium": "vacuum",
             "polarization_deg": 0.0, "amplitude": 0.8}
    resp = explainer.explain_param_change(before, after)
    assert resp.ok is True
    assert "2.40" in resp.summary and "5.80" in resp.summary
    assert any("缩短" in p for p in resp.key_points)   # f ↑ → λ ↓
    # λ_before / λ_after = 5.8 / 2.4 ≈ 2.42
    assert any("2.42" in p for p in resp.key_points)


def test_explain_param_change_medium_and_polarization(explainer):
    before = {"frequency": 2.4, "medium": "vacuum", "polarization": 0.0, "amplitude": 0.8}
    after = {"frequency": 2.4, "medium": "glass", "polarization": 90.0, "amplitude": 0.8}
    resp = explainer.explain_param_change(before, after)
    assert resp.ok is True
    assert "glass" in resp.summary
    assert any("1.500" in p for p in resp.key_points)  # 折射率变化被量化
    assert any("纯 y" in p for p in resp.key_points)   # ψ=90° 极化规则


def test_explain_param_change_no_change(explainer):
    same = {"frequency": 2.4, "medium": "vacuum", "polarization": 0.0, "amplitude": 0.8}
    resp = explainer.explain_param_change(same, dict(same))
    assert resp.ok is True
    assert "完全一致" in resp.summary


# ---------------------------------------------------------------------------
# 5. JSON 序列化
# ---------------------------------------------------------------------------

def test_response_json_serialization(runner, explainer, tmp_path):
    result = runner.run(SimulationParams(), out_dir=tmp_path)
    resp = explainer.explain_result(result)

    d = resp.to_dict()
    assert isinstance(d, dict)
    text = json.dumps(d, ensure_ascii=False)   # Path/Enum 泄漏会在此抛错
    parsed = json.loads(text)
    assert parsed["ok"] is True
    assert parsed["kind"] == "result"
    assert parsed["generated_by"] == "template_rules_v1"

    text2 = resp.to_json()
    assert json.loads(text2)["title"] == resp.title


def test_error_response_json_serialization(explainer):
    resp = explainer.explain_error(
        SimulationResult(ok=False, status="timeout", reason="timeout after 15s")
    )
    parsed = json.loads(resp.to_json())
    assert parsed["ok"] is True
    assert parsed["kind"] == "error"


# ---------------------------------------------------------------------------
# 6. 非法请求拒绝（门面校验）
# ---------------------------------------------------------------------------

def test_missing_result_rejected(explainer):
    resp = explainer.explain(ExplanationRequest(kind=ExplanationKind.RESULT))
    assert resp.ok is False
    assert resp.error == "missing_result"


def test_param_change_missing_params_rejected(explainer):
    resp = explainer.explain_param_change({"frequency": 2.4}, {})
    assert resp.ok is False
    assert resp.error == "missing_params"
