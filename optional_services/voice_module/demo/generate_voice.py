# -*- coding: utf-8 -*-
"""Firefly 流萤语音体验 Demo。

用法:
    cd <voice_module>/demo

    1) 编辑 input.txt, 第一行可写 emotion 声明(可选):
         emotion: comfort
         今天也辛苦了，要早点休息哦。
    2) 运行:
         python generate_voice.py                    # 按 input.txt 生成+播放
         python generate_voice.py --emotion happy    # 命令行覆盖情绪
         python generate_voice.py --text "你好" --no-play

流程: 文本 → edge-tts(按情绪语速/音量) → RVC 流萤音色(变调) → energy 增益
      → 保存 output.wav + 归档 output/<日期>_<时间>_<情绪>.wav → 自动播放

情绪: neutral / happy / excited / comfort / sad (省略时自动按文本关键词推断)
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime
from pathlib import Path

DEMO_DIR = Path(__file__).resolve().parent
MODULE_ROOT = DEMO_DIR.parent
sys.path.insert(0, str(MODULE_ROOT))

INPUT_FILE = DEMO_DIR / "input.txt"
LATEST = DEMO_DIR / "output.wav"
ARCHIVE = DEMO_DIR / "output"

HEADER_RE = re.compile(r"^\s*emotion\s*[:=]\s*(\w+)\s*$", re.IGNORECASE)


def read_input() -> tuple[str, str | None]:
    """返回 (text, header_emotion)。"""
    if not INPUT_FILE.is_file():
        sys.exit(f"找不到 {INPUT_FILE}")
    lines = INPUT_FILE.read_text(encoding="utf-8").strip().splitlines()
    emotion = None
    if lines and (m := HEADER_RE.match(lines[0])):
        emotion = m.group(1).lower()
        lines = lines[1:]
    text = "\n".join(lines).strip()
    if not text:
        sys.exit("input.txt 没有文本内容")
    return text, emotion


def main() -> int:
    parser = argparse.ArgumentParser(description="流萤语音生成 Demo")
    parser.add_argument("--text", help="直接指定文本 (忽略 input.txt)")
    parser.add_argument("--emotion", choices=["neutral", "happy", "excited", "comfort", "sad"],
                        help="覆盖情绪 (默认: input.txt 头部声明 > 自动推断)")
    parser.add_argument("--no-play", action="store_true", help="只生成不播放")
    args = parser.parse_args()

    text, header_emotion = (args.text, None) if args.text else read_input()
    emotion = args.emotion or header_emotion  # None = 自动推断

    from core.emotion import get_params, parse_emotion  # noqa: PLC0415
    from core.speech import speak_text  # noqa: PLC0415

    name, source = parse_emotion(text, emotion)
    params = get_params(name)
    print("=" * 56)
    print(f"文本     : {text.strip()[:40]}{'…' if len(text) > 40 else ''}")
    print(f"情绪     : {name} (来源: {source}) | speed={params.speed} pitch={params.pitch:+d} energy={params.energy}")

    t0 = time.perf_counter()
    result = speak_text(text, speaker_model="firefly", emotion=name, play=False)
    total = time.perf_counter() - t0

    wav = result.to_wav_bytes()
    LATEST.write_bytes(wav)
    ARCHIVE.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_path = ARCHIVE / f"{stamp}_{name}.wav"
    archive_path.write_bytes(wav)

    audio_s = sum(s.duration_s for s in result.segments)
    print(f"输出     : {LATEST}")
    print(f"归档     : {archive_path.name}")
    print(f"音频时长 : {audio_s:.2f}s | 生成耗时: {total:.1f}s "
          f"(TTS {sum(s.tts_ms for s in result.segments) / 1000:.1f}s + RVC {sum(s.vc_ms for s in result.segments) / 1000:.1f}s)")
    print("=" * 56)

    if not args.no_play:
        try:
            import numpy as np
            import sounddevice as sd

            print("▶ 播放中 (Ctrl+C 跳过)…")
            sd.play(result.concat_audio().astype(np.float32), result.sample_rate)
            sd.wait()
            print("✔ 播放完成")
        except Exception as e:  # noqa: BLE001
            print(f"(无法自动播放: {e} — 请手动播放 {LATEST})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
