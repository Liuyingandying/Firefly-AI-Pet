# Phase 9D.6 — Controlled Real End-to-End Smoke: E2E Report

**Outcome: NO-GO**

Real-blocker finding: the native Codex interactive CLI requires a real terminal/TTY.
The Phase 9C direct-native argv transport safely delivered the prompt and spawned the
Codex process (transport PASS, spawn PASS), but the detached process printed
`Error: stdin is not a terminal` and never executed any workspace change. Interactive
managed execution FAILED. The full Claude → Codex → Claude chain therefore did not
complete: Step 2 never succeeded, Step 3 never ran, and the workflow never reached
SUCCEEDED.

---

## Environment

- Host: Windows 11 Home China, 10.0.26200, PowerShell 5.1
- Python: 3.13.9 (`<仓库目录>\.venv\Scripts\python.exe`), PySide6 6.11.1
- claude CLI: `<npm-global目录>\claude.ps1` → real native
  `node_modules\@anthropic-ai\claude-code\bin\claude.exe` (routed through the local
  proxy configured in `~/.claude/settings.json`; no credentials recorded here)
- codex CLI: `<npm-global目录>\codex.ps1` → resolved native target
  `node.exe` (`<Node目录>\node.exe`) + `node_modules\@openai\codex\bin\codex.js`
- Test driver: `tools/smoke_phase9d6_end_to_end.py` (new harness, the only new tool;
  no production source modified)

## Disposable workspace

- `C:\Users\<用户名>\AppData\Local\Temp\firefly_phase9d6_gl32qbao` — kept for inspection
- Fully isolated from `<仓库目录>`; never used as the Codex workspace root

## Initial fixture

| file | content | note |
|------|---------|------|
| `greeting.py` | `def greet(name):\n    return "Hello"` | intentionally wrong |
| `test_greeting.py` | `from greeting import greet; assert greet("Firefly") == "Hello, Firefly!"` | must not be modified |
| `README.md` | `Tiny disposable Phase 9D.6 test workspace.` | |

`python test_greeting.py` → `AssertionError`, exit 1 → **initial test FAIL confirmed**
(as required; fixture is valid).

## Agent call counts

- Claude Plan: **1** (real managed Claude call, completed)
- Codex Implement: **1** (real native Codex spawn, non-functional)
- Claude Review: **0** (correctly not run — abort path)
- Total real agent launches: **2**

## Step 1 — Real Claude Plan

- Coordinator: real `WorkflowCoordinator.create_plan_implement_review()` +
  `confirm_step("step_1")` + real `PlanStepExecutor.execute()` (transient
  `QuickAskRunner`, `session_manager=None`, `persistent=False`, `isolated=True`).
- Prompt: real `build_plan_prompt()` (no hand-written prompt).
- Step 1 state: **SUCCEEDED** (mechanically).
- PLAN artifact: `runtime/artifacts/7230b155347e/plan.md`, 191 bytes,
  sha256 `9d5576e0…`; `artifacts.json` index written.
- **PLAN content invalid (finding #2).** `plan.md` contains only:
  > "I don't see a specific task or request yet. What would you like me to plan? …"
  >
  It does not mention `greeting.py`, the test, validation, or an implementation —
  it fails the section 9 relevance requirement. The executor "SUCCEEDED" on any
  non-empty usable text. The harness validated only non-empty + SUCCEEDED, not plan
  relevance — a harness gap. Per the section 27 failure rule (invalid plan → do not
  start Codex), the smoke should have stopped here. Root cause (proxy/model/transport)
  was not diagnosed — no on-site fix per section 39.
- Step 2 after Step 1: **AWAITING_CONFIRMATION** (not auto-executed). ✓

## Step 1 workspace protection

- Business files (`greeting.py`, `test_greeting.py`, `README.md`): **byte-identical**
  before/after Step 1.
- Tool metadata: no `.claude/` / `.codex/` created by the Plan call. (Only
  `__pycache__/greeting.cpython-313.pyc`, produced by the harness's own local
  `python test_greeting.py` fixture runs — not an agent change.)

## Step 2 — Real Codex handoff + human gate

- `confirm_step("step_2")` → real `ImplementStepExecutor.execute()`: reuses the PLAN
  artifact + original `TaskRequest` + real `build_implement_prompt()` + Phase 9C
  direct native argv transport (`ProcessLauncher.launch_agent("codex", ws, prompt)`).
- Launch returned `(True, "已把任务发送给 Codex。")`; Step 2 → **RUNNING**.
- A real `codex` process spawned (PID 29144 + a child), started 20:24:17.
- **BLOCKER (finding #1):** the detached Codex CLI immediately printed
  `Error: stdin is not a terminal` and made **zero workspace changes in 26+ minutes**.
  `runtime/sources/codex.json` never updated (no Codex hook events fired), confirming
  the session never genuinely started.
- Human gate: the harness paused and asked the user to confirm Codex completion. No
  completion confirmation ever arrived. The user subsequently inspected the workspace
  and **confirmed the task FAILED**: `greeting.py` still `return "Hello"`, the test
  still FAILs, `test_greeting.py` unmodified. Gate resolved to **abort**; no
  `continue.txt` was written; no auto-confirmation, no sleep-based completion.

## Step 2 completion + workspace changes

- `confirm_completion()` was **never called** (no user confirmation of success).
- Workspace changed files from Codex: **none** (`greeting.py` unchanged).
- `test_greeting.py` modified? **No**.
- `README.md` modified? **No**.
- Local test after: **still FAIL** (`AssertionError`) — no implementation occurred.

## Step 2 artifacts

- `CHANGED_FILES` artifact: **not created** (Step 2 never completed).
- `IMPLEMENTATION_SUMMARY` artifact: **not created**.
- The executor's no-empty-diff guard never triggered because completion was never
  attempted — this is consistent with the production no-auto-complete contract.

## Step 3 — Real Claude Review

- **0 calls.** Correctly skipped: user confirmed Codex failed → abort → no Review per
  section 27. `coordinator.confirm_step("step_3")` never executed.
- `REVIEW` artifact: **none**.
- Review verdict: N/A.

## Final states

- Step 1: **SUCCEEDED** (persisted via PLAN artifact).
- Step 2: **RUNNING** at abort (in-memory only; the coordinator was transient and was
  lost when the harness process was killed — no persisted Step 2 state).
- Step 3: **PENDING** — never reached AWAITING_CONFIRMATION.
- WorkflowState: **never SUCCEEDED**. No final persisted workflow state (the
  coordinator is in-memory; the harness was killed before any completion write).
- `Workflow SUCCEEDED` would have meant "workflow steps completed" — it was not
  reached, and nothing in this run implies the implementation was correct.

## Session isolation

- `config/sessions.json`: **absent before and after** — unchanged. ✓
- Plan/Review were configured for fresh transient sessions (`session_manager=None`,
  `persistent=False`); no ordinary SessionManager was involved.

## Lifecycle behavior

- Claude workflow calls: `runtime/sources/claude.json` **did change** over the smoke
  window (timestamp advanced). Attribution: this hosting Claude Code session fires the
  `simulate_event.py` hooks on every turn (UserPromptSubmit/Stop), so the file is
  updated by the host session's activity, not provably by the workflow's transient
  Claude call. Within-turn isolation for workflow Claude calls was validated by
  9D.2/9D.5 and is not contradicted here (no independent within-turn measurement was
  re-taken because Step 2 never completed).
- Real Codex native task: `runtime/sources/codex.json` **unchanged** — no Codex hook
  events fired. Codex lifecycle was **not** observed. This is consistent with the
  failed session (no hook firing), not a successful external-lifecycle update.

## Business-file integrity

- `greeting.py`, `test_greeting.py`, `README.md`: **byte-identical across the entire
  smoke** (no agent modified any of them).

## Tool metadata changes

- Workspace tool metadata: none added by agents (no `.claude/`, no `.codex/`).
- `__pycache__/greeting.cpython-313.pyc`: created by the harness's own local fixture
  test runs (expected, not an agent change).

## Production project untouched

- 82 tracked source files (`core/*.py`, `ui/*.py`, `app.py`, `state_broker.py`,
  `tools/*.py` excluding the new harness) verified hash-identical to the pre-smoke
  baseline. No production source modified by test agents.
- `runtime/artifacts/7230b155347e/` holds only the smoke's test data (expected).

## Regression

- Pre-smoke (all required suites): 9C (50), 9D.1 (60), 9D.2 artifact-store (24),
  9D.2 plan-executor (43), 9D.3 (56), 9D.4 implement (50), 9D.4 snapshot (28),
  9D.5 (69) — **380 tests, all PASS**.
- Post-smoke: 9D.4 (50) + 9D.5 (69) — **all PASS**. No production behavior changed.

## Issues

1. **[BLOCKER] Native Codex interactive execution requires a real TTY.**
   `QProcess.startDetached()` + the direct native Codex argv transport safely
   delivers the prompt and spawns the Codex process — safe argv transport **PASS**,
   process spawn **PASS** — but the interactive Codex CLI requires a terminal; the
   detached process output `Error: stdin is not a terminal` and performed **no
   workspace modification** over 26+ minutes. Interactive managed execution **FAIL**.
   Not attributable to Codex programming ability, `WorkspaceSnapshot`,
   `ArtifactStore`, or model output — the process never ran a task at all.
2. **[FINDING] Step 1 PLAN artifact content was invalid.** The managed Claude Plan
   call returned a generic "I don't see a specific task or request yet" response
   instead of a plan for `greeting.py`. The executor and the harness both accepted
   any non-empty text. Not diagnosed or fixed on-site (section 39).
3. **[INFRASTRUCTURE] Harness background wait is not robust.** The indefinite
   human-gate wait ran as a background process and was killed by the environment
   after ~26 minutes. A resumable/persistent gate is needed for future controlled
   runs (not a production bug).
4. **[HARNESS GAP] In-memory-only workflow state cannot survive a harness kill.**
   The coordinator/executor state (incl. the Step-2 baseline) lives in the harness
   process; an external kill loses it. Not a production defect — a harness design
   limitation for controlled runs.

## Go / No-Go

**NO-GO.**

The real Claude → Codex → Claude minimal chain did **not** work: Step 1 produced an
invalid plan, Step 2 (real Codex) spawned but never executed the task, Step 3 never
ran, and the workflow never reached SUCCEEDED. The primary real blocker is the Codex
TTY dependency in the detached-spawn path. Workflow Persistence must **not** proceed
until this blocker is resolved.

## Next phase suggestion

- Resolve the Codex interactive-TTY blocker as a separate Hotfix/next phase (e.g. host
  the native Codex surface in a real terminal — Windows Terminal via the existing `wt`
  path, or an interactive session — rather than a detached non-TTY spawn), then
  re-validate the real chain.
- Add plan-relevance acceptance to the smoke harness (reject a PLAN artifact that does
  not reference the task files), and make the human gate resumable across process
  death so a controlled run cannot be lost to an environment kill.
- Re-run Phase 9D.6 with the user present to operate/observe the Codex window.
