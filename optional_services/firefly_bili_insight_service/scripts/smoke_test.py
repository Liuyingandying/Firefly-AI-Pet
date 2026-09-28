"""Firefly_BiliInsight_Service 四 action 冒烟测试（匿名，公开视频）。

用法（在仓库根目录、已激活安装了 requirements.txt 的 venv）：

    python scripts/smoke_test.py                # 全部四项（transcribe 取 0-60s 窗口）
    python scripts/smoke_test.py --bvid BV1...  # 指定其他公开视频
    python scripts/smoke_test.py --skip-transcribe

每项输出一行 PASS/FAIL 与延迟；transcribe 会真实下载片段并跑本地
faster-whisper（首次运行会自动下载模型，见 README「ASR 模型」）。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SERVICE = Path(__file__).resolve().parent.parent / "service.py"
DEFAULT_BVID = "BV1s54y1n7Ev"  # 公开教程视频「Docker 10分钟快速入门」


def _kill_tree(proc: subprocess.Popen) -> None:
    """超时后杀掉整棵进程树。

    subprocess 超时只杀直接子进程（service.py），其派生的 ffmpeg 孙进程会
    因 stdout/stderr 管道无人读取而永久阻塞，并继续占用缓存文件——必须
    连同孙进程一起终止（Windows 用 taskkill /T，POSIX 用进程组）。
    """
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
        )
    else:
        proc.kill()


def call(request: dict, timeout: float) -> dict:
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(SERVICE)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        cwd=str(SERVICE.parent),
    )
    try:
        stdout, stderr = proc.communicate(
            json.dumps(request, ensure_ascii=False) + "\n", timeout=timeout
        )
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            stdout, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            stdout = ""
        return {"ok": False,
                "error": {"type": "timeout", "message": f">{timeout}s (tree killed)"}}
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            envelope = json.loads(line)
            if "ok" in envelope:
                return envelope
    return {"ok": False, "error": {"type": "crashed",
            "message": f"exit={proc.returncode} stderr={(stderr or '')[-300:]}"}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bvid", default=DEFAULT_BVID)
    parser.add_argument("--skip-transcribe", action="store_true")
    args = parser.parse_args()

    cases: list[tuple[str, dict, float]] = [
        ("health", {"action": "health"}, 60),
        ("metadata", {"action": "metadata", "video_id": args.bvid}, 120),
        ("frame", {"action": "frame", "video_id": args.bvid, "timestamp": "1:00"}, 180),
    ]
    if not args.skip_transcribe:
        cases.append(("transcribe",
                      {"action": "transcribe", "video_id": args.bvid,
                       "start_time": 0, "end_time": 60}, 600))

    failures = 0
    for name, request, timeout in cases:
        t0 = time.perf_counter()
        try:
            envelope = call(request, timeout)
        except subprocess.TimeoutExpired:
            envelope = {"ok": False, "error": {"type": "timeout", "message": f">{timeout}s"}}
        latency = int((time.perf_counter() - t0) * 1000)
        ok = bool(envelope.get("ok"))
        detail = ""
        if ok:
            data = envelope.get("data") or {}
            if name == "metadata":
                detail = f"title={data.get('title')!r} subtitle_flag={data.get('has_subtitle_flag')}"
            elif name == "transcribe":
                detail = f"segments={data.get('segment_count')} chars={data.get('chars')}"
        else:
            detail = str(envelope.get("error"))
            failures += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<10} {latency:>6}ms  {detail}")

    print(f"\n{len(cases) - failures}/{len(cases)} PASS")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
