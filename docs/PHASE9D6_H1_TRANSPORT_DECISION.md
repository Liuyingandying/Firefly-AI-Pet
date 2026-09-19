# Phase 9D.6-H1 — Managed Codex Transport + Plan Validity Preflight: Decision

**Date:** 2026-08-16
**Scope:** Audit + local capability discovery + one controlled benchmark + architecture
decision. **No production code was modified** (`core/`, `ui/`, `app.py` untouched).
**Outcome: GO for Hotfix implementation** (details in section 15).

---

## 1. Executive Summary

Phase 9D.6 ended NO-GO for two independent findings:

1. **[BLOCKER] Native interactive Codex cannot run managed.** `QProcess.startDetached()`
   + `codex <prompt>` needs a real TTY (`Error: stdin is not a terminal`) and made zero
   workspace changes in 26+ minutes.
2. **[FINDING] The managed Claude Plan artifact was invalid.** The model returned a generic
   *"I don't see a specific task or request yet."* The executor accepted any non-empty text
   and Step 1 "SUCCEEDED" mechanically.

This preflight resolves both:

- **Transport:** `codex exec` (0.147.0, non-interactive) **does not require a TTY**, emits
  structured JSONL events with a `thread_id`, respects the native sandbox, fires the Firefly
  lifecycle hooks, and is a **viable managed Workflow backend**. A single controlled real
  smoke PASSED: `greeting.py` fixed, `test_greeting.py` untouched, `python test_greeting.py`
  passes, exit 0, no TTY error, no interactive approval required (see the documented approval
  tradeoff in section 6.4 / 7).
- **Plan:** deterministic argv forensics **proved the prompt transport is intact** (the exact
  406-char 9D.6 prompt arrives byte-for-byte at the final native process through the full
  PowerShell-shim chain). The failure is **model-level**: workflow Plan/Review steps run on
  the default `haiku`-class model via the local proxy and returned a clarification stub. The
  executor's success condition (non-empty text + artifact written) is too weak. A
  deterministic **Plan validity gate** (no LLM, no pseudo-NLP) rejects this class; a reference
  implementation was verified against the real 9D.6 artifact and a realistic good plan.

**Recommended managed backend: `codex exec --sandbox workspace-write --json`** for Workflow
Implement. **ConPTY and app-server: not worth adopting now.** Keep the interactive native
route for "Open Codex" (Scenario A); do not use it as a managed backend (Scenario B).

**Security posture preserved:** the smoke used `--sandbox workspace-write` and **no**
`--dangerously-bypass-*`, **no** `--approve-for-me`, **no** `-a never`. The one honest
tradeoff — non-interactive exec auto-executes commands under the sandbox without a live
per-command approval prompt — is recorded, not hidden.

---

## 2. 9D.6 Failure Evidence

Facts carried over from `docs/PHASE9D6_E2E_REPORT.md` (NO-GO), not re-argued:

- Transport PASS: direct native argv safely delivered the prompt; process spawned (PID 29144).
- Spawn PASS, **managed execution FAIL**: detached interactive Codex printed
  `Error: stdin is not a terminal`, made zero workspace changes in 26+ minutes,
  `runtime/sources/codex.json` never updated.
- Step 1 SUCCEEDED mechanically with an **invalid PLAN** (191 bytes, sha256
  `9d5576e0…`): *"I don't see a specific task or request yet…"* — no reference to
  `greeting.py`, the test, or validation.
- Harness gaps noted: non-empty + SUCCEEDED accepted the garbage plan; the human gate died to
  an environment kill after ~26 min; in-memory-only workflow state could not survive.
- Regression: 380 pre / 119 post tests all PASS. Production source untouched.

---

## 3. Codex CLI Capability Audit (real local help, codex-cli 0.147.0)

All items below come from `codex --version` / `codex --help` / `codex exec --help` /
`codex app-server --help` / `codex exec-server --help` / `codex exec resume --help` /
`codex review --help` run locally on 2026-08-16. Not from memory.

### 3.1 Top-level

- `codex [OPTIONS] [PROMPT]` — **interactive** TUI session with a positional prompt
  (this is the current 9C path that fails without a TTY).
- `codex exec` — **"Run Codex non-interactively"** (alias `e`). This is the managed candidate.
- `codex review` — non-interactive code review (alternative for a Review step, not adopted).
- `codex resume` / `exec resume` / `fork` / `archive` — session management.
- `codex app-server` (experimental), `codex exec-server` (EXPERIMENTAL), `codex mcp-server`.
- Top-level options include `-s/--sandbox <read-only|workspace-write|danger-full-access>`,
  `-a/--ask-for-approval <untrusted|on-request|never>`, `-C/--cd <DIR>`, `--add-dir`,
  `--skip-git-repo-check`, `--approve-for-me`, `--dangerously-bypass-approvals-and-sandbox`,
  `--dangerously-bypass-hook-trust`, `-c/--config key=value`.

### 3.2 `codex exec` (the relevant surface)

Usage: `codex exec [OPTIONS] [PROMPT]`. **No TTY requirement** (it is the non-interactive
mode). Prompt input: **positional argument**, or stdin (`-` / piped; if a prompt is also
given, piped stdin is appended as a `<stdin>` block).

Relevant options:
- `--json` — emit **events to stdout as JSONL** (thread.started / turn.started / item.* /
  turn.completed, incl. `thread_id` and `usage`).
- `-o, --output-last-message <FILE>` — write the agent's final message to a file.
- `-C, --cd <DIR>` — working root (the app currently relies on process cwd instead).
- `--skip-git-repo-check` — run outside a git repo (required for the disposable fixture).
- `--ephemeral` — no persistent session files (matches the workflow's transient policy).
- `-s, --sandbox <read-only|workspace-write|danger-full-access>` — the native sandbox.
  **`workspace-write` is the correct mode for an Implement step** (modify the workspace;
  writes outside the workspace are still confined).
- `-c model_reasoning_effort=<low|medium|high>` — config override (already used by the app).
- **No `--ask-for-approval` at exec level**; the top-level `-a` is accepted *before* the
  subcommand (`codex -a <policy> exec …` verified). Default (no `-a`) is the observed policy
  in section 6.
- `--approve-for-me` / `--dangerously-bypass-approvals-and-sandbox` /
  `--dangerously-bypass-hook-trust` — exist but are **NOT** to be used by Firefly (section 7).

### 3.3 `codex exec resume`

`codex exec resume [SESSION_ID] [PROMPT]` with `--last`, `--json`, `--ephemeral`,
`--skip-git-repo-check`. Provides **non-interactive session resume by thread/session id** —
the natural continuity story for a managed backend.

### 3.4 `codex app-server` / `exec-server`

- `app-server` is **experimental**: JSON-RPC-style server over `stdio://` (default),
  `unix://`, `ws://`, or `off`; daemon subcommands (start/stop/restart/bootstrap); TS/JSON
  Schema generation. Supports streaming turns, persistent sessions, cancel, and client-side
  approval — but is explicitly experimental and would make the host the approval authority
  (section 10).
- `exec-server` is EXPERIMENTAL: a standalone exec service over ws/stdio with
  `--concurrent-requests`, `--exit-on-stdin-close`. Not considered.

### 3.5 `codex review`

Non-interactive review against current changes/commits/branches. Not adopted (Step 3 stays
Claude Review per the current template).

### 3.6 Local config observed (`~/.codex/config.toml`)

- `model = "gpt-5.6-sol"`, `model_reasoning_effort = "high"`, `[windows] sandbox = "elevated"`.
- Hooks are wired and trusted (SessionStart/UserPromptSubmit/PreToolUse/PostToolUse/
  PermissionRequest/Stop/SessionEnd → `tools/simulate_event.py` → `runtime/sources/codex.json`).
- **Security observation:** `config.toml` carries a plaintext `ANTHROPIC_AUTH_TOKEN` plus a
  DeepSeek `ANTHROPIC_BASE_URL` in its `[shell_environment_policy.set]` block (the value is
  not reproduced in this document). It is the user's own proxy setup; noted here because it is
  an API credential stored in plaintext and it also demonstrates that agent traffic in this
  environment is proxy-routed (relevant to the Plan finding, section 11).

---

## 4. `codex exec` Assessment (15 audit questions from the preflight brief)

| # | Question | Finding |
|---|----------|---------|
| 1 | Needs a TTY? | **No.** Non-interactive by design. Smoke confirmed no `stdin is not a terminal`. |
| 2 | Non-interactive? | **Yes.** `codex exec` is the non-interactive mode. |
| 3 | How is the prompt input? | **Positional argument** (or stdin). Production already passes it positionally. |
| 4 | Positional arg? | **Yes.** |
| 5 | stdin? | **Yes** (`-` / piped; appended as `<stdin>` when a prompt is also given). |
| 6 | JSON/JSONL output? | **Yes** (`--json` → JSONL events incl. `thread_id`, `usage`). |
| 7 | Working directory? | `-C/--cd` and/or process cwd. Production uses cwd (QProcess). Smoke used cwd; worked. |
| 8 | Sandbox policy? | Native `--sandbox read-only|workspace-write|danger-full-access`. Implement needs `workspace-write`. |
| 9 | Approval policy? | No exec-level `-a`; top-level `-a` accepted before `exec`. Default observed = auto-execute under the sandbox (no live approval prompt) — tradeoff, section 6.4. |
| 10 | Exit code reliable? | **Yes.** Smoke: exit 0 on success; the `pytest` failure surfaced as exit 1 inside a `command_execution` item, not as a process failure. |
| 11 | Tool errors represented? | Structured: `item.started`/`item.completed` with `command_execution` `exit_code`/`status`, `file_change`, `agent_message`; provider errors as `error` events. |
| 12 | Thread/session id? | **Yes.** `thread.started` emits `thread_id` (smoke: `01a0062c-…`); `exec resume <id>` continues it. |
| 13 | Cancel? | No exec-level cancel flag. Managed cancel = process-tree kill (existing `taskkill /T /F` pattern in `QuickAskRunner.stop`) + later `exec resume <thread_id>`. |
| 14 | Hooks fire? | **Yes.** Smoke: `runtime/sources/codex.json` timestamp advanced via the trusted hooks. |
| 15 | Suitable managed executor? | **Yes** — the recommended backend, with the approval tradeoff recorded (section 6.4). |

---

## 5. Controlled Exec Smoke (ONE real online run)

Tool: `tools/benchmark_codex_transport.py` (new; no production code touched).
Workspace: disposable `C:\Users\<用户名>\AppData\Local\Temp\firefly_codex_exec_pjsgmvop`
(kept for inspection, fixture identical to 9D.6).

Command (production-shaped: QProcess-style pipes → `powershell.exe -File run_cli.ps1` →
`E:/npm-global/codex.ps1` shim → `node codex.js`):

```
codex exec --sandbox workspace-write --json --skip-git-repo-check
          -c model_reasoning_effort=low --ephemeral
          -o <ws>/_codex_last_message.md
          "Fix greeting.py so the existing test passes. Do not modify test_greeting.py."
```

### 5.1 Verdicts (preflight section 9 checklist)

| # | Check | Result |
|---|-------|--------|
| 1 | No `stdin is not a terminal` | **PASS** |
| 2 | Prompt genuinely received | **PASS** — agent inspected files, then fixed the real defect |
| 3 | `greeting.py` modified | **PASS** → `def greet(name):\n    return f"Hello, {name}!"\n` |
| 4 | `test_greeting.py` unmodified | **PASS** (byte-identical) |
| 5 | `python test_greeting.py` | **PASS** (exit 0) |
| 6 | Exit code | **0** (clean) |
| 7 | stdout/stderr | JSONL events on stdout; one benign stderr note (`Reading additional input from stdin…` — read EOF, no hang) |
| 8 | JSON/JSONL events | 19 events; `thread.started`→`thread_id`, `turn.started`, `item.*`, `turn.completed` with usage |
| 9 | Codex hook updated lifecycle | **PASS** — `runtime/sources/codex.json` advanced (source=hook) |
| 10 | User approval required | **NO** (see 5.2) |

### 5.2 Important nuances

- **4 `error` events were transient reconnect retries** (`Reconnecting… 2/5..5/5 (request
  timed out)`) followed by an automatic HTTPS fallback; the turn then completed normally.
  This is proxy/network flakiness in this environment, not a transport defect — but it is a
  real gap for a managed path: the current `CodexJsonlAdapter` maps `error` events to a hard
  `ERROR`, so `QuickAskRunner` would fail the turn even though the turn succeeded (Hotfix
  candidate #4, section 15.1).
- **No approval prompt was requested.** The agent read files, applied a `file_change`
  (workspace-write sandbox), ran `python -m pytest -q` (exit 1, pytest absent), then ran
  `python test_greeting.py` (pass). No `PermissionRequest`/approval event appeared. Under
  `codex exec`'s default policy, commands auto-execute **within the sandbox**; the sandbox
  still confined writes to the workspace (nothing outside the workspace was created/modified).
- **Elapsed 144.3 s** for a trivial fix — dominated by the reconnect backoff. Expect a managed
  step to take minutes on this proxy.

---

## 6. Approval / Permission / Sandbox posture (preflight section 7)

Firefly's workflow must keep Codex's native sandbox, approval, and permission policy. We did
**not** use `--dangerously-bypass-approvals-and-sandbox`, `--approve-for-me`, or `-a never`.

The honest finding: **`codex exec` (non-interactive) does not present a live per-command
approval prompt.** Default behavior auto-executes model commands inside the chosen sandbox.
The sandbox (`workspace-write`) is the confinement mechanism; the human approval boundary
shifts to the surrounding Firefly gates:

- **Pre-handoff:** Step 2 `confirm_step` (user confirms before Codex starts) — already in the
  product.
- **Post-handoff:** Step 2 `confirm_completion` (user confirms after scanning the diff) —
  already in the product.
- **Sandbox:** `--sandbox workspace-write` confines writes to the workspace.

This is a **tradeoff to accept or reject explicitly**: a managed `codex exec` step gives up the
live per-command approval of the interactive TUI. If the product requires per-command approval
*inside* Implement, that is a blocker for `exec` — the path would then be `codex app-server`
with Firefly as the approval authority (section 10). **Recommendation:** accept the tradeoff
for H1; the existing confirm-before/confirm-after gates plus workspace-write sandbox keep the
boundary the product already ships.

---

## 7. ConPTY Assessment (local architecture only, not implemented)

If one insisted on running the **interactive** Codex TUI programmatically, Windows ConPTY is
the *correct* primitive (it provides a real console to a detached child). But for Firefly:

- **Implementation complexity: high.** `CreatePseudoConsole` + pipe plumbing, VT/ANSI
  parsing, resize propagation, reaping, and process-ownership handling are all hand-rolled;
  PySide6/Qt has **no native ConPTY support**, so it must be wrapped in a custom device or
  worker thread.
- **Qt integration: poor** (no first-class widget; needs a custom VT renderer).
- **Cancel: possible** (write Ctrl+C into the pty) but extra plumbing.
- **Windows reliability: moderate.** ConPTY is stable, but edge cases (buffer resize, some
  full-screen apps) persist.
- **What it buys:** live interactive approval in-process and true TUI fidelity.

**Verdict: not worth it now.** `codex exec` eliminates the TTY requirement for the managed
path entirely, so ConPTY solves a problem the recommended backend already avoids. Revisit only
if Firefly must host the interactive TUI (Scenario A) *inside its own window*.

---

## 8. Windows Terminal Assessment

Phase 9C measured that `wt.exe` re-serialization corrupts parts of user prompts (embedded
double quotes and newlines), so `wt.exe + interpolated prompt` is not a safe managed prompt
transport. There is **no safe "attach a non-interactive pipe with a pre-typed prompt to wt.exe"
mode** — wt is a GUI terminal host, not a prompt-delivery transport.

**Verdict:** keep `wt.exe` for **Open Codex only** (Scenario A, no prompt payload). Do not use
it as a managed backend (Scenario B). This is consistent with 9C and unchanged.

---

## 9. Transport Matrix

| Criterion | Current interactive detached (9C) | `codex exec` | ConPTY + interactive | Windows Terminal (wt) | app-server |
|---|---|---|---|---|---|
| TTY requirement | **Yes** (blocker) | **No** | No (pty emulates) | No (GUI host) | No |
| Safe prompt transport | Yes (native argv) | Yes (positional/stdin) | Yes | **No** (9C corruption) | Yes (JSON-RPC) |
| Streaming | TUI only | JSONL events | TUI | TUI | JSON-RPC events |
| Structured events | No | **Yes** (`--json`) | No | No | **Yes** |
| Permissions/sandbox | Native | Native (`--sandbox`) | Native | Native | Native + client-side |
| Live approval | **Yes** (interactive) | No (auto-exec, sandbox confines) | **Yes** | **Yes** | **Yes** (Firefly = authority) |
| Cancel | kill (orphan risk) | process-tree kill + `exec resume` | pty Ctrl+C | close tab | **native cancel** |
| Session / resume | interactive resume | **`exec resume <id>`** | interactive resume | interactive resume | persistent threads |
| Implementation complexity | low (already built, broken) | **low** (reuse `QuickAskRunner`/`build_codex_args`) | **high** | low (built) | **high** (experimental server + protocol) |
| Windows reliability | fails (no TTY) | **good** (smoke PASS) | moderate | good (as terminal) | experimental |
| Workflow suitability | **FAIL** (proven) | **RECOMMENDED** | not now | not for managed | not now |

---

## 10. Recommended Managed Backend & Open-Native vs Managed Split

### 10.1 Scenario A — Open Codex (user-driven interactive)

Unchanged. Keep the interactive native route (`wt.exe`, no prompt payload). This is a product
surface for a human to drive Codex with live approval. **Not deprecated.**

### 10.2 Scenario B — Workflow Implement with Codex (Firefly-managed)

**Recommended backend: `codex exec`.**

```
codex exec --sandbox workspace-write --json --ephemeral --skip-git-repo-check
           -c model_reasoning_effort=low -o <lastmsg> <prompt>
```

- Non-TTY, structured JSONL (incl. `thread_id`), native workspace-write sandbox, reliable exit
  code, hooks fire, `exec resume <thread_id>` for continuity, process-tree kill for cancel.
- Firefly's human gate remains: **user confirms Step 2 → exec runs → user confirms
  completion** (baseline diff + `USER_CONFIRMED`, unchanged from 9D.4 semantics).
- The two backends are **not required to be the same**. Open stays interactive; Workflow goes
  non-interactive.

### 10.3 `app-server` — not now

`app-server` would genuinely add: native cancel, persistent sessions, streaming, and
**client-side approval** — i.e., Firefly would present Codex permission requests to the user.
That directly contradicts the current product principle (Codex keeps its own native approval
inside a native window; Firefly only gates before/after). It is also experimental and a
long-lived server process. **Verdict:** do not adopt now. Record as the future path *if* the
product evolves to make Firefly a Codex approval authority. The same reasoning demotes
`exec-server`.

---

## 11. Plan Failure Root Cause

Trace (TaskRequest → plan.md) with verdicts:

| Step | Code | Verdict |
|---|---|---|
| `TaskRequest.text` (harness) | `tools/smoke_phase9d6_end_to_end.py:244` | Correct task text (D ruled out) |
| `build_plan_prompt(request)` | `core/workflow_prompt.py:36` | Correct: `.format(task=task)` embeds task verbatim (A ruled out) |
| `QuickAskRunner.ask(prompt)` → argv | `ui/process_launcher.py:283`, `ui/quick_chat_protocol.py:108` | **Transport proven intact by deterministic argv forensics** (B ruled out) |
| PowerShell shim hop → claude.exe | `tools/run_cli.ps1` + `claude.ps1` | Forensics: 406-char multi-line prompt arrives **byte-for-byte** at the native process; all 12 CLI args preserved (B ruled out) |
| Claude response → AgentEvent FINAL | `core/agent_adapters.py:74` | Faithful: artifact text == extracted FINAL text (E ruled out) |
| PLAN artifact write | `core/artifact_store.py:83` | Correct: 191 bytes, sha256 recorded, file valid (F ruled out) |

**Root cause: model-level (C).** The managed Claude Plan call ran on the **default model
"haiku"** (`~/.claude/settings.json` → `"model": "haiku"`) mapped through the local proxy
(`http://127.0.0.1:15721`) to `deepseek-v4-flash`. `build_claude_args`
(`ui/quick_chat_protocol.py:108`) passes **no `--model`**, so workflow Plan/Review steps
inherit the haiku default. On 2026-08-16 that model returned a clarification stub
(*"I don't see a specific task or request yet…"*) to a complete, valid prompt. The 9D.2 online
smoke succeeded on the *identical* chain, so this is a **model-state/proxy variance**, not a
code bug. The executor's success condition (`_usable_text()` non-empty + artifact written,
`ui/workflow_executor.py:214-258`) is **too weak** to distinguish a plan from a clarification
stub → Step 1 SUCCEEDED mechanically with an invalid plan.

Supporting evidence this environment is proxy-flaky: the `codex exec` smoke observed 5
`Reconnecting… (request timed out)` retries before an HTTPS fallback (section 5.2).

**Conclusion:** not a transport bug, not a construction bug, not a harness bug. The defense is
a deterministic Plan validity gate (section 12) + pinning a capable model for workflow steps
(hotfix #2, section 15).

---

## 12. Plan Validation Contract

Deterministic, Qt-free, provider-free, no LLM, no pseudo-NLP. Reference implementation
verified in `tools/scratch_plan_gate_check.py`.

```python
# Proposed core/plan_validator.py contract (NOT implemented this phase)
REASONS = ("EMPTY", "TOO_SHORT", "NO_TASK_REFERENCE", "GENERIC_NO_TASK_RESPONSE", "VALID")

@dataclass(frozen=True)
class PlanValidationResult:
    valid: bool
    reason_codes: tuple[str, ...]
    matched_task_terms: tuple[str, ...]
    length: int

def validate_plan_text(plan_text: str, task_text: str) -> PlanValidationResult:
    ...
```

Rules (v1, deliberately minimal — gates garbage, never judges quality):
1. **EMPTY** — stripped text empty.
2. **TOO_SHORT** — length < 80 chars (a real plan with a heading is longer).
3. **GENERIC_NO_TASK_RESPONSE** — case-insensitive substring match on known surface
   patterns, e.g. `"i don't see a specific task"`, `"no task or request"`,
   `"what would you like me to"`, `"please provide a task"`, `"could you provide"`,
   `"as an ai language model"`.
4. **NO_TASK_REFERENCE** — extract **file-like tokens** from the request
   (`(?<![\w./\\])([A-Za-z0-9_][A-Za-z0-9_.\-]*\.[A-Za-z0-9]{1,10})(?![\w./\\])`,
   e.g. `greeting.py`, `test_greeting.py`) and require at least one to appear in the plan.
   If the request has no file-like tokens, this rule is skipped (the other three still apply).
   This is derived from the request — it does **not** hardcode this test.
5. Otherwise **VALID**.

Verified against: the real 9D.6 plan.md → `invalid` (`GENERIC_NO_TASK_RESPONSE` +
`NO_TASK_REFERENCE`); a realistic good plan → `valid` (matched `greeting.py`); empty/short
edges → correct.

### Failure semantics

If the managed Claude process exits 0 but `PlanValidationResult.invalid`:
- **Step 1 → FAILED** with error code **`plan_invalid`** (the coordinator already supports
  `mark_step_failed` → workflow FAILED, later steps SKIPPED; no new workflow state needed).
- **No automatic Claude retry. No Codex continuation.** The user sees a failed Plan step.
- Optional UX nicety (not required): surface the `reason_codes` in the card.

---

## 13. Hotfix Plan (implementation order — NEXT phase, not this one)

1. **Plan validity gate** (small, low-risk, deterministic): add `PlanValidationResult` +
   `validate_plan_text` (Qt-free) + wire into `PlanStepExecutor._on_finished` before the
   artifact write → fail `plan_invalid` on invalid. Add unit tests incl. the real 9D.6
   artifact. This immediately turns silent garbage into a visible FAILED step.
2. **Pin a capable model for workflow Plan/Review steps** (small): pass `--model` (e.g.
   `sonnet`, or a configured workflow model) for transient workflow calls only, leaving
   ordinary Quick Ask on its default. Directly reduces the garbage-input class. **Product
   decision:** which model tier is acceptable cost/quality for workflow steps.
3. **Codex exec transport for Implement** (larger):
   - Parameterize `build_codex_args` sandbox (keep `read-only` default for Quick Ask; add
     `workspace-write` for the workflow Implement step), or add a workflow-specific builder.
   - **Close the stdin write channel after start.** `codex exec` reads stdin and can block
     waiting for it (Phase 8C observation). The smoke's pipe hit EOF and did not block, but
     `QProcess` leaves stdin open by default — the managed executor must close it (or point it
     at a closed/null handle) immediately after `start()`.
   - `--skip-git-repo-check` is required to run outside a git repo (the disposable fixture and
     non-git user workspaces). It is **not** a trust/sandbox bypass — the sandbox and approval
     policy still apply; production `build_codex_args` already adds it conditionally. Keep it.
   - Adapt the Implement executor to drive `codex exec` to completion via
     `QuickAskRunner`-style pipes, capture JSONL + `thread_id` + last message, and keep the
     user-confirmation completion gate (baseline diff + `USER_CONFIRMED`, 9D.4 semantics).
     Optional later: `ARTIFACT_VALIDATED` on non-empty diff (product decision).
   - Cancel = process-tree kill + optional `exec resume <thread_id>`.
4. **Adapter reconnect tolerance** (small, correctness): teach `CodexJsonlAdapter` to treat
   `Reconnecting…` events as `STATUS(reconnecting)` instead of hard `ERROR`, so a transient
   proxy timeout does not fail a successful managed turn (observed in the smoke).
5. **Harness hardening** (for the retest): make the D.6 human gate resumable across process
   death and add the plan-relevance acceptance (the gate makes this natural).

Order rationale: the gate is cheap and fixes the correctness class; the model pin reduces
garbage at the source; the transport is the bigger lift and unblocks the E2E; the adapter
tolerance keeps the flaky proxy from failing successful runs.

---

## 14. Retest Plan

Component checks before the full E2E (each is one online call, budgeted):

1. After gate + model pin: re-run a 9D.2-style Plan smoke (1 Claude call) — expect a valid
   plan that the gate **accepts**.
2. After transport: re-run `tools/benchmark_codex_transport.py` (1 codex call) with the
   production-shaped invocation — expect the same PASS as section 5.

Then **full Phase 9D.6 E2E** (Claude Plan → `codex exec` Implement → user confirm → Claude
Review) with the updated harness. **GO only if both component checks pass first.** This is
required before Workflow Persistence (section 15.4).

---

## 15. Final Verdicts (preflight section 22)

1. **Current interactive detached as a managed backend?** Deprecate for Scenario B; the 9D.6
   blocker is structural (needs a TTY).
2. **Open Codex interactive route?** Keep (Scenario A, `wt.exe`, no prompt payload).
3. **`codex exec` needs a TTY?** **No** (proven).
4. **`codex exec` suitable for managed Workflow?** **Yes** — recommended, with the approval
   tradeoff (section 6) accepted explicitly.
5. **Implement ConPTY?** **No** — it solves a need the recommended backend avoids; high cost.
6. **Use app-server now?** **No** — experimental and would make Firefly the approval authority,
   changing the product principle. Revisit if that changes.
7. **Recommended Step 2 backend?** `codex exec --sandbox workspace-write --json --ephemeral
   --skip-git-repo-check -c model_reasoning_effort=low -o <lastmsg> <prompt>`.
8. **Plan failure root cause?** Model-level (C): default `haiku`-class model via the proxy
   returned a clarification stub to a valid prompt; executor success condition too weak.
   Transport/construction/collection/write all ruled out by code review + deterministic argv
   forensics.
9. **Plan validity gate?** Deterministic `PlanValidationResult` (section 12); invalid → Step 1
   FAILED `plan_invalid`; no retry, no Codex.
10. **Hotfix order?** Plan gate first, then model pin, then `codex exec` transport, then
    adapter reconnect tolerance.
11. **Re-run full D.6?** **Yes** — after hotfixes 1–3 and both component smokes pass.
12. **Before persistence, still missing?** (a) hotfixes landed + component smokes; (b) full
    D.6 E2E PASS; (c) explicit acceptance of the exec approval tradeoff; (d) decision on Step 2
    completion (user-confirmed vs `ARTIFACT_VALIDATED` on diff); (e) managed cancel/resume
    semantics agreed; (f) adapter reconnect tolerance (or accept flaky-proxy failures);
    (g) workflow-state persistence to be built in the next phase on the now-working chain.

**GO / NO-GO for Hotfix implementation: GO** (subject to the explicit tradeoff acceptance in
6/15.12c and the model-tier decision in hotfix #2).

---

## 16. Non-Goals

- No production code modified this phase (`core/`, `ui/`, `app.py` unchanged; verified).
- No ConPTY implementation.
- No app-server/exec-server integration.
- No Workflow Persistence work.
- No `--dangerously-bypass-*` / `--approve-for-me` / `-a never`.
- No second online codex smoke; no Claude call was made during this preflight.

## 17. Files Added (this phase)

- `docs/PHASE9D6_H1_TRANSPORT_DECISION.md` (this document).
- `tools/benchmark_codex_transport.py` — controlled `codex exec` smoke (the one online run).
- `tools/benchmark_plan_argv_delivery.py` — deterministic Claude Plan argv forensics (offline).
- `tools/scratch_plan_gate_check.py` — verified reference draft of the Plan validity gate.

`runtime/sources/codex.json` was updated by the smoke's own Codex hooks (expected — it is the
lifecycle bridge working as designed); no production source file changed.
