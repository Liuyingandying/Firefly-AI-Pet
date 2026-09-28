# -*- coding: utf-8 -*-
"""命令行推理入口 (PoC 验收路径)。

用法:
    cd voice_module
    python scripts/inference.py --input examples/input.wav --output output/output.wav
    python scripts/inference.py --input examples/input.wav --output output/out.wav --pitch 12
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

MODULE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MODULE_ROOT))

from core.pipeline import convert_voice, list_speakers  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Firefly Voice Conversion CLI (RVC engine)")
    parser.add_argument("--input", required=True, help="输入音频 (wav/flac/mp3...)")
    parser.add_argument("--output", required=True, help="输出 wav 路径")
    parser.add_argument("--speaker", default="firefly", help="说话人 (config.yaml 注册名)")
    parser.add_argument("--pitch", type=int, default=None, help="变调半音 (男声→流萤可试 12)")
    parser.add_argument("--index-rate", type=float, default=None, help="检索特征混合比例 0-1")
    args = parser.parse_args()

    t0 = time.perf_counter()
    result = convert_voice(args.input, speaker_model=args.speaker, output_audio=args.output,
                           pitch=args.pitch, index_rate=args.index_rate)
    total = time.perf_counter() - t0

    out = Path(args.output)
    if not out.is_absolute():
        out = MODULE_ROOT / out
    print("=" * 60)
    print(f"说话人     : {result.speaker}")
    print(f"输出文件   : {out}")
    print(f"采样率     : {result.sample_rate} Hz")
    print(f"音频时长   : {len(result.audio) / result.sample_rate:.2f} s")
    print(f"推理耗时   : {result.seconds:.2f} s (含首跑模型加载则总耗时 {total:.2f} s)")
    print(f"状态       : {result.status}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
