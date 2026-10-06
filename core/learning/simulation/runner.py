# -*- coding: utf-8 -*-
"""SimulationRunner — 均匀平面波仿真执行器（M0.1 兼容 + M0.4 注册表集成）。

双接口（并存, 向后兼容）:

M0.1 直接参数接口:
    runner = SimulationRunner()
    result = runner.run(SimulationParams(...))          # out_dir 缺省 → 临时目录
    result.png_path / gif_path / md_path / out_dir      # Path 对象

M0.4 注册表接口:
    result = runner.run_experiment("uniform-plane-wave", out_dir=tmp_path)
    result.status ∈ SimulationStatus（completed/param_error/execution_failed/…）

安全边界: 模板脚本固定（人工预置, AST 白名单备案）, 本模块只把参数 JSON
传给子进程渲染; 任何非法参数在进入子进程之前就被拒绝或返回 param_error。
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.learning.simulation.result import SimulationStatus
from core.learning.simulation.schema import (
    AMP_RANGE,
    F_RANGE_GHZ,
    MEDIA,
    SPEED_OF_LIGHT,
)

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_DEFAULT_TEMPLATE = _TEMPLATES_DIR / "uniform_plane_wave.py"
_DEFAULT_EXPERIMENTS_DIR = Path(__file__).resolve().parents[3] / "experiments"


# ---------------------------------------------------------------------------
# SimulationParams（宽松构造版, M0.4 直接参数入口）
# ---------------------------------------------------------------------------
# 与 schema.SimulationParams 字段鸭子兼容; 范围校验延迟到 runner.run() 运行时
# 以 SimulationResult(param_error) 返回而非抛异常。

@dataclass(frozen=True)
class SimulationParams:
    """仿真参数（构造时只做类型/词表校验, 范围校验在 runner 运行时）。"""

    frequency: float = 2.4       # GHz
    medium: str = "vacuum"
    polarization: float = 0.0    # 度
    amplitude: float = 0.8

    def __post_init__(self) -> None:
        if isinstance(self.frequency, bool) or not isinstance(self.frequency, (int, float)):
            if not (isinstance(self.frequency, float) and math.isfinite(self.frequency)):
                raise ValueError(f"frequency 必须为正数, 得到 {self.frequency!r}")
        if isinstance(self.frequency, (int, float)) and self.frequency <= 0:
            raise ValueError(f"frequency 必须为正数, 得到 {self.frequency}")
        medium = str(self.medium or "").strip().lower()
        if medium not in MEDIA:
            raise ValueError(
                f"medium 非法: {self.medium!r}（可选: {', '.join(sorted(MEDIA))}）"
            )
        if not isinstance(self.polarization, (int, float)) or isinstance(
            self.polarization, bool
        ):
            raise ValueError(f"polarization 必须是数值, 得到 {self.polarization!r}")
        if not isinstance(self.amplitude, (int, float)) or isinstance(
            self.amplitude, bool
        ):
            raise ValueError(f"amplitude 必须是数值, 得到 {self.amplitude!r}")

    @property
    def refractive_index(self) -> float:
        return MEDIA[str(self.medium).strip().lower()]

    @property
    def wavelength_mm(self) -> float:
        return SPEED_OF_LIGHT / (self.refractive_index * self.frequency * 1e9) * 1000.0

    def to_payload(self) -> dict:
        return {
            "frequency_ghz": self.frequency,
            "medium": self.medium,
            "refractive_index": self.refractive_index,
            "polarization_deg": self.polarization,
            "amplitude": self.amplitude,
            "wavelength_mm": round(self.wavelength_mm, 4),
        }


# ---------------------------------------------------------------------------
# SimulationResult（权威定义; Path 字段保持 Path, to_dict() 序列化为 str）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SimulationResult:
    """一次仿真执行的结构化结果（M0.1/M0.4 共用）。"""

    ok: bool = True
    reason: str = ""
    status: str = SimulationStatus.COMPLETED.value
    experiment_id: str = ""
    display_name: str = ""
    run_id: str = ""
    out_dir: Path | str = ""
    png_path: Path | str = ""
    gif_path: Path | str = ""
    md_path: Path | str = ""
    elapsed_ms: int = 0
    params_payload: dict = field(default_factory=dict)
    error_detail: str = ""

    def to_dict(self) -> dict:
        """JSON 安全字典（Path → str, status 为纯字符串）。"""
        return {
            "ok": self.ok,
            "reason": self.reason,
            "status": self.status,
            "experiment_id": self.experiment_id,
            "display_name": self.display_name,
            "run_id": self.run_id,
            "out_dir": str(self.out_dir),
            "png_path": str(self.png_path),
            "gif_path": str(self.gif_path),
            "md_path": str(self.md_path),
            "elapsed_ms": self.elapsed_ms,
            "params_payload": dict(self.params_payload),
            "error_detail": self.error_detail,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


# ---------------------------------------------------------------------------
# SimulationRunner
# ---------------------------------------------------------------------------

class SimulationRunner:
    """均匀平面波仿真执行器（模板子进程 + 注册表查找）。"""

    def __init__(
        self,
        python: str | None = None,
        *,
        timeout_s: int = 15,
        experiments_dir: Path | None = None,
    ):
        self._python = python or sys.executable
        self._timeout_s = max(timeout_s, 1)
        self._templates_dir = _TEMPLATES_DIR
        self._experiments_dir = (
            Path(experiments_dir) if experiments_dir is not None
            else _DEFAULT_EXPERIMENTS_DIR
        )

    # -- M0.1 直接参数接口 ---------------------------------------------------

    def run(
        self,
        params: Any,
        *,
        out_dir: Path | None = None,
        template: Path | None = None,
    ) -> SimulationResult:
        """params → 运行时校验 → 子进程渲染 → 产物校验 → SimulationResult。

        接受 schema.SimulationParams（严格版）与本模块 SimulationParams
        （宽松版）——鸭子类型读取四个同名字段。任何非预期异常都被收敛为
        ``execution_failed`` 结果, 不向调用方逃逸。
        """
        t0 = time.monotonic()
        run_id = uuid.uuid4().hex[:12]
        target = Path(out_dir) if out_dir is not None else Path(
            tempfile.mkdtemp(prefix="firefly_sim_")
        )
        template_file = Path(template) if template is not None else _DEFAULT_TEMPLATE

        try:
            return self._run_impl(
                params, target, template_file, t0, run_id
            )
        except SimulationResult:  # pragma: no cover - 内部控制流保留
            raise
        except Exception as exc:  # noqa: BLE001 - 收敛为结果而非逃逸
            return self._failed(
                SimulationStatus.EXECUTION_FAILED,
                f"execution_failed:{exc}",
                run_id=run_id,
                elapsed_ms=int((time.monotonic() - t0) * 1000),
            )

    def _run_impl(
        self,
        params: Any,
        target: Path,
        template_file: Path,
        t0: float,
        run_id: str,
    ) -> SimulationResult:
        def elapsed() -> int:
            return int((time.monotonic() - t0) * 1000)

        # ---- 1. 运行时参数校验（宽松构造对象的最后防线） -------------------
        err = self._validate(params)
        if err is not None:
            return self._failed(
                SimulationStatus.PARAM_ERROR, err, run_id=run_id, elapsed_ms=elapsed()
            )

        payload = {
            "frequency_ghz": float(params.frequency),
            "medium": str(params.medium).strip().lower(),
            "refractive_index": MEDIA[str(params.medium).strip().lower()],
            "polarization_deg": float(params.polarization),
            "amplitude": float(params.amplitude),
            "wavelength_mm": round(
                SPEED_OF_LIGHT
                / (MEDIA[str(params.medium).strip().lower()] * float(params.frequency) * 1e9)
                * 1000.0,
                4,
            ),
        }

        # ---- 2. 输出目录 + 模板存在性 --------------------------------------
        target.mkdir(parents=True, exist_ok=True)
        if not template_file.is_file():
            return self._failed(
                SimulationStatus.EXECUTION_FAILED,
                f"template 未找到: {template_file.name}",
                run_id=run_id,
                elapsed_ms=elapsed(),
            )

        # ---- 3. 参数落盘 ---------------------------------------------------
        params_file = target / "params.json"
        params_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # ---- 4. 子进程执行模板（超时/非零退出 → 失败结果） ------------------
        env = os.environ.copy()
        env.setdefault("MPLBACKEND", "Agg")
        try:
            proc = subprocess.run(
                [self._python, str(template_file),
                 "--params", str(params_file), "--out", str(target)],
                capture_output=True,
                timeout=self._timeout_s,
                env=env,
                cwd=str(target),
            )
        except subprocess.TimeoutExpired:
            return self._failed(
                SimulationStatus.TIMEOUT,
                f"timeout after {self._timeout_s}s",
                run_id=run_id,
                elapsed_ms=elapsed(),
            )
        if proc.returncode != 0:
            tail = (proc.stderr or b"").decode("utf-8", "replace").strip()[-300:]
            return self._failed(
                SimulationStatus.EXECUTION_FAILED,
                f"template exit {proc.returncode}",
                run_id=run_id,
                elapsed_ms=elapsed(),
                error_detail=tail,
            )

        # ---- 5. 产物校验 ---------------------------------------------------
        png_path = target / "electric_field.png"
        gif_path = target / "wave.gif"
        missing = [p.name for p in (png_path, gif_path) if not p.is_file()]
        if missing:
            return self._failed(
                SimulationStatus.EXECUTION_FAILED,
                f"产物缺失: {', '.join(missing)}",
                run_id=run_id,
                elapsed_ms=elapsed(),
            )

        # ---- 6. 实验说明 ----------------------------------------------------
        md_path = self._write_readme(target, payload, display_name="")

        return SimulationResult(
            ok=True,
            status=SimulationStatus.COMPLETED.value,
            run_id=run_id,
            out_dir=target,
            png_path=png_path,
            gif_path=gif_path,
            md_path=md_path,
            elapsed_ms=elapsed(),
            params_payload=payload,
        )

    # -- M0.4 注册表接口 -------------------------------------------------------

    def run_experiment(
        self,
        experiment_id: str,
        *,
        params_override: dict | None = None,
        out_dir: Path | None = None,
    ) -> SimulationResult:
        """experiment_id → 注册表查找 → 默认参数合并 → run()。"""
        from core.learning.simulation.registry import ExperimentRegistry

        registry = ExperimentRegistry([self._experiments_dir])
        meta = registry.get_experiment(experiment_id)
        if meta is None:
            return self._failed(
                SimulationStatus.PARAM_ERROR,
                f"experiment_id={experiment_id!r} 未注册",
            )

        template_file = self._templates_dir / f"{meta.template_id}.py"
        merged: dict = dict(meta.default_params or {})
        if params_override:
            merged.update(params_override)

        try:
            params = SimulationParams(
                frequency=merged.get("frequency", 2.4),
                medium=str(merged.get("medium", "vacuum")),
                polarization=merged.get("polarization", 0.0),
                amplitude=merged.get("amplitude", 0.8),
            )
        except (ValueError, TypeError) as exc:
            return self._failed(
                SimulationStatus.PARAM_ERROR, f"param_error:{exc}"
            )

        result = self.run(params, out_dir=out_dir, template=template_file)
        if result.ok:
            result = dataclasses.replace(
                result,
                experiment_id=meta.experiment_id,
                display_name=meta.display_name,
            )
        return result

    # -- 内部工具 --------------------------------------------------------------

    @staticmethod
    def _validate(params: Any) -> str | None:
        """运行时范围校验; 返回错误原因或 None（通过）。"""
        try:
            f = float(params.frequency)
        except (TypeError, ValueError):
            return f"param_error: frequency 必须是数值, 得到 {params.frequency!r}"
        if math.isnan(f) or math.isinf(f):
            return f"param_error: frequency 必须是有限数值, 得到 {params.frequency!r}"
        if not (F_RANGE_GHZ[0] <= f <= F_RANGE_GHZ[1]):
            return (
                f"param_error: frequency={f} 超出允许范围 "
                f"[{F_RANGE_GHZ[0]}, {F_RANGE_GHZ[1]}]"
            )
        medium = str(getattr(params, "medium", "") or "").strip().lower()
        if medium not in MEDIA:
            return f"param_error: medium 非法: {medium!r}"
        try:
            pol = float(params.polarization)
            amp = float(params.amplitude)
        except (TypeError, ValueError) as exc:
            return f"param_error: {exc}"
        if math.isnan(pol) or math.isnan(amp) or math.isinf(amp):
            return "param_error: polarization/amplitude 必须是有限数值"
        if not (AMP_RANGE[0] <= amp <= AMP_RANGE[1]):
            return (
                f"param_error: amplitude={amp} 超出允许范围 "
                f"[{AMP_RANGE[0]}, {AMP_RANGE[1]}]"
            )
        return None

    @staticmethod
    def _failed(
        status: SimulationStatus,
        reason: str,
        *,
        run_id: str = "",
        elapsed_ms: int = 0,
        error_detail: str = "",
    ) -> SimulationResult:
        return SimulationResult(
            ok=False,
            reason=reason,
            status=status.value if isinstance(status, SimulationStatus) else str(status),
            run_id=run_id,
            elapsed_ms=elapsed_ms,
            error_detail=error_detail,
        )

    @staticmethod
    def _write_readme(target: Path, payload: dict, *, display_name: str) -> Path:
        title = display_name or "均匀平面波传播实验"
        md_path = target / "experiment.md"
        md_path.write_text(
            f"# {title}\n\n"
            f"| 参数 | 值 |\n|---|---|\n"
            f"| 频率 f | {payload['frequency_ghz']:.2f} GHz |\n"
            f"| 介质折射率 n | {payload['refractive_index']:.4f} |\n"
            f"| 极化角 ψ | {payload['polarization_deg']:.1f}° |\n"
            f"| 振幅 E0 | {payload['amplitude']:.2f} |\n"
            f"| 波长 λ | {payload['wavelength_mm']:.1f} mm |\n\n"
            f"## 观察要点\n\n"
            f"- 波长 λ = c/(n·f)：介质折射率越大，波长越短\n"
            f"- 极化角 ψ 决定电场振动方向（Ex/Ey 分量此消彼长）\n"
            f"- 电场幅值 E0 决定波峰高度\n",
            encoding="utf-8",
        )
        return md_path
