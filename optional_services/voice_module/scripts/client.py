# -*- coding: utf-8 -*-
"""API 测试客户端。

用法 (先启动服务: python api/server.py):
    python scripts/client.py examples/input.wav              # 音频→流萤音色 (v1.0)
    python scripts/client.py --text "今天也要努力学习哦"       # 文字→流萤音色 (v1.0)
    python scripts/client.py --speak "辛苦了,早点休息哦"       # v1.1 表达层 (情绪+分句)
    python scripts/client.py --speak "测试" --play            # 边转边播 (本机扬声器)
"""

import argparse
import json
import sys
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8300"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", nargs="?", help="输入音频文件")
    parser.add_argument("--text", help="文字直转模式 (v1.0 TTS+VC)")
    parser.add_argument("--speak", help="表达层模式 (v1.1 情绪+分句+队列)")
    parser.add_argument("--emotion", help="指定情绪 happy/sad/comfort/excited/neutral")
    parser.add_argument("--play", action="store_true", help="speak 模式下本机播放")
    parser.add_argument("--priority", type=int, default=1)
    parser.add_argument("--speaker", default="firefly")
    parser.add_argument("--pitch", type=int, default=0)
    parser.add_argument("-o", "--output", default="output/client_result.wav")
    args = parser.parse_args()

    with httpx.Client(timeout=300) as client:
        if args.speak:
            r = client.post(f"{BASE}/voice/speak", json={
                "text": args.speak, "emotion": args.emotion, "speaker_id": args.speaker,
                "play": args.play, "priority": args.priority,
            })
            if r.headers.get("content-type", "").startswith("audio/"):
                out = Path(args.output)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(r.content)
                print(f"情绪: {r.headers.get('X-Emotion')} (来源 {r.headers.get('X-Emotion-Source')}) "
                      f"| 分句: {r.headers.get('X-Segments')} | 耗时: {r.headers.get('X-Total-Ms')} ms")
                print(f"已保存: {out.resolve()}")
            else:
                print(json.dumps(r.json(), ensure_ascii=False, indent=1))
            return
        if args.text:
            r = client.post(f"{BASE}/voice/tts_convert", data={"text": args.text, "speaker_id": args.speaker, "pitch": args.pitch})
        else:
            if not args.audio:
                parser.error("需要 audio 参数或 --text / --speak")
            r = client.post(
                f"{BASE}/voice/convert",
                data={"speaker_id": args.speaker, "pitch": args.pitch},
                files={"file": (Path(args.audio).name, open(args.audio, "rb"), "audio/wav")},
            )
    if r.status_code != 200:
        sys.exit(f"失败 [{r.status_code}]: {r.text}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(r.content)
    print(f"转换耗时: {r.headers.get('X-Convert-Ms', '?')} ms | 说话人: {r.headers.get('X-Speaker', args.speaker)}")
    print(f"已保存: {out.resolve()}")


if __name__ == "__main__":
    main()
