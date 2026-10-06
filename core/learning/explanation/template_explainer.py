# -*- coding: utf-8 -*-
"""Template explainer — 模板规则解释器（M0.5 第一版, 零模型调用）。

纯函数集合：``ExplanationRequest → ExplanationResponse``。
规则来源：
- 物理量解读规则复用 ``core.learning.simulation.schema`` 的常量词表
  （MEDIA / F_RANGE_GHZ / AMP_RANGE）——只读 import, 不复制数值;
- 错误规则按 ``SimulationStatus`` 词表分支, 解析 runner 生成的 reason 文本。

AST 备案口径：仅 import 标准库 + core.learning.simulation（只读）;
无网络 / 无模型 / 无文件写入。
"""

from __future__ import annotations

import math

from core.learning.explanation.schema import (
    ExplanationKind,
    ExplanationRequest,
    ExplanationResponse,
)
from core.learning.simulation.result import SimulationStatus
from core.learning.simulation.schema import AMP_RANGE, F_RANGE_GHZ, MEDIA, SPEED_OF_LIGHT

_GENERATED_BY = "template_rules_v1"


# ---------------------------------------------------------------------------
# 参数归一化与派生物理量（接受 payload 键与 schema 键两种写法）
# ---------------------------------------------------------------------------

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


def _norm_params(raw: dict | None) -> dict:
    """归一化参数字典（未知键丢弃, 值不校验——规则侧各自守卫）。"""
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for key, value in raw.items():
        canon = _KEY_ALIASES.get(str(key).strip().lower())
        if canon is not None:
            out[canon] = value
    return out


def _refractive_index(norm: dict) -> float | None:
    medium = str(norm.get("medium", "") or "").strip().lower()
    return MEDIA.get(medium)


def _wavelength_mm(norm: dict) -> float | None:
    """λ = c/(n·f) 毫米; 参数缺失/非法返回 None（规则侧跳过该行）。"""
    n = _refractive_index(norm)
    try:
        f = float(norm.get("frequency"))
    except (TypeError, ValueError):
        return None
    if n is None or f <= 0 or math.isnan(f) or math.isinf(f):
        return None
    return SPEED_OF_LIGHT / (n * f * 1e9) * 1000.0


def _fmt(value: float | None, nd: int = 1, default: str = "—") -> str:
    return f"{value:.{nd}f}" if value is not None and math.isfinite(value) else default


# ---------------------------------------------------------------------------
# kind=result：实验结果解释
# ---------------------------------------------------------------------------

def _polarization_rule(psi: float) -> str:
    psi = psi % 360.0
    if abs(psi) < 1e-6 or abs(psi - 360.0) < 1e-6:
        return "纯 x 方向线极化（Ey = 0）：电场只沿 x̂ 振动"
    if abs(psi - 90.0) < 1e-6 or abs(psi - 270.0) < 1e-6:
        return "纯 y 方向线极化（Ex = 0）：电场只沿 ŷ 振动"
    if abs(psi - 45.0) < 1e-6 or abs(psi - 135.0) < 1e-6:
        return "等幅双分量线极化：Ex 与 Ey 幅值相等"
    return f"一般线极化：Ex 占比 cos ψ={math.cos(math.radians(psi)):.2f}，Ey 占比 sin ψ={math.sin(math.radians(psi)):.2f}"


def explain_result(request: ExplanationRequest) -> ExplanationResponse:
    result = request.result
    assert result is not None  # explainer 门面已校验
    payload = dict(result.params_payload or {})
    norm = _norm_params(payload)

    f = norm.get("frequency")
    n = _refractive_index(norm)
    lam = _wavelength_mm(norm)
    lam_vac = _wavelength_mm({"frequency": f, "medium": "vacuum"})
    medium = str(norm.get("medium", "—"))
    psi = norm.get("polarization", 0.0)
    amp = norm.get("amplitude", "—")

    title = result.display_name or "均匀平面波传播实验"
    lines = [
        f"本次仿真在 **{medium}（n = {_fmt(n, 4)}）** 中观察频率 "
        f"**{_fmt(f, 2) if isinstance(f, (int, float)) else '—'} GHz** 的均匀平面波。",
        f"- 波长 λ = c/(n·f) ≈ **{_fmt(lam)} mm**",
        f"- 电场振幅 E0 = {amp}，极化角 ψ = {_fmt(psi if isinstance(psi, (int, float)) else None)}°",
    ]
    key_points = [
        f"波长与频率成反比：f = {_fmt(f if isinstance(f, (int, float)) else None, 2)} GHz 时 λ ≈ {_fmt(lam)} mm",
        f"介质使波长缩短：同频率真空波长 λ₀ ≈ {_fmt(lam_vac)} mm，{medium} 中缩短为 1/n = 1/{_fmt(n, 3)}",
        _polarization_rule(psi if isinstance(psi, (int, float)) else 0.0),
        "electric_field.png 展示 t=0 时刻 Ex/Ey 空间分布；wave.gif 展示一个周期内波峰沿 +z 的传播（黄色虚线为波峰标记）",
    ]
    suggestions = [
        "增大频率或换更高折射率介质，观察 PNG 中波长变密",
        "把极化角调到 45°/90°，观察 Ex/Ey 两条曲线此消彼长",
    ]

    return ExplanationResponse(
        ok=True,
        kind=ExplanationKind.RESULT.value,
        generated_by=_GENERATED_BY,
        title=f"实验解读：{title}",
        summary="\n".join(lines),
        key_points=tuple(key_points),
        suggestions=tuple(suggestions),
        request_echo=request.echo(),
    )


# ---------------------------------------------------------------------------
# kind=param_change：参数变化解释
# ---------------------------------------------------------------------------

def _change_rule(key: str, before_norm: dict, after_norm: dict) -> str | None:
    """单参数变化 → 一条物理解释；无法量化时返回定性描述。"""
    b, a = before_norm.get(key), after_norm.get(key)
    try:
        if key == "frequency":
            fb, fa = float(b), float(a)
            lb = _wavelength_mm(before_norm)
            la = _wavelength_mm(after_norm)
            direction = "缩短" if fa > fb else "变长"
            return (
                f"频率 {_fmt(fb, 2)} → {_fmt(fa, 2)} GHz：波长{direction}，"
                f"λ {_fmt(lb)} → {_fmt(la)} mm（λ ∝ 1/f）"
            )
        if key == "medium":
            nb, na = _refractive_index(before_norm), _refractive_index(after_norm)
            lb, la = _wavelength_mm(before_norm), _wavelength_mm(after_norm)
            return (
                f"介质 {b}（n = {_fmt(nb, 3)}）→ {a}（n = {_fmt(na, 3)}）："
                f"折射率越大波长越短，λ {_fmt(lb)} → {_fmt(la)} mm"
            )
        if key == "polarization":
            pb, pa = float(b) % 360.0, float(a) % 360.0
            return (
                f"极化角 ψ {_fmt(pb)}° → {_fmt(pa)}°：{_polarization_rule(pa)}；"
                f"PNG 中 Ex/Ey 分量幅值随之重新分配"
            )
        if key == "amplitude":
            return f"振幅 E0 {_fmt(float(b), 2)} → {_fmt(float(a), 2)}：波峰高度等比变化，波长不变"
    except (TypeError, ValueError):
        return f"{_PARAM_LABELS.get(key, key)}：{b!r} → {a!r}（含非数值, 跳过定量分析）"
    return None


def explain_param_change(request: ExplanationRequest) -> ExplanationResponse:
    before = _norm_params(request.params_before)
    after = _norm_params(request.params_after)

    changed = [
        key for key in ("frequency", "medium", "polarization", "amplitude")
        if key in before and key in after and before[key] != after[key]
    ]
    if not before or not after:
        return ExplanationResponse(
            ok=False,
            kind=ExplanationKind.PARAM_CHANGE.value,
            generated_by=_GENERATED_BY,
            error="missing_params",
            request_echo=request.echo(),
        )
    if not changed:
        return ExplanationResponse(
            ok=True,
            kind=ExplanationKind.PARAM_CHANGE.value,
            generated_by=_GENERATED_BY,
            title="参数变化解读",
            summary="两次参数完全一致，结果差异仅来自运行时因素（如渲染时间）。",
            key_points=("未检测到 frequency/medium/polarization/amplitude 的变化",),
            request_echo=request.echo(),
        )

    lines = [f"检测到 {len(changed)} 项参数变化："]
    key_points = []
    for key in changed:
        rule = _change_rule(key, before, after)
        if rule:
            lines.append(f"- {rule}")
            key_points.append(rule)

    lb, la = _wavelength_mm(before), _wavelength_mm(after)
    if lb is not None and la is not None and abs(la - lb) > 1e-9:
        ratio = lb / la
        key_points.append(f"综合效果：波长变化比 λ_before/λ_after ≈ {ratio:.2f}")

    return ExplanationResponse(
        ok=True,
        kind=ExplanationKind.PARAM_CHANGE.value,
        generated_by=_GENERATED_BY,
        title="参数变化解读",
        summary="\n".join(lines),
        key_points=tuple(key_points),
        suggestions=(
            "用相同参数复跑一次确认差异只来自参数（runner 具有确定性）",
            "每次只改一个参数，便于归因",
        ),
        request_echo=request.echo(),
    )


# ---------------------------------------------------------------------------
# kind=error：错误原因解释
# ---------------------------------------------------------------------------

def _param_error_rule(reason: str) -> tuple[str, tuple[str, ...]]:
    """解析 runner 的 param_error reason → (原因解释, 修复建议)。"""
    if "未注册" in reason:
        return (
            "请求的 experiment_id 不在实验注册表中：注册表扫描 "
            "experiments/<domain>/<id>/experiment.yaml，未命中即拒绝。",
            ("检查 experiment_id 拼写（连字符/下划线）",
             "在 experiments/ 下新增 experiment.yaml 即可注册新实验"),
        )
    if "frequency" in reason:
        return (
            f"参数 frequency 非法：合法教学范围是 {F_RANGE_GHZ[0]}–{F_RANGE_GHZ[1]} GHz"
            f"（超出范围或非有限数值都会被拒绝）。",
            (f"把 frequency 调整到 [{F_RANGE_GHZ[0]}, {F_RANGE_GHZ[1]}] GHz 内",
             "ISM 常用教学频点：2.4 GHz / 5.8 GHz"),
        )
    if "amplitude" in reason:
        return (
            f"参数 amplitude 非法：合法归一化范围是 {AMP_RANGE[0]}–{AMP_RANGE[1]}。",
            (f"把 amplitude 调整到 [{AMP_RANGE[0]}, {AMP_RANGE[1]}]",),
        )
    if "medium" in reason:
        return (
            f"参数 medium 非法：只接受封闭词表 {', '.join(sorted(MEDIA))}。",
            ("从词表中选择介质；新介质需先扩展 schema.MEDIA（人工预置）",),
        )
    if "polarization" in reason:
        return (
            "参数 polarization 非法：必须是有限数值"
            "（超出 0–360° 会按模 360 归一, 非数值直接拒绝）。",
            ("传入数值, 例如 0 / 45 / 90",),
        )
    return (f"参数校验失败：{reason}", ("按 reason 中的字段名修正参数",))


def explain_error(request: ExplanationRequest) -> ExplanationResponse:
    result = request.result
    assert result is not None  # explainer 门面已校验
    if result.ok:
        return ExplanationResponse(
            ok=False,
            kind=ExplanationKind.ERROR.value,
            generated_by=_GENERATED_BY,
            error="result_ok_no_error",
            request_echo=request.echo(),
        )

    status = str(result.status)
    reason = result.reason or ""
    detail = (result.error_detail or "").strip()

    if status == SimulationStatus.PARAM_ERROR.value:
        cause, fixes = _param_error_rule(reason)
        title = "失败解读：参数被拒绝"
    elif status == SimulationStatus.TIMEOUT.value:
        cause = (
            f"模板渲染在时间预算内未完成（{reason or 'timeout'}）。"
            "渲染成本随帧数×分辨率增长, 默认 12 帧 GIF 在慢环境下可能触顶。"
        )
        fixes = ("增大 SimulationRunner(timeout_s=…)", "降低模板帧数/分辨率（人工预置侧）")
        title = "失败解读：渲染超时"
    elif status == SimulationStatus.EXECUTION_FAILED.value:
        cause = f"模板进程异常退出或产物缺失（{reason}）。"
        if detail:
            cause += f" 子进程 stderr 尾部：{detail}"
        fixes = ("确认 matplotlib 可用且使用 Agg 后端", "查看 error_detail 定位模板内错误")
        title = "失败解读：执行失败"
    elif status == SimulationStatus.SECURITY_REJECTED.value:
        cause = "模板未通过安全审查（AST 白名单校验拒绝执行）。"
        fixes = ("模板只能使用备案的 import 白名单（numpy/matplotlib/PIL）",)
        title = "失败解读：安全拒绝"
    else:
        cause = f"未知失败状态 {status!r}：{reason}"
        fixes = ("携带完整 reason 反馈给维护者",)
        title = "失败解读"

    return ExplanationResponse(
        ok=True,
        kind=ExplanationKind.ERROR.value,
        generated_by=_GENERATED_BY,
        title=title,
        summary=cause,
        key_points=(f"status = {status}", reason or "（无 reason 文本）"),
        suggestions=tuple(fixes),
        request_echo=request.echo(),
    )
