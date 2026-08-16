# Phase 9D.6-H2 — Managed Codex Exec + Plan Validation Hotfix: Outcome Report

**Date:** 2026-08-16
**Scope:** The five hotfix work items for the 9D.6 real-E2E blockers (Plan validity
gate, Workflow Plan/Review model pin, managed `codex exec` Implement transport,
Codex JSONL reconnect tolerance, staged resumable D.6 harness). No new product
feature, no workflow persistence, no new Workflow type.
**Verdict for D.6 retest: NO-GO** (implementation complete and offline-validated;
both agent backends are degraded in this environment today, so the controlled
online chain did not produce a working plan / a working implement).

---

## 1. PlanValidation model

`core/plan_validation.py` (new, Qt-free, stdlib-only, deterministic, zero LLM,
zero network). Vocabulary exactly as specified:

- `PlanValidationReason.EMPTY` — stripped text empty.
- `PlanValidationReason.TOO_SHORT` — below `MIN_PLAN_LEN = 80` chars.
- `PlanValidationReason.GENERIC_NO_TASK_RESPONSE` — a known clarification stub
  (audited list of ~17 surface phrases; not a refusal classifier).
- `PlanValidationReason.NO_TASK_REFERENCE` — the request had file-like tokens
  (`greeting.py`, `src/foo.py`, `README.md`, incl. path-like and case-insensitive
  matching) but the plan matched none. **Skipped entirely** when the request has
  no file-like tokens.

`PlanValidationResult` carries `valid`, `reason_codes`, `matched_task_terms`,
`length`. `validate_plan_text(plan_text, task_text)` is a pure function.

## 2. Real bad plan is blocked

The real 9D.6 bad plan (`"I don't see a specific task or request yet…"`) is
rejected by the gate (`GENERIC_NO_TASK_RESPONSE` + `NO_TASK_REFERENCE`). Wired
into `PlanStepExecutor._on_finished` before the artifact write: an invalid plan
fails Step 1 with **`error_code = "plan_invalid"`**, never writes a PLAN
artifact, never starts Step 2, never auto-retries.

Online proof: the controlled Plan smoke (below) produced the *same class* of stub
from the model; the gate rejected it (`plan_invalid`, Step 1 FAILED, zero
artifacts). **Success criterion 1 (bad Plan no longer SUCCEEDED) is met.**

## 3. Good plan passes

Unit-tested: `GOOD_PLAN` (a realistic plan referencing `greeting.py`) → `valid`
with `matched_task_terms == ("greeting.py",)`. Path-like tokens, case handling,
and the no-file-token-request case are covered. A good plan proceeds normally
(Step 1 → SUCCEEDED → Step 2 → …). *Online demonstration is pending the model
backend recovering (see §16).*

## 4. Workflow Plan model pin

`build_claude_args(..., model=None)` gains an optional per-invocation `--model`.
Workflow Plan passes `WORKFLOW_CLAUDE_MODEL = "opus"` (`ui/quick_chat_protocol.py`),
which the local provider env maps (`ANTHROPIC_DEFAULT_OPUS_MODEL` →
`deepseek-v4-pro`). Verified argv: `--safe-mode --model opus --permission-mode plan
--no-session-persistence`. `~/.claude/settings.json` is untouched.

## 5. Workflow Review model pin

`ReviewStepExecutor` passes the same `model=WORKFLOW_CLAUDE_MODEL`; the Review
argv is identical in shape to Plan (read-only, safe-mode, opus, no session
persistence). No online Review call was made this phase (§29 of the brief; the
Review path is validated by argv + tests).

## 6. Short Talk unchanged

`build_claude_args` adds `--model` **only** when a caller passes `model=`; the
default (no `model=`) never adds `--model`, so Short Talk keeps the default
Haiku/Flash semantics. Verified by `test_short_talk_model_semantics_unchanged`
and `test_external_short_talk_unchanged`.

## 7. Codex managed command

Workflow Implement now drives:

```
codex exec --sandbox workspace-write --json -c model_reasoning_effort=low
           --skip-git-repo-check --ephemeral <single prompt argv item>
```

through `QuickAskRunner` (existing owned `QProcess` + PowerShell shim + argv
transport). No `--dangerously-bypass-*`, no `--approve-for-me`, no `-a never`,
no `~/.codex` config change.

## 8. Sandbox policy

`--sandbox workspace-write` confines Codex writes to the workspace. The
Implement executor passes `sandbox="workspace-write"`; `build_codex_args` keeps
`read-only` as the default for every other codex path.

## 9. Approval tradeoff

Accepted explicitly. Non-interactive `codex exec` auto-executes model commands
under the native `workspace-write` sandbox with no live per-command approval
prompt. The product boundary remains the surrounding Firefly gates: Step 2 start
requires user confirmation and Step 2 completion requires a user confirmation
with workspace evidence. No command-level approval UI was added to the card
(section 23 of the brief) — the confirmation gate is step-level.

## 10. QProcess ownership

The managed exec uses the runner's owned `QProcess` (not `startDetached`). Firefly
reads stdout, reads stderr, captures the exit code, cancels the owned process
tree, and knows when the process finished. "Process spawned" is never a managed
execution state. Verified: the codex smoke observed the exec run to completion,
exit 0 captured, and the exec-finished state exposed.

## 11. JSONL adapter

`codex exec` stdout JSONL flows through the existing `CodexJsonlAdapter` →
`AgentEvent` path. The Implement executor never parses provider JSON (verified by
source scan + tests). Observed event types in the online smoke: `started`,
`session` (thread_id), `status`, `final`.

## 12. Reconnect handling

`CodexJsonlAdapter` now maps an `error` event whose message carries clear
transient-reconnect evidence (`"reconnecting"`) to `STATUS(reconnecting)` instead
of a hard `ERROR`; `turn.failed` and `error` events without reconnect evidence
still fail hard. The executor's `_saw_error` stays clear for a transient retry,
so a successful turn is not failed by proxy flakiness.

## 13. exec finished ≠ Step success

A managed exec finishing (exit 0 + `AgentEvent FINAL` + process finished) does
NOT succeed Step 2. The step stays RUNNING until the user confirms completion
(workspace snapshot diff + `USER_CONFIRMED` + CHANGED_FILES/IMPLEMENTATION_SUMMARY
artifacts). `exit 0` is a transport-success signal only, never code correctness.
Verified offline (63-test suite) and in the online smoke (`step2_state_after_exec:
running`, `completion.ok=false` with no changes).

## 14. Cancel

Cancel owns only the current executor's process tree: the runner's
`taskkill /PID <owned pid> /T /F` (existing project helper). No `taskkill` of all
`codex.exe` (source-verified). `app._on_workflow_cancel` stops the owned exec
then drops executor state and cancels the workflow.

## 15. D6 gate recovery

`tools/smoke_phase9d6_h2_staged.py` (new) replaces the infinite background gate
with three resumable stages: `--prepare`, `--run-plan-and-implement`
(real Claude Plan → gate → managed codex exec → save test-only state → exit),
`--continue` (reconstruct → user-confirmed completion → local test → optional
Claude Review). State lives in `runtime/e2e/` and is allowlisted (no API keys,
no transcripts, no session ids). No infinite `while True` gate.

## 16. Claude online calls

**2** (both the 9D.2 Plan smoke; the second was a diagnostic re-capture of the
same failure to record the rejected text). Result: the pinned model returned a
generic clarification stub (`"I don't see a specific task or request in your
message yet…"`) to a complete, valid prompt; the gate rejected it
(`plan_invalid`, Step 1 FAILED, no artifacts). This is a model/proxy-state
failure, not a transport/construction failure — the argv (`--model opus`) and the
gate are verified. **Per brief §27, an invalid Plan smoke ⇒ NO-GO for the D.6
retest.** No further Claude retry was made.

## 17. Codex online calls

**2** (both the new H2 codex exec smoke; the first had a harness-only `timed_out`
inversion bug, fixed and re-run once). Result: the managed exec ran to completion
(owned process, exit 0, JSONL parsed into session/status/final events, exec-finished
state exposed, Step 2 stayed RUNNING), but the agent executed **no tools / no file
changes** — `greeting.py` unchanged, local test still failing. Same environment
model/proxy degradation as §16 (per H1 §3.6, codex traffic is also proxy-routed).
The transport behaved correctly; the agent did not work.

## 18. Disposable smoke result

Disposable fixture workspaces only (system temp). Initial test FAILs as required.
The codex smoke workspace was inspected and cleaned up. The Plan smoke left no
artifacts (gate rejected before any write).

## 19. New / modified files

**New**
- `core/plan_validation.py` — deterministic Plan validity gate.
- `tools/test_phase9d6_h2_hotfix.py` — 63-test hotfix suite.
- `tools/smoke_phase9d6_h2_staged.py` — staged resumable D.6 harness.
- `tools/smoke_phase9d6_h2_codex_exec.py` — managed codex exec online smoke.
- `docs/PHASE9D6_H2_HOTFIX_REPORT.md` — this report.

**Modified**
- `core/agent_events.py` — `STATUS_RECONNECTING`.
- `core/agent_adapters.py` — transient-reconnect tolerance in `CodexJsonlAdapter`.
- `ui/quick_chat_protocol.py` — `build_claude_args(model=)`, `build_codex_args(sandbox=)`,
  `WORKFLOW_CLAUDE_MODEL`.
- `ui/process_launcher.py` — `ask(..., model=, sandbox=)`, stdin write-channel close
  for codex, reconnect constant.
- `ui/workflow_executor.py` — Plan gate + model pin.
- `ui/workflow_review_executor.py` — model pin.
- `ui/workflow_implement_executor.py` — rewritten to managed `codex exec`.
- `ui/workflow_card.py` — exec-finished state ("Codex finished · Review changes").
- `app.py` — cancel semantics (owns the process), `_on_implement_execution_finished`,
  shutdown stops the managed exec.
- `tools/test_phase9d2_plan_executor.py` — valid plan bodies for the gate.
- `tools/test_phase9d4_implement_workflow.py` — adapted to managed exec.
- `tools/test_phase9d5_review_workflow.py` — adapted flow (finish exec before complete).
- `tools/smoke_phase9d2_plan_executor.py` — diagnostic capture on failure.

## 20. New tests

`tools/test_phase9d6_h2_hotfix.py` — **63 tests** covering the 60 required cases
(Plan gate 1–16, model pin 17–23, codex managed exec 24–52, D6 harness 56–60)
plus regression assertions (SessionManager, Short Talk, 9C Open Codex, completion
semantics). All pass.

## 21. Regression

Full regression across all 26 test suites passes (`$env:PYTHONPATH=…; python
tools\<test>.py`): **26/26 suites, 0 failures**, including 9D.2/9D.3/9D.4/9D.5
with the new gate + managed exec, and 8C.2/8C.3/9C with the adapter / `ask()`
signature changes.

## 22. 9C interactive Open Codex regression

Unchanged and passing: `ProcessLauncher.launch_agent` and the interactive native
route are untouched; the Implement executor no longer uses them, but the 9C
handoff tests (`test_9c_send_to_codex_regression`, `test_send_to_codex_keeps_9c_contract`,
`test_phase9c_native_handoff`) all pass.

## 23. Global config untouched

`~/.claude/settings.json` and `~/.codex` are not modified. No `--dangerously-*`,
no approval bypass flags. `config/sessions.json` unchanged across the online
smokes.

## 24. GO / NO-GO for D.6 retest

**NO-GO** — not because the hotfix code is wrong (it is complete and offline-
validated, and the gate/transport behaved exactly as designed under real load),
but because **the environment's model backends are degraded today**: the pinned
Claude model returned a no-task clarification stub, and codex exec ran without
executing any work. Both are the same proxy/model-state class documented in the
9D.6-H1 decision. Per the brief (§27/§28), a non-working online Plan smoke and a
non-working codex Implement smoke mean the controlled D.6 retest cannot pass right
now. **Retest readiness:** re-run `tools/smoke_phase9d2_plan_executor.py` (expect
a valid plan accepted by the gate) and `tools/smoke_phase9d6_h2_codex_exec.py`
(expect `greeting.py` modified + test passing) once the proxy/model recovers, then
run the staged harness (`--prepare` → `--run-plan-and-implement` → `--continue
--review`). The hotfix is ready; the environment is the blocker.
