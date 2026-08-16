"""Phase 9D.6-H1 — Controlled real `codex exec` transport benchmark (ONE online run).

Objective: decide whether `codex exec` (non-interactive) can replace the
detached interactive native Codex spawn as the Workflow Implement backend.
This is the FIRST real online codex-exec exercise in Firefly (8C built the
code path with ``--sandbox read-only`` but never ran Codex online).

Security contract (Phase 9D.6-H1 section 7):
  - Preserves Codex's native sandbox: --sandbox workspace-write (required for
    the Implement step to modify the workspace).
  - Preserves the approval/permission boundary: NO --dangerously-bypass-*,
    NO --approve-for-me, NO -a never. Default approval policy is used.
  - Runs ONLY in a disposable workspace under the system temp directory,
    never the production project.

The command mirrors the production transport chain for a managed step
(QuickAskRunner shape): QProcess-style pipes (no TTY) -> powershell.exe ->
tools/run_cli.ps1 -> E:/npm-global/codex.ps1 shim -> node codex.js exec --json.

Usage:
    python tools/benchmark_codex_transport.py [--workspace PATH] [--timeout SECONDS]
        [--no-cleanup]

Prints a benchmark summary and writes <workspace>/../codex_exec_evidence.json.
Exit 0 = the transport is a viable managed backend (file edited, test passes);
exit 1 = viable but a caveat (e.g. needed approval, read-only fallback);
exit 2 = transport FAILED (TTY error / timeout / no change).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
CLI_WRAPPER = PROJECT_DIR / "tools" / "run_cli.ps1"
CODEX_SOURCE = PROJECT_DIR / "runtime" / "sources" / "codex.json"

FIXTURE_GREETING = 'def greet(name):\n    return "Hello"\n'
FIXTURE_TEST = 'from greeting import greet\n\nassert greet("Firefly") == "Hello, Firefly!"\n'
FIXTURE_README = "Tiny disposable Phase 9D.6-H1 codex exec benchmark workspace.\n"

TASK = "Fix greeting.py so the existing test passes. Do not modify test_greeting.py."

BUSINESS_FILES = ("greeting.py", "test_greeting.py", "README.md")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def snapshot(root: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    if not root.exists():
        return out
    for p in sorted(root.rglob("*")):
        if p.is_file():
            rel = p.relative_to(root).as_posix()
            try:
                out[rel] = p.read_bytes()
            except OSError:
                out[rel] = b"<unreadable>"
    return out


def run_local_test(ws: Path) -> dict:
    try:
        proc = subprocess.run(
            [sys.executable, str(ws / "test_greeting.py")],
            cwd=str(ws), capture_output=True, text=True, timeout=120,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"ran": False, "exit": None, "passed": None, "tail": f"<error: {exc}>"}
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-8:]
    return {"ran": True, "exit": proc.returncode, "passed": proc.returncode == 0, "tail": "\n".join(tail)}


def kill_tree(pid: int) -> None:
    try:
        subprocess.run(
            ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
            capture_output=True, timeout=30,
        )
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Controlled codex exec transport benchmark")
    parser.add_argument("--workspace", help="existing disposable workspace to reuse")
    parser.add_argument("--timeout", type=int, default=360)
    parser.add_argument("--no-cleanup", action="store_true")
    args = parser.parse_args()

    if args.workspace:
        ws = Path(args.workspace).expanduser().resolve()
        ws.mkdir(parents=True, exist_ok=True)
        created = False
    else:
        ws = Path(tempfile.mkdtemp(prefix="firefly_codex_exec_"))
        created = True

    evidence: dict = {}
    result_ok = False
    codex_before = CODEX_SOURCE.read_bytes() if CODEX_SOURCE.exists() else None

    try:
        if created or not (ws / "greeting.py").exists():
            (ws / "greeting.py").write_text(FIXTURE_GREETING, encoding="utf-8")
            (ws / "test_greeting.py").write_text(FIXTURE_TEST, encoding="utf-8")
            (ws / "README.md").write_text(FIXTURE_README, encoding="utf-8")

        initial = run_local_test(ws)
        print(f"[bench] workspace={ws}")
        print(f"[bench] initial test: exit={initial['exit']} passed={initial['passed']}")
        evidence["initial_test"] = initial
        if initial["passed"] is not False:
            print("[bench] INVALID FIXTURE: initial test must FAIL")
            return 2

        before = snapshot(ws)
        lastmsg = ws / "_codex_last_message.md"
        shim = shutil.which("codex")
        if not shim:
            print("[bench] codex CLI not found on PATH")
            return 2
        shim = Path(shim)
        cmd = [
            shutil.which("powershell.exe") or "powershell.exe",
            "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-NonInteractive",
            "-File", str(CLI_WRAPPER), str(shim),
            "exec",
            "--sandbox", "workspace-write",
            "--json",
            "--skip-git-repo-check",
            "-c", "model_reasoning_effort=low",
            "--ephemeral",
            "-o", str(lastmsg),
            TASK,
        ]
        print(f"[bench] cmd = powershell -File run_cli.ps1 codex.ps1 exec --sandbox workspace-write --json ...")
        t0 = time.time()
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=str(ws), text=True, encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            print(f"[bench] spawn failed: {exc}")
            evidence["error"] = f"spawn: {exc}"
            return 2

        timed_out = False
        try:
            stdout, stderr = proc.communicate(timeout=args.timeout)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            print(f"[bench] TIMEOUT after {args.timeout}s; killing process tree")
            kill_tree(proc.pid)
            stdout, stderr = proc.communicate(timeout=30)
            exit_code = proc.returncode
        elapsed = round(time.time() - t0, 1)

        evidence["exit_code"] = exit_code
        evidence["timed_out"] = timed_out
        evidence["elapsed_seconds"] = elapsed
        evidence["stdout"] = stdout[-20000:]
        evidence["stderr"] = stderr[-4000:]
        evidence["last_message_file"] = (
            lastmsg.read_text(encoding="utf-8")[:4000] if lastmsg.exists() else None
        )
        print(f"[bench] exit={exit_code} elapsed={elapsed}s timed_out={timed_out}")

        no_tty_error = "stdin is not a terminal" in (stdout + stderr)
        evidence["no_tty_error_present"] = no_tty_error
        print(f"[bench] 'stdin is not a terminal' in output: {no_tty_error}")

        # JSONL events present?
        jsonl_lines = [ln for ln in stdout.splitlines() if ln.strip().startswith("{")]
        event_types: list[str] = []
        thread_id = None
        for ln in jsonl_lines:
            try:
                obj = json.loads(ln)
            except ValueError:
                continue
            t = obj.get("type")
            if t:
                event_types.append(t)
            if t == "thread.started" and isinstance(obj.get("thread_id"), str):
                thread_id = obj["thread_id"]
        evidence["jsonl_line_count"] = len(jsonl_lines)
        evidence["event_types"] = event_types[:60]
        evidence["thread_id"] = thread_id
        print(f"[bench] jsonl_lines={len(jsonl_lines)} thread_id={thread_id or 'NONE'}")
        print(f"[bench] event_types={event_types[:25]}")

        after = snapshot(ws)
        changed = {
            rel: ("changed" if before.get(rel) != after.get(rel) else "same")
            for rel in set(before) | set(after)
        }
        business_delta = {k: v for k, v in changed.items() if k in BUSINESS_FILES}
        evidence["workspace_delta"] = business_delta
        print(f"[bench] business delta: {business_delta}")

        greeting_modified = business_delta.get("greeting.py") == "changed"
        test_untouched = business_delta.get("test_greeting.py") in (None, "same")
        evidence["greeting_modified"] = greeting_modified
        evidence["test_greeting_untouched"] = test_untouched
        evidence["greeting_content"] = (
            (ws / "greeting.py").read_text(encoding="utf-8") if (ws / "greeting.py").exists() else None
        )

        local_after = run_local_test(ws)
        evidence["local_test_after"] = local_after
        print(f"[bench] local test after: exit={local_after['exit']} passed={local_after['passed']}")

        codex_after = CODEX_SOURCE.read_bytes() if CODEX_SOURCE.exists() else None
        hook_fired = codex_after != codex_before
        evidence["codex_hook_fired"] = hook_fired
        evidence["codex_source_before"] = codex_before.decode("utf-8", "replace") if codex_before else None
        evidence["codex_source_after"] = codex_after.decode("utf-8", "replace") if codex_after else None
        print(f"[bench] codex hook updated runtime/sources/codex.json: {hook_fired}")

        evidence_path = ws.parent / "codex_exec_evidence.json"
        evidence_path.write_text(json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[bench] evidence: {evidence_path}")

        if no_tty_error or timed_out:
            result_ok = False
        elif greeting_modified and local_after["passed"] and test_untouched:
            result_ok = True
        elif greeting_modified and not local_after["passed"]:
            result_ok = False
        else:
            result_ok = False
        print("[bench] RESULT:", "PASS" if result_ok else "FAIL")
        return 0 if result_ok else (1 if not (no_tty_error or timed_out) else 2)
    finally:
        if created and not args.no_cleanup:
            shutil.rmtree(ws, ignore_errors=True)
            print(f"[bench] cleaned up disposable workspace {ws}")


if __name__ == "__main__":
    sys.exit(main())
