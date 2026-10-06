# -*- coding: utf-8 -*-
"""均匀平面波仿真参数 schema 与校验（M0.1）。

参数在 dataclass 构造时即校验：非法输入在进入沙箱**之前**就被拒绝。
介质折射率表内置（教学基准值），不由外部注入。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

SPEED_OF_LIGHT = 299_792_458.0  # m/s

#: 教学介质表：折射率 n（基准值，2.4 GHz 非色散近似）
MEDIA: dict[str, float] = {
    "vacuum": 1.0,
    "air": 1.0003,
    "water": 1.333,
    "glass": 1.5,
}

#: 允许的频率范围（GHz）——覆盖 ISM/教学常用段
F_RANGE_GHZ = (0.1, 10.0)
#: 极化角范围（度），超出按模 360 归一
POLARIZATION_MOD = 360.0
#: 归一化振幅范围
AMP_RANGE = (0.05, 1.0)


class SimulationParamError(ValueError):
    """参数非法（进入沙箱前即拒绝）。"""


def _require_number(name: str, value, lo: float, hi: float, *, clamp: bool = False) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise SimulationParamError(f"{name} 必须是数值, 得到 {value!r}") from None
    if math.isnan(num) or math.isinf(num):
        raise SimulationParamError(f"{name} 必须是有限数值, 得到 {value!r}")
    if clamp:
        return max(lo, min(hi, num))
    if not (lo <= num <= hi):
        raise SimulationParamError(f"{name}={num} 超出允许范围 [{lo}, {hi}]")
    return num


@dataclass(frozen=True)
class SimulationParams:
    """均匀平面波仿真参数（全部字段构造时校验）。

    frequency      仿真频率 (GHz)
    medium         介质（折射率查 MEDIA 表）
    polarization   线极化角 (度, 0–360, 模 360 归一)
    amplitude      归一化电场振幅 (0.05–1.0)
    """

    frequency: float = 2.4
    medium: str = "vacuum"
    polarization: float = 0.0
    amplitude: float = 0.8

    def __post_init__(self) -> None:
        f = _require_number("frequency", self.frequency, *F_RANGE_GHZ)
        object.__setattr__(self, "frequency", f)

        medium = str(self.medium or "").strip().lower()
        if medium not in MEDIA:
            raise SimulationParamError(
                f"medium 非法: {self.medium!r}（可选: {', '.join(sorted(MEDIA))}）"
            )
        object.__setattr__(self, "medium", medium)

        # 极化角按模 360 归一（等价方向）
        pol = _require_number("polarization", self.polarization, -1e9, 1e9)
        object.__setattr__(self, "polarization", pol % POLARIZATION_MOD)

        amp = _require_number("amplitude", self.amplitude, *AMP_RANGE)
        object.__setattr__(self, "amplitude", amp)

    # -- 派生物理量 ---------------------------------------------------------

    @property
    def refractive_index(self) -> float:
        return MEDIA[self.medium]

    @property
    def wavelength_mm(self) -> float:
        """介质内波长 λ = c / (n·f)，毫米单位。"""
        lam_m = SPEED_OF_LIGHT / (self.refractive_index * self.frequency * 1e9)
        return lam_m * 1000.0

    @property
    def period_ns(self) -> float:
        """周期 T = 1/f（GHz → ns）。"""
        return 1.0 / self.frequency

    @property
    def wave_number_rad_per_mm(self) -> float:
        """β = 2π/λ（1/mm）。"""
        return 2.0 * math.pi / self.wavelength_mm

    def to_payload(self) -> dict:
        """传给模板脚本的完整参数载荷（含派生量, 模板侧仍可自校验）。"""
        return {
            "frequency_ghz": self.frequency,
            "medium": self.medium,
            "refractive_index": self.refractive_index,
            "polarization_deg": self.polarization,
            "amplitude": self.amplitude,
            "wavelength_mm": round(self.wavelength_mm, 4),
        }
