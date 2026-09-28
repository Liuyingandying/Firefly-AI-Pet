# -*- coding: utf-8 -*-
"""RVCConverter: 将官方 RVC 推理链封装为可常驻的进程内转换器。

- 模型只加载一次, 之后每次 convert 只做推理 (服务化必需)。
- 通过设置 weight_root/rmvpe_root 环境变量复用官方路径解析逻辑, 不修改引擎代码。
- 线程安全: 推理段用全局锁串行 (GPU 单卡)。
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np
import soundfile as sf
import yaml

logger = logging.getLogger(__name__)

MODULE_ROOT = Path(__file__).resolve().parent.parent
ENGINE_DIRNAME = "Retrieval-based-Voice-Conversion-WebUI-main"


@dataclass
class SpeakerSpec:
    name: str
    model: Path
    index: Optional[Path] = None
    index_rate: float = 0.75
    f0_up_key: int = 0
    rms_mix_rate: float = 1.0
    protect: float = 0.33
    resample_sr: int = 0
    display_name: str = ""

    def resolved(self, module_root: Path) -> "SpeakerSpec":
        resolve = lambda p: p if p.is_absolute() else (module_root / p)  # noqa: E731
        self.model = resolve(self.model).resolve()
        self.index = resolve(self.index).resolve() if self.index else None
        return self


@dataclass
class ConvertResult:
    audio: np.ndarray
    sample_rate: int
    seconds: float
    speaker: str
    status: str = "成功"


class RVCConverter:
    """进程内 RVC 推理封装。with 语义: 进入时按需加载引擎与说话人。"""

    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = Path(config_path) if config_path else MODULE_ROOT / "config.yaml"
        with open(self.config_path, "r", encoding="utf-8") as f:
            self.cfg = yaml.safe_load(f)

        self.engine_root = (MODULE_ROOT / self.cfg["engine"].get("engine_root", f"vendor/{ENGINE_DIRNAME}")).resolve()
        if not self.engine_root.is_dir():
            raise FileNotFoundError(f"RVC 引擎目录不存在: {self.engine_root}")
        rmvpe_root = (MODULE_ROOT / self.cfg["engine"].get("rmvpe_root", "models")).resolve()

        # 必须在 import 引擎模块之前设置 (官方代码用 setdefault 读取)
        os.environ.setdefault("rmvpe_root", str(rmvpe_root))

        if str(self.engine_root) not in sys.path:
            sys.path.insert(0, str(self.engine_root))
        os.chdir(self.engine_root)  # 官方 CLI 同款行为: i18n/资产按仓库根解析

        self.f0_method = self.cfg["engine"].get("f0_method", "rmvpe")
        self._lock = threading.Lock()
        self._vc = None
        self._loaded: Dict[str, object] = {}
        self.speakers: Dict[str, SpeakerSpec] = {}

    # ---------------- 引擎加载 ----------------

    def _ensure_engine(self):
        if self._vc is not None:
            return
        from configs.config import Config  # noqa: PLC0415  官方引擎模块
        from infer.vc.modules import VC  # noqa: PLC0415

        argv = sys.argv
        sys.argv = [argv[0]]  # Config 会解析 argv, 官方 cli.py 同款保护
        try:
            self._config = Config()
        finally:
            sys.argv = argv

        self._vc = VC(self._config)
        logger.info("RVC 引擎就绪: device=%s dtype=%s", self._config.device, self._config.dtype)

    def load_speaker(self, name: str) -> SpeakerSpec:
        """加载/切换说话人模型 (幂等)。"""
        self._ensure_engine()
        if name in self._loaded:
            return self._loaded[name]
        if name not in self.speakers:
            raise KeyError(f"未注册的说话人: {name}, 可用: {list(self.speakers)}")
        spec = self.speakers[name]
        if not spec.model.is_file():
            raise FileNotFoundError(f"说话人 {name} 模型不存在: {spec.model}")
        os.environ["weight_root"] = str(spec.model.parent)
        t0 = time.perf_counter()
        self._vc.get_vc(spec.model.name)
        self._loaded.clear()  # VC 单实例一次只持有一个 net_g
        self._loaded[name] = spec
        logger.info("说话人 %s 加载完成 (%.1fs)", name, time.perf_counter() - t0)
        return spec

    # ---------------- 推理 ----------------

    def convert(
        self,
        input_path,
        speaker: str,
        f0_up_key: Optional[int] = None,
        index_rate: Optional[float] = None,
    ) -> ConvertResult:
        """input_path: 音频文件路径 → ConvertResult(内存音频)。"""
        spec = self.load_speaker(speaker)
        key = spec.f0_up_key if f0_up_key is None else int(f0_up_key)
        rate = spec.index_rate if index_rate is None else float(index_rate)
        index_path = str(spec.index) if (spec.index and spec.index.is_file() and rate > 0) else ""

        t0 = time.perf_counter()
        with self._lock:
            status, result = self._vc.vc_single(
                0,                       # sid: 单说话人模型固定 0
                str(input_path),
                key,                     # f0_up_key (变调半音)
                self.f0_method,
                index_path,
                rate,
                spec.resample_sr,
                spec.rms_mix_rate,
                spec.protect,
            )
        seconds = time.perf_counter() - t0
        if not result or result[0] is None or result[1] is None:
            raise RuntimeError(f"RVC 推理失败: {status}")
        sr, audio = result
        return ConvertResult(audio=np.asarray(audio), sample_rate=int(sr), seconds=seconds, speaker=speaker, status=status)

    def save(self, result: ConvertResult, output_path):
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(output_path), result.audio, result.sample_rate)
        return output_path

    # ---------------- 注册表 ----------------

    def register_speakers(self) -> Dict[str, SpeakerSpec]:
        for name, item in (self.cfg.get("speakers") or {}).items():
            spec = SpeakerSpec(
                name=name,
                model=Path(item["model"]),
                index=Path(item["index"]) if item.get("index") else None,
                index_rate=float(item.get("index_rate", 0.75)),
                f0_up_key=int(item.get("f0_up_key", 0)),
                rms_mix_rate=float(item.get("rms_mix_rate", 1.0)),
                protect=float(item.get("protect", 0.33)),
                resample_sr=int(item.get("resample_sr", 0)),
                display_name=item.get("display_name", name),
            )
            self.speakers[name] = spec.resolved(MODULE_ROOT)
        return self.speakers

    @property
    def device(self) -> str:
        return str(getattr(self._config, "device", "unknown")) if self._vc else "not-loaded"
