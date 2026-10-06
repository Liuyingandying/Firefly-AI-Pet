# -*- coding: utf-8 -*-
"""均匀平面波传播模板（固定脚本, 人工预置）。

用法:
    python uniform_plane_wave.py --params <params.json> --out <out_dir>

只依赖 numpy / matplotlib / PIL；不触网络、不写临时目录之外的任何路径。
AST 备案: import 白名单 (numpy/matplotlib/PIL)；无 eval/exec/open/subprocess。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

import matplotlib

matplotlib.use("Agg")  # 无头渲染, 禁止任何 GUI 后端

import matplotlib.pyplot as plt  # noqa: E402
from PIL import Image  # noqa: E402

# ---------------------------------------------------------------- 常量

SPEED_OF_LIGHT = 299_792_458.0          # m/s
MEDIA = {"vacuum": 1.0, "air": 1.0003, "water": 1.333, "glass": 1.5}

FRAMES = 12
FRAME_MS = 100                          # 每帧 100ms → 循环 1.2s

ACCENT = "#9b50ff"                      # 雷紫
FIELD_COLOR = "#22d3ee"                 # 青色波线
BG = "white"


# ---------------------------------------------------------------- 工具

def _fail(message: str) -> None:
    print(f"TEMPLATE_ERROR: {message}", file=sys.stderr)
    raise SystemExit(2)


def _load_params(path: str) -> dict:
    try:
        params = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        _fail(f"params json unreadable: {exc}")
    if not isinstance(params, dict):
        _fail("params json must be an object")
    for key in ("frequency_ghz", "refractive_index", "polarization_deg", "amplitude"):
        if key not in params:
            _fail(f"params missing key: {key}")
    if not (isinstance(params["frequency_ghz"], (int, float)) and params["frequency_ghz"] > 0):
        _fail("frequency must be positive")
    return params


# ---------------------------------------------------------------- 渲染

def render_frame(ax, z_mm, wave, envelope, color: str, label: str) -> None:
    ax.clear()
    ax.plot(z_mm, wave, color=color, linewidth=2.0, label=label)
    ax.fill_between(z_mm, wave, 0, color=color, alpha=0.12)
    ax.set_ylim(-1.15, 1.15)
    ax.set_xlim(z_mm[0], z_mm[-1])
    ax.set_xlabel("z (mm)")
    ax.set_ylabel("E (norm)")
    ax.grid(True, alpha=0.25)
    ax.legend(loc="upper right", fontsize=8)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="uniform plane wave renderer")
    parser.add_argument("--params", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    params = _load_params(args.params)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    f_ghz = float(params["frequency_ghz"])
    n = float(params["refractive_index"])
    psi = math.radians(float(params["polarization_deg"]))
    e0 = float(params["amplitude"])

    lam_mm = SPEED_OF_LIGHT / (n * f_ghz * 1e9) * 1000.0
    z_mm = np.linspace(0.0, 3.0 * lam_mm, 1200)

    # 极化分解: E = E0·cos(ψ)·x̂ + E0·sin(ψ)·ŷ
    ex_amp = e0 * math.cos(psi)
    ey_amp = e0 * math.sin(psi)

    # ---- 1. electric_field.png: t=0 快照（极化分量 Ex/Ey 空间分布） ----
    # 极化分解 E = E0·cos(ψ)·x̂ + E0·sin(ψ)·ŷ：ψ 变化 → 两分量此消彼长,
    # PNG 像素随极化角可测变化（验收: pol=0° vs pol=90° 可分辨）。
    fig, ax = plt.subplots(figsize=(7.2, 4.0), dpi=130)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    phase0 = 2.0 * np.pi * z_mm / lam_mm
    ex = e0 * math.cos(psi) * np.cos(phase0)
    ey = e0 * math.sin(psi) * np.cos(phase0)
    ax.plot(z_mm, ex, color=FIELD_COLOR, linewidth=2.2,
            label=f"Ex (ψ = {math.degrees(psi):.0f}°)")
    ax.plot(z_mm, ey, color=ACCENT, linewidth=1.6, linestyle="--",
            label=f"Ey (ψ = {math.degrees(psi):.0f}°)")
    ax.axhline(0.0, color="#666", linewidth=0.8)
    ax.set_ylim(-1.2, 1.2)
    ax.set_title(
        f"Uniform Plane Wave · f = {f_ghz:.1f} GHz · λ = {lam_mm:.1f} mm (n = {n:.4f})"
    )
    ax.set_xlabel("z (mm)")
    ax.set_ylabel("E (V/m)")
    ax.grid(True, alpha=0.25)
    ax.legend(loc="upper right", fontsize=8)
    fig.tight_layout()
    fig.savefig(out_dir / "electric_field.png", facecolor=BG)
    plt.close(fig)

    # ---- 2. wave.gif: 12 帧传播动画（一个周期, 时间推进） ----
    frames_img: list[Image.Image] = []
    omega_frames = 2.0 * math.pi / FRAMES
    for i in range(FRAMES):
        phase = omega_frames * i
        fig, ax = plt.subplots(figsize=(7.2, 4.0), dpi=110)
        fig.patch.set_facecolor(BG)
        ax.set_facecolor(BG)
        wave = e0 * np.cos(2.0 * np.pi * z_mm / lam_mm - phase)
        ax.plot(z_mm, wave, color=FIELD_COLOR, linewidth=2.2)
        ax.fill_between(z_mm, wave, 0, color=FIELD_COLOR, alpha=0.12)
        # 波峰标记（跟随相位移动的可见参考点）
        peak_z = ((phase / (2.0 * np.pi)) % 1.0) * lam_mm
        ax.axvline(peak_z, color="#f59e0b", linewidth=1.2, linestyle="--", alpha=0.8)
        ax.set_ylim(-1.25, 1.25)
        ax.set_xlim(z_mm[0], z_mm[-1])
        ax.set_xlabel("z (mm)")
        ax.set_ylabel("E (norm)")
        ax.set_title(f"Uniform Plane Wave · frame {i + 1}/{FRAMES} · ψ = {math.degrees(psi):.0f}°")
        ax.grid(True, alpha=0.25)
        fig.tight_layout()
        fig.canvas.draw()
        buf = np.asarray(fig.canvas.buffer_rgba())[..., :3]
        plt.close(fig)
        frames_img.append(Image.fromarray(buf, "RGB"))

    frames_img[0].save(
        out_dir / "wave.gif",
        save_all=True,
        append_images=frames_img[1:],
        duration=FRAME_MS,
        loop=0,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
