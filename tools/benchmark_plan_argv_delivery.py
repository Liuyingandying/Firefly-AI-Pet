"""Phase 9D.6-H1 — Deterministic argv forensics for the Claude Plan prompt path.

Replays the EXACT production argv construction that the 9D.6 Plan step used
(PlanStepExecutor -> QuickAskRunner -> build_claude_args -> _wrapped_cli_args
-> QProcess -> powershell.exe -File run_cli.ps1 -> claude.ps1 shim -> native)
but substitutes the real claude shim with a FAKE shim that forwards to a native
argv-dump target. The fake target records the argument vector it actually
received, so we can answer with zero model calls:

    Did the multi-line PLAN prompt (with the embedded TaskRequest text) arrive
    intact at the final native process, or was it corrupted/truncated by the
    Windows PowerShell 5.1 argument hop?

This is pure local capability discovery: no production source is modified, no
Claude/Codex model is invoked, and the fake target lives under the system temp
directory. The transport chain under test is byte-for-byte the production one;
only the final executable is swapped for a recorder.

Usage:
    python tools/benchmark_plan_argv_delivery.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from PySide6.QtCore import QEventLoop, QTimer, QProcess
from PySide6.QtWidgets import QApplication

from core.routing_models import TaskRequest
from core.workflow_prompt import build_plan_prompt
from ui.process_launcher import _wrapped_cli_args, CLI_WRAPPER
from ui.quick_chat_protocol import build_claude_args

# The exact TaskRequest used by the real 9D.6 smoke harness.
TASK_TEXT = "Fix greeting.py so the existing test passes. Do not modify test_greeting.py."

DUMP_TEMPLATE = '''"""Auto-generated argv dump target (forensics only)."""
import json, sys
payload = {{
    "argv": sys.argv[1:],
    "argc": len(sys.argv) - 1,
    "prompt_last": (sys.argv[-1] if len(sys.argv) > 1 else None),
}}
out = r"{dump_path}"
with open(out, "w", encoding="utf-8") as f:
    f.write(json.dumps(payload, ensure_ascii=False, indent=2))
print("ARGV_DUMPED")
'''

SHIM_TEMPLATE = """#!/usr/bin/env pwsh
# Auto-generated fake npm-shim (forensics only): mimics claude.ps1's single
# native hop `& native.exe $args` exactly, but targets the argv recorder.
$basedir = Split-Path $MyInvocation.MyCommand.Definition -Parent
$python = "{python}"
if ($MyInvocation.ExpectingInput) {{
  $input | & $python "{target}" $args
}} else {{
  & $python "{target}" $args
}}
exit $LASTEXITCODE
"""


def main() -> int:
    app = QApplication.instance() or QApplication([])
    td = Path(tempfile.mkdtemp(prefix="firefly_plan_argv_"))
    dump_path = td / "argv_dump.json"
    target = td / "dump_argv.py"
    shim = td / "fake_claude.ps1"

    python = sys.executable
    target.write_text(DUMP_TEMPLATE.format(dump_path=str(dump_path)), encoding="utf-8")
    shim.write_text(SHIM_TEMPLATE.format(python=python, target=str(target)), encoding="utf-8")

    request = TaskRequest(text=TASK_TEXT, workspace=str(td))
    prompt = build_plan_prompt(request)
    cli_args = build_claude_args(prompt, effort="low", persistent=False, isolated=True)
    program, args = _wrapped_cli_args(str(shim), cli_args, interactive=False)

    print(f"prompt_len={len(prompt)}")
    print(f"cli_args_count={len(cli_args)}")
    print(f"program={program}")
    print(f"runner_argc={len(args)}")

    proc = QProcess(app)
    proc.setProgram(program)
    proc.setArguments(args)
    proc.setWorkingDirectory(str(td))
    proc.setProcessChannelMode(QProcess.SeparateChannels)
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    proc.finished.connect(loop.quit)
    proc.start()
    timer.start(30_000)
    loop.exec()
    exit_code = proc.exitCode()
    stdout = bytes(proc.readAllStandardOutput()).decode("utf-8", "replace")
    stderr = bytes(proc.readAllStandardError()).decode("utf-8", "replace")
    proc.waitForFinished(2000)

    if exit_code != 0 or not dump_path.exists():
        print("EXIT_CODE:", exit_code)
        print("STDOUT:", stdout[:500])
        print("STDERR:", stderr[:800])
        print("RESULT=FAIL (no argv dump produced)")
        return 1

    received = json.loads(dump_path.read_text(encoding="utf-8"))
    argc = received["argc"]
    last = received["prompt_last"] or ""
    exact_match = last == prompt

    # Sanity: the flag prefix should have arrived as separate args.
    prefix_ok = received["argv"][:3] == [
        "-p", "--permission-mode", "plan",
    ]
    prompt_ok = exact_match and len(last) == len(prompt)

    print(f"received_argc={argc}")
    print(f"received_first5={received['argv'][:5]}")
    print(f"flag_prefix_preserved={prefix_ok}")
    print(f"prompt_received_len={len(last)}")
    print(f"prompt_exact_match={exact_match}")
    if not exact_match:
        print("--- INTENDED prompt (first 300) ---")
        print(repr(prompt[:300]))
        print("--- RECEIVED prompt (first 300) ---")
        print(repr(last[:300]))

    ok = prompt_ok and prefix_ok
    print("RESULT=", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
