"""Phase 8C backend benchmark — local process + minimal Claude online.

Measures the process chain Firefly Quick Ask currently uses:

    QProcess -> powershell.exe -> run_cli.ps1 -> <cli>.CMD -> cmd.exe -> native CLI

Two modes (std-lib only, Windows-first, no deps):

    python tools/benchmark_phase8c_backend.py --local
        Local-only startup costs. Produces NO model tokens.

    python tools/benchmark_phase8c_backend.py --claude [--out docs/phase8c_benchmark_results.json]
        2 cold one-shot + 2 resume + 1 direct-exe comparison.
        Minimal prompt: "Reply exactly: OK".

There is intentionally NO --codex-online mode. Codex online calls are out of
scope for Phase 8C and are never invoked by this tool.

Safety properties:
  - argv is always a list (never shell=True, never string-concatenated).
  - Per-call timeout; on timeout the process tree is taskkilled.
  - The prompt is fixed; no project files are read by the model.
  - No API keys / env values are ever printed.
  - Native session IDs are truncated to 8 chars before any persistence.
  - Runtime hook side-effects (observer hooks write runtime/sources/*.json)
    are snapshotted before the run and restored afterwards, so the live pet
    state is left untouched.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
CLI_WRAPPER = PROJECT_DIR / "tools" / "run_cli.ps1"
RUNTIME_FILES = [
    PROJECT_DIR / "runtime" / "sources" / "claude.json",
    PROJECT_DIR / "runtime" / "sources" / "codex.json",
    PROJECT_DIR / "runtime" / "state.json",
]

CLAUDE_PROMPT = "Reply exactly: OK"
PROMPT_LOG = "<prompt redacted>"  # never persisted
SAMPLES = 5
ONLINE_SAMPLES = 2

DEFAULT_TIMEOUT_S = 180.0


# --------------------------------------------------------------------------
# process resolution
# --------------------------------------------------------------------------

def _powershell() -> str:
    return shutil.which("powershell.exe") or shutil.which("powershell") or "powershell.exe"


def _claude_cmd() -> str | None:
    return shutil.which("claude")


def _codex_cmd() -> str | None:
    return shutil.which("codex")


def _claude_native_exe() -> str | None:
    """Resolve the real claude.exe behind the .CMD shim (best effort)."""
    cmd = _claude_cmd()
    if not cmd:
        return None
    p = Path(cmd).resolve()
    if p.suffix.lower() in {".cmd", ".bat"}:
        node_modules = p.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        if node_modules.exists():
            return str(node_modules)
    return cmd


def _node() -> str | None:
    return shutil.which("node")


def wrapper_argv(target: str, args: list[str]) -> list[str]:
    """Mirror _wrapped_cli_args(...interactive=False) in ui/process_launcher.py."""
    return [
        _powershell(),
        "-NoLogo",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-NonInteractive",
        "-File",
        str(CLI_WRAPPER),
        target,
        *args,
    ]


# --------------------------------------------------------------------------
# runtime state snapshot (hooks write these; we restore them)
# --------------------------------------------------------------------------

def _snapshot_runtime() -> dict[str, bytes | None]:
    snap: dict[str, bytes | None] = {}
    for p in RUNTIME_FILES:
        try:
            snap[str(p)] = p.read_bytes() if p.exists() else None
        except OSError:
            snap[str(p)] = None
    return snap


def _restore_runtime(snap: dict[str, bytes | None]) -> list[str]:
    changed: list[str] = []
    for strp, original in snap.items():
        p = Path(strp)
        try:
            if original is None:
                if p.exists():
                    p.unlink()
                    changed.append(strp)
            else:
                current = p.read_bytes() if p.exists() else None
                if current != original:
                    p.write_bytes(original)
                    changed.append(strp)
        except OSError:
            pass
    return changed


# --------------------------------------------------------------------------
# generic measured run
# --------------------------------------------------------------------------

class RunResult:
    def __init__(self, name: str):
        self.name = name
        self.spawn_ms: float | None = None
        self.total_ms: float | None = None
        self.first_event_ms: float | None = None
        self.first_text_ms: float | None = None
        self.exit_code: int | None = None
        self.timed_out = False
        self.first_event_type: str | None = None
        self.session_id_prefix: str | None = None
        self.session_id_full: str | None = None  # memory only; never persisted
        self.stderr_tail: str = ""
        self.stdout_tail: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "spawn_ms": self.spawn_ms,
            "first_event_ms": self.first_event_ms,
            "first_text_ms": self.first_text_ms,
            "total_ms": self.total_ms,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "first_event_type": self.first_event_type,
            "session_id_prefix": self.session_id_prefix,
            "stderr_tail": self.stderr_tail[-300:],
            "stdout_tail": self.stdout_tail[-300:],
        }


def _kill_tree(pid: int) -> None:
    try:
        subprocess.run(
            ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            timeout=10,
        )
    except Exception:
        pass


def _timed_capture(
    name: str,
    argv: list[str],
    *,
    cwd: str | None,
    timeout_s: float,
    parse_claude_stream: bool,
) -> RunResult:
    """Run a subprocess and timestamp events. Never uses shell=True."""
    res = RunResult(name)
    started = time.perf_counter()

    def _ms(ts: float) -> float:
        return round((ts - started) * 1000.0, 1)

    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        res.stderr_tail = f"spawn error: {exc}"
        return res
    res.spawn_ms = _ms(time.perf_counter())

    stdout_lines: list[tuple[float, str]] = []
    stderr_lines: list[tuple[float, str]] = []
    done = threading.Event()

    def _reader(stream, sink: list, field: str):
        try:
            while True:
                raw = stream.readline()
                if raw == b"":
                    break
                text = raw.decode("utf-8", "replace")
                if text:
                    sink.append((time.perf_counter(), text))
                    if field == "first_event" and res.first_event_ms is None:
                        pass  # set below by parser
        except Exception:
            pass
        done.set()

    t_out = threading.Thread(target=_reader, args=(proc.stdout, stdout_lines, "out"), daemon=True)
    t_err = threading.Thread(target=_reader, args=(proc.stderr, stderr_lines, "err"), daemon=True)
    t_out.start()
    t_err.start()

    first_event_seen = False

    try:
        deadline = time.monotonic() + timeout_s
        while True:
            if proc.poll() is not None:
                break
            # incremental parse of lines accumulated so far
            if parse_claude_stream:
                for ts, line in stdout_lines:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    if not first_event_seen:
                        try:
                            obj = json.loads(stripped)
                        except json.JSONDecodeError:
                            continue
                        first_event_seen = True
                        res.first_event_ms = _ms(ts)
                        res.first_event_type = str(obj.get("type"))
                        _set_session(obj, res)
                        # inspect for text delta in the same event
                        _maybe_text(obj, res, ts, started)
                    else:
                        try:
                            obj = json.loads(stripped)
                        except json.JSONDecodeError:
                            continue
                        _set_session(obj, res)
                        _maybe_text(obj, res, ts, started)
            if time.monotonic() > deadline:
                raise TimeoutError("deadline")
            time.sleep(0.002)
    except TimeoutError:
        res.timed_out = True
        if proc.poll() is None:
            _kill_tree(proc.pid)
    finally:
        try:
            out, err = proc.communicate(timeout=5)
        except Exception:
            proc.kill()
            out, err = proc.communicate()
        if out:
            for raw in out.split(b"\n"):
                text = raw.decode("utf-8", "replace")
                if text:
                    stdout_lines.append((time.perf_counter(), text))
        if err:
            stderr_lines.append((time.perf_counter(), err.decode("utf-8", "replace")))

    # final parse pass over all lines (covers trailing lines)
    if parse_claude_stream:
        for ts, line in stdout_lines:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if res.first_event_ms is None:
                res.first_event_ms = _ms(ts)
                res.first_event_type = str(obj.get("type"))
            _set_session(obj, res)
            _maybe_text(obj, res, ts, started)

    res.exit_code = proc.returncode
    res.total_ms = _ms(time.perf_counter())
    res.stderr_tail = "".join(t for _, t in stderr_lines).strip()[-300:]
    res.stdout_tail = "".join(t for _, t in stdout_lines).strip()[-300:]
    return res


def _set_session(obj: dict, res: RunResult) -> None:
    if res.session_id_full is not None:
        return
    sid = obj.get("session_id")
    if isinstance(sid, str) and sid:
        res.session_id_full = sid
        res.session_id_prefix = sid[:8]


def _maybe_text(obj: dict, res: RunResult, ts: float, started: float) -> None:
    if res.first_text_ms is not None:
        return
    kind = obj.get("type")
    if kind == "stream_event":
        ev = obj.get("event")
        if isinstance(ev, dict) and ev.get("type") == "content_block_delta":
            delta = ev.get("delta")
            if isinstance(delta, dict) and delta.get("type") == "text_delta" and delta.get("text"):
                res.first_text_ms = round((ts - started) * 1000.0, 1)
    elif kind == "assistant":
        message = obj.get("message")
        if isinstance(message, dict) and message.get("content"):
            res.first_text_ms = res.first_text_ms if res.first_text_ms is not None else round((ts - started) * 1000.0, 1)


def _timed_simple(name: str, argv: list[str], *, cwd: str | None, timeout_s: float) -> RunResult:
    res = RunResult(name)
    started = time.perf_counter()
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        res.stderr_tail = f"spawn error: {exc}"
        return res
    res.spawn_ms = round((time.perf_counter() - started) * 1000.0, 1)
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        res.timed_out = True
        _kill_tree(proc.pid)
        try:
            out, err = proc.communicate(timeout=5)
        except Exception:
            out, err = b"", b""
    res.total_ms = round((time.perf_counter() - started) * 1000.0, 1)
    res.exit_code = proc.returncode
    res.stdout_tail = out.decode("utf-8", "replace").strip()[-300:]
    res.stderr_tail = err.decode("utf-8", "replace").strip()[-300:]
    return res


# --------------------------------------------------------------------------
# aggregation
# --------------------------------------------------------------------------

def _summarize(results: list[RunResult], key: str) -> dict:
    vals = [getattr(r, key) for r in results if getattr(r, key) is not None and not r.timed_out]
    if not vals:
        return {"n": 0, "min": None, "median": None, "max": None}
    return {
        "n": len(vals),
        "min": round(min(vals), 1),
        "median": round(statistics.median(vals), 1),
        "max": round(max(vals), 1),
    }


def _claude_args(prompt: str, *, resume: str | None) -> list[str]:
    args = [
        "-p",
        "--permission-mode",
        "plan",
        "--effort",
        "low",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
    ]
    if resume:
        args.extend(["--resume", resume])
    return [*args, prompt]


# --------------------------------------------------------------------------
# local benchmark (no model tokens)
# --------------------------------------------------------------------------

def run_local(workspace: str, timeout_s: float) -> dict:
    results: list[RunResult] = []
    ps = _powershell()
    claude_cmd = _claude_cmd()
    claude_exe = _claude_native_exe()
    codex_cmd = _codex_cmd()

    def _run_many(label: str, argv_factory, n: int = SAMPLES) -> None:
        for i in range(n):
            results.append(_timed_simple(f"{label}#{i + 1}", argv_factory(), cwd=workspace, timeout_s=timeout_s))

    print("== local: powershell startup ==")
    _run_many("powershell-exit0", lambda: [ps, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "exit 0"])

    if claude_exe:
        print("== local: claude.exe --version (direct) ==")
        _run_many("claude-exe-version", lambda: [claude_exe, "--version"])
        print("== local: claude.exe --help (direct) ==")
        _run_many("claude-exe-help", lambda: [claude_exe, "--help"])
    if claude_cmd:
        print("== local: claude.CMD --version (via run_cli.ps1 wrapper) ==")
        _run_many("claude-cmd-version", lambda: wrapper_argv(claude_cmd, ["--version"]))
        print("== local: claude.CMD --help (via run_cli.ps1 wrapper) ==")
        _run_many("claude-cmd-help", lambda: wrapper_argv(claude_cmd, ["--help"]))
    if claude_exe:
        print("== local: claude.exe --version (via run_cli.ps1 wrapper, no .CMD) ==")
        _run_many("claude-exe-version-wrapper", lambda: wrapper_argv(claude_exe, ["--version"]))
    if codex_cmd:
        print("== local: codex.CMD --version (via run_cli.ps1 wrapper) ==")
        _run_many("codex-cmd-version", lambda: wrapper_argv(codex_cmd, ["--version"]))
        print("== local: codex.CMD --help (via run_cli.ps1 wrapper) ==")
        _run_many("codex-cmd-help", lambda: wrapper_argv(codex_cmd, ["--help"]))

    summary: dict[str, dict] = {}
    for key in ("total_ms", "spawn_ms"):
        group: dict[str, dict] = {}
        for r in results:
            group.setdefault(r.name.split("#")[0], []).append(r)
        for name, runs in group.items():
            summary[f"{name}.{key}"] = _summarize(runs, key)
    return summary


# --------------------------------------------------------------------------
# Claude online benchmark (minimal, capped)
# --------------------------------------------------------------------------

def run_claude(workspace: str, timeout_s: float, out_path: str | None) -> dict:
    claude_cmd = _claude_cmd()
    claude_exe = _claude_native_exe()
    if not claude_cmd or not claude_exe:
        print("claude CLI not found; aborting", file=sys.stderr)
        return {}

    print("snapshotting runtime hook files ...")
    snap = _snapshot_runtime()
    results: list[RunResult] = []

    try:
        # cold one-shots through the production wrapper chain
        print("== claude cold (production wrapper path) ==")
        for i in range(ONLINE_SAMPLES):
            argv = wrapper_argv(claude_cmd, _claude_args(CLAUDE_PROMPT, resume=None))
            r = _timed_capture(f"claude-cold#{i + 1}", argv, cwd=workspace, timeout_s=timeout_s, parse_claude_stream=True)
            results.append(r)
            print(f"  cold#{i + 1}: total={r.total_ms}ms first_event={r.first_event_ms}ms first_text={r.first_text_ms}ms exit={r.exit_code}")

        # direct exe comparison (one sample)
        print("== claude cold (direct exe, no wrapper) ==")
        argv = [claude_exe, *_claude_args(CLAUDE_PROMPT, resume=None)]
        r = _timed_capture("claude-cold-direct#1", argv, cwd=workspace, timeout_s=timeout_s, parse_claude_stream=True)
        results.append(r)
        print(f"  direct#1: total={r.total_ms}ms first_event={r.first_event_ms}ms first_text={r.first_text_ms}ms exit={r.exit_code}")

        # resume one-shots reuse the full session id from cold run #1,
        # kept in memory only (never persisted; prefix-only is saved).
        resume_sid = results[0].session_id_full if results else None
        print("== claude resume (production wrapper path) ==")
        if resume_sid is None:
            print("  !! no session id captured; skipping resume runs", file=sys.stderr)
        for i in range(ONLINE_SAMPLES):
            argv = wrapper_argv(claude_cmd, _claude_args(CLAUDE_PROMPT, resume=resume_sid))
            r = _timed_capture(f"claude-resume#{i + 1}", argv, cwd=workspace, timeout_s=timeout_s, parse_claude_stream=True)
            results.append(r)
            print(f"  resume#{i + 1}: total={r.total_ms}ms first_event={r.first_event_ms}ms first_text={r.first_text_ms}ms exit={r.exit_code} session={r.session_id_prefix}")
    finally:
        changed = _restore_runtime(snap)
        if changed:
            print(f"note: restored runtime hook side-effects: {changed}")

    summary: dict = {}
    for key in ("total_ms", "first_event_ms", "first_text_ms", "spawn_ms"):
        group: dict[str, list[RunResult]] = {}
        for r in results:
            if r.timed_out:
                continue
            group.setdefault(r.name.split("#")[0], []).append(r)
        summary[key] = {name: _summarize(runs, key) for name, runs in group.items()}

    payload = {
        "tool": "tools/benchmark_phase8c_backend.py",
        "claude_version": _local_version([claude_exe, "--version"]),
        "claude_cmd": claude_cmd,
        "prompt": PROMPT_LOG,
        "cold_runs": ONLINE_SAMPLES,
        "resume_runs": ONLINE_SAMPLES,
        "results": [r.to_dict() for r in results],
        "summary": summary,
    }
    if out_path:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"wrote {out_path}")
    return payload


def _local_version(argv: list[str]) -> str | None:
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=30).stdout.strip().splitlines()
        return out[0] if out else None
    except Exception:
        return None


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 8C backend benchmark (no Codex online mode).")
    ap.add_argument("--local", action="store_true", help="local startup benchmarks only (no model tokens)")
    ap.add_argument("--claude", action="store_true", help="minimal Claude cold+resume benchmark (online, capped)")
    ap.add_argument("--workspace", default=str(PROJECT_DIR), help="cwd for benchmark runs")
    ap.add_argument("--out", default=None, help="optional JSON results path (e.g. docs/phase8c_benchmark_results.json)")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="per-call timeout seconds")
    args = ap.parse_args()

    workspace = args.workspace
    if not os.path.isdir(workspace):
        print(f"workspace not found: {workspace}", file=sys.stderr)
        return 2

    if not args.local and not args.claude:
        ap.print_help()
        return 0

    payload: dict = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"), "python": sys.version.split()[0]}
    if args.local:
        payload["local"] = run_local(workspace, args.timeout)
    if args.claude:
        payload["claude"] = run_claude(workspace, args.timeout, args.out)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
