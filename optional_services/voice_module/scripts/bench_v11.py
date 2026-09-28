# -*- coding: utf-8 -*-
"""v1.1 性能基准 (真实链路: edge-tts + RVC + AudioQueue)。

    cd voice_module
    python scripts/bench_v11.py

输出 markdown 表格文本, 用于 docs/Performance_v1.1.md。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

MODULE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MODULE_ROOT))

from audio.audio_queue import get_audio_queue  # noqa: E402
from core.speech import speak_text  # noqa: E402

LONG = ("今天我们学习了三个新知识点。第一个是语音情绪的表达方式！"
        "第二个是长文本的分句处理，它会自动切分和合并短句。"
        "第三个是播放队列的优先级机制。明天继续，记得复习哦。")


def main():
    rows = []

    # 1) 单句情绪延迟 (wav 模式: 全部转完才返回)
    for emotion, text in [
        ("neutral", "今天也要努力学习哦。"),
        ("happy", "哈哈，太好了，我们成功啦！"),
        ("comfort", "今天也辛苦了，要早点休息哦。"),
        ("sad", "对不起，这次没做好，有点难过。"),
        ("excited", "哇塞，太厉害了吧！"),
    ]:
        r = speak_text(text, emotion=emotion)
        rows.append({"case": f"单句/{emotion}", "text": text,
                     "segments": len(r.segments), "audio_s": round(r.segments[0].duration_s, 2),
                     "tts_ms": r.segments[0].tts_ms, "vc_ms": r.segments[0].vc_ms,
                     "total_ms": r.total_ms, "first_audio_ms": None})
        print(rows[-1], flush=True)

    # 2) 长文本 (wav 模式)
    r = speak_text(LONG, emotion="happy")
    tts_sum = sum(s.tts_ms for s in r.segments)
    vc_sum = sum(s.vc_ms for s in r.segments)
    rows.append({"case": "长文本/happy", "text": LONG[:18] + "…",
                 "segments": len(r.segments),
                 "audio_s": round(sum(s.duration_s for s in r.segments), 2),
                 "tts_ms": tts_sum, "vc_ms": vc_sum, "total_ms": r.total_ms, "first_audio_ms": None})
    print(rows[-1], flush=True)

    # 3) 长文本 play 模式: 首声延迟 (回调时间戳)
    events = []
    q = get_audio_queue(on_event=lambda k, i: events.append((k, time.perf_counter(), i.text if i else "")))
    t0 = time.perf_counter()
    r2 = speak_text(LONG, emotion="happy", play=True)
    total = int((time.perf_counter() - t0) * 1000)
    starts = [(t, txt) for k, t, txt in events if k == "start"]
    first_audio = int((starts[0][0] - t0) * 1000) if starts else None
    q.wait_idle(timeout=120)
    rows.append({"case": "长文本/play模式", "text": "同上(边转边播)",
                 "segments": len(r2.segments),
                 "audio_s": round(sum(s.duration_s for s in r2.segments), 2),
                 "tts_ms": tts_sum, "vc_ms": vc_sum, "total_ms": total,
                 "first_audio_ms": first_audio})
    print(rows[-1], flush=True)

    out = MODULE_ROOT / "output" / "bench_v11.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n已保存:", out)


if __name__ == "__main__":
    main()
