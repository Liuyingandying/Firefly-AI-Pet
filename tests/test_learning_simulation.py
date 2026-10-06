"""M0.1 均匀平面波仿真核心验收测试。

覆盖：默认参数运行 / 参数变化影响结果 / 非法参数拒绝 / 输出文件存在。
隔离：全部在临时目录运行，不触真实用户数据。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PIL import Image

from core.learning.simulation.schema import (
    MEDIA,
    SimulationParams,
    SimulationParamError,
)
from core.learning.simulation.runner import SimulationRunner


@pytest.fixture(scope="module")
def runner():
    return SimulationRunner(timeout_s=15)


# ---------------------------------------------------------------------------
# schema 校验
# ---------------------------------------------------------------------------

def test_default_params_valid():
    params = SimulationParams()
    assert params.frequency == 2.4
    assert params.medium == "vacuum"
    assert params.polarization == 0.0
    assert params.amplitude == 0.8


@pytest.mark.parametrize("field,value", [
    ("frequency", 0.05),   # 低于下限
    ("frequency", 50.0),   # 高于上限
    ("frequency", "abc"),  # 非数值
    ("frequency", float("nan")),
    ("amplitude", 0.01),   # 低于下限
    ("amplitude", 5.0),    # 高于上限
])
def test_invalid_frequency_rejected(field, value):
    with pytest.raises(SimulationParamError):
        SimulationParams(**{field: value})


def test_invalid_medium_rejected():
    with pytest.raises(SimulationParamError, match="medium"):
        SimulationParams(medium="metal")


def test_medium_case_normalized():
    params = SimulationParams(medium="Vacuum")
    assert params.medium == "vacuum"


def test_polarization_mod_360():
    params = SimulationParams(polarization=370.0)
    assert params.polarization == 10.0


# ---------------------------------------------------------------------------
# 物理量
# ---------------------------------------------------------------------------

def test_wavelength_formula():
    params = SimulationParams(frequency=2.4, medium="vacuum")
    lam_mm = 299_792_458.0 / (1.0 * 2.4e9) * 1000.0
    assert params.wavelength_mm == pytest.approx(lam_mm, rel=1e-6)


def test_medium_changes_wavelength():
    vac = SimulationParams(frequency=2.4, medium="vacuum")
    glass = SimulationParams(frequency=2.4, medium="glass")
    assert glass.wavelength_mm < vac.wavelength_mm  # n 越大 λ 越短
    assert glass.refractive_index == 1.5


# ---------------------------------------------------------------------------
# runner 端到端
# ---------------------------------------------------------------------------

def test_runner_default_params_produce_outputs(runner, tmp_path):
    out = tmp_path / "out_default"
    result = runner.run(SimulationParams(), out_dir=out)
    assert result.ok is True
    assert result.png_path is not None and result.png_path.is_file()
    assert result.gif_path is not None and result.gif_path.is_file()
    assert result.md_path is not None and result.md_path.is_file()
    # PNG 非空且为有效图像
    im = Image.open(result.png_path)
    assert im.size[0] > 0 and im.size[1] > 0
    # GIF 非空且可解析
    gif = Image.open(result.gif_path)
    assert gif.n_frames >= 8
    assert gif.format == "GIF"
    # 实验说明存在且含参数
    md_text = result.md_path.read_text(encoding="utf-8")
    assert "均匀平面波" in md_text
    assert "GHz" in md_text


def test_runner_out_dir_default_creates_temp(runner):
    result = runner.run(SimulationParams())
    assert result.out_dir is not None
    assert result.out_dir.is_dir()
    assert result.png_path.is_file()


def test_runner_param_variation_changes_output(runner, tmp_path):
    import numpy as np
    from PIL import Image

    out0 = tmp_path / "pol0"; out0.mkdir()
    out90 = tmp_path / "pol90"; out90.mkdir()

    r0 = runner.run(SimulationParams(polarization=0.0, amplitude=1.0), out_dir=out0)
    r90 = runner.run(SimulationParams(polarization=90.0, amplitude=1.0), out_dir=out90)
    assert r0.ok and r90.ok

    # 极化角 0° vs 90° → PNG 像素可分辨
    im0 = np.asarray(Image.open(r0.png_path).convert("L"), dtype=float)
    im90 = np.asarray(Image.open(r90.png_path).convert("L"), dtype=float)
    assert im0.shape == im90.shape
    assert np.abs(im0 - im90).mean() > 0.1  # 可测差异（坐标轴/网格贡献基线相似度）


# ---------------------------------------------------------------------------
# 异常与边界
# ---------------------------------------------------------------------------

def test_runner_invalid_medium_rejected_at_schema(runner):
    """medium='metal' 在 schema 构造时即拒绝（不进入沙箱）。"""
    with pytest.raises(SimulationParamError, match="medium"):
        SimulationParams(medium="metal")


def test_runner_never_raises_on_any_params(runner, tmp_path):
    import math
    for kwargs in (
        {"frequency": float("nan")},
        {"medium": 123},
        {"polarization": None},
        {"amplitude": "x"},
    ):
        try:
            runner.run(SimulationParams(**kwargs), out_dir=tmp_path)
        except (SimulationParamError, TypeError):
            pass  # schema 层拒绝即符合预期
        except Exception as exc:
            pytest.fail(f"非 SchemaError 异常逃逸: {kwargs} -> {exc}")


def test_runner_timeout_configuration(runner):
    """超时配置可注入（默认 15s，此处仅确认属性暴露）。"""
    assert runner._timeout_s == 15
    custom = SimulationRunner(timeout_s=2)
    assert custom._timeout_s == 2
