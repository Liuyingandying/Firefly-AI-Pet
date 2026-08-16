"""Benchmark Codex Short Talk reasoning effort (minimal vs low) — diagnostic.

Runs the SAME argv the production Short Talk uses (read-only, ephemeral, json),
only varying ``model_reasoning_effort``. Reports median/min/max and success for
3x minimal + 3x low, alternating to de-bias network drift. No tokens/secrets.
"""

from __future__ import annotations

import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from ui.process_launcher import _claude_settings_env, _wrapped_cli_args, find_executable

PROMPT = "晚上好，请只回复：晚上好！"


def run_one(exe: str, workspace: Path, env: dict, effort: str) -> dict:
    args = [
        "exec", "--sandbox", "read-only", "--json",
        "-c", f"model_reasoning_effort={effort}", "--ephemeral", PROMPT,
    ]
    program, argv = _wrapped_cli_args(exe, args, interactive=False)
    t0 = time.monotonic()
    try:
        r = subprocess.run([program, *argv], cwd=str(workspace), env=env,
                           capture_output=True, timeout=150)
        elapsed = time.monotonic() - t0
        out = r.stdout.decode("utf-8", "replace")
        err = r.stderr.decode("utf-8", "replace")
        reconn = out.lower().count("reconnecting") + err.lower().count("reconnecting") + err.lower().count("timed out")
        ok = r.returncode == 0 and "晚上好" in out
        return {"effort": effort, "elapsed": elapsed, "exit": r.returncode, "ok": ok, "reconn": reconn}
    except subprocess.TimeoutExpired:
        return {"effort": effort, "elapsed": time.monotonic() - t0, "exit": "TIMEOUT", "ok": False, "reconn": -1}


def main() -> None:
    workspace = Path("E:/Firefly_AI_Pet")
    exe = find_executable("codex")
    env = dict(os.environ)
    for k, v in _claude_settings_env().items():
        env[k] = v

    order = ["low", "minimal", "low", "minimal", "low", "minimal"]
    results: dict[str, list[dict]] = {"low": [], "minimal": []}
    for i, effort in enumerate(order):
        r = run_one(exe, workspace, env, effort)
        results[effort].append(r)
        print(f"[bench] #{i+1} effort={effort:8s} elapsed={r['elapsed']:.1f}s "
              f"exit={r['exit']} ok={r['ok']} reconn={r['reconn']}", flush=True)

    print("[bench] === summary ===")
    for label in ("low", "minimal"):
        rows = results[label]
        els = [r["elapsed"] for r in rows]
        oks = sum(1 for r in rows if r["ok"])
        print(f"[bench] {label:8s}: n={len(rows)} median={statistics.median(els):.1f}s "
              f"min={min(els):.1f}s max={max(els):.1f}s success={oks}/{len(rows)}", flush=True)


if __name__ == "__main__":
    main()
