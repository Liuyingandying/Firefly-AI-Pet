# Firefly Interactive Learning — Real TUI Spawn Audit

Date: 2026-09-27. Scope: Firefly → Z Code interactive terminal handoff only.

## Real click and persisted course

The production log at `%LOCALAPPDATA%\FireflyAI\logs\learning_entry.log` identifies PID 31588 as the loaded Firefly runtime at 12:10:37. The 12:11:00 ability-panel click opened the Learning Bridge dialog. At 12:11:03 the log records `course_id=crs-d91e981f5a92`, `action=new`, workspace `C:\Users\FAJ\AppData\Local\FireflyAI\learning\courses\crs-d91e981f5a92\workspace`, headless task `task-8ec9a62b37`, and `[ZCODE_PROCESS_STARTED] pid=39084 mode=task-injected`. Thus this particular click took the **new/init** path, despite the user's description of pressing “继续学习”; it did not hit the reuse gate. PID 39084 was the headless init process, not a TUI.

At 12:14:18 the same log records `[ZCODE_SESSION_OPEN]` for `sess_0d75c470-cacd-465f-99e3-c319f4c81af4`, followed by a Firefly message. The old marker was written **before** `_spawn_detached` and recorded no argv, PID, or exit status. It cannot prove that a terminal appeared. There is no contemporaneous `ZCODE_SESSION_REUSE` or `ZCODE_SESSION_REBUILD` marker for the 12:11 click.

The real `manifest.json` identifies the course as “RC 电路充放电”. Its `bridge/course_binding.json` contains the same `zcode_session_id`. A read-only query of `~/.zcode/cli/db/db.sqlite` found that exact session in Z Code's `session` table, with `directory` equal to the course workspace and a Firefly learning bootstrap title.

## Spawn path and direct probes

Production code executes `BridgeLearningSession._poll()` → `open_learning_session()` after reading `init.json`. On reuse, `launch_learning_mode()` executes `open_learning_session()` before returning. The empty `LaunchPlan.argv` on reuse means **no new headless init command**; it is not the interactive argv. The original code did call `_spawn_detached`, but discarded its PID on reuse and treated the call as proof of opening.

Resolved intended interactive argv on this machine:

```text
"C:\Users\FAJ\AppData\Local\Microsoft\WindowsApps\wt.exe"
  -d "C:\Users\FAJ\AppData\Local\FireflyAI\learning\courses\crs-d91e981f5a92\workspace"
  "E:\Node\node.exe"
  "C:\Users\FAJ\AppData\Local\Programs\ZCode\resources\glm\zcode.cjs"
  --resume "sess_0d75c470-cacd-465f-99e3-c319f4c81af4"
```

These are separate argv elements with `shell=False`. `open_learning_session()` builds an env from `os.environ` and calls `_ensure_zcode_provider_env()` for both `ZCODE_BUILTIN_PROVIDER_CONFIG_FILE` and `ZCODE_PERSONAL_PROVIDER_CONFIG_FILE`. The earlier headless success alone did not establish that, but the interactive production code and regression test do.

An independent PowerShell invocation of this `wt.exe` argv returned 0. After two seconds, the existing `WindowsTerminal.exe` process remained, but no `node.exe ... zcode.cjs --resume <session>` process was found. Invoking the same `node zcode.cjs --resume` directly in a TTY without the provider env exited 1 with “无法定位 CLI ZCode Built-in Provider Config”. With both provider env vars set to the installed official config paths, it exited 1 with:

```text
Error: Cannot find package '@zcode/tui' imported from C:\Users\FAJ\AppData\Local\Programs\ZCode\resources\glm\zcode.cjs
```

The installed `resources\glm` has `zcode.cjs` and plugin packages, but no resolvable `@zcode/tui`; no dedicated `zcode-cli` wrapper was found on PATH. Calling the pre-fix production `open_learning_session()` independently returned terminal-launcher PID 37368, yet no matching Node TUI process remained after three seconds. That PID was the `wt.exe` launcher, not evidence of a live TUI. Its exit code was not captured. The direct Node probe's exit code was 1.

## Root cause and bounded repair

The installed Z Code desktop CLI bundle can execute headless bootstrap, but its interactive `--resume` path imports an absent `@zcode/tui` package. Consequently the terminal command exits before a usable TUI persists. The old `[ZCODE_SESSION_OPEN]` and submitted message concealed that failure. A second possible issue—whether an existing Windows Terminal instance forwards the caller's provider env—cannot be resolved until a complete TUI runtime is available; it is not needed to explain the observed failure.

The minimal code repair checks Node resolution of `@zcode/tui` from the actual CLI directory before launching the terminal. If unavailable, it raises `ZCODE_TUI_RUNTIME_MISSING`, logs `[ZCODE_TUI_PREFLIGHT_FAILED]`, and makes the Firefly bridge show “Z Code 学习空间打开失败” instead of a false opened message. When the preflight succeeds, the launcher logs `[ZCODE_TUI_SPAWN_ATTEMPT]` with argv and provider-env presence and `[ZCODE_TUI_PROCESS_STARTED]` with the **terminal launcher** PID. Reuse now returns that PID. The status text asks the user to confirm the course TUI rather than equating `Popen` with visible success. No teach-mcp, learning state, Resource Manifest, or Return Channel logic changed.

Focused tests: `tests/test_real_tui_spawn.py`, `tests/test_zcode_cli_provider_env.py`, and `tests/test_learning_return_delivery.py`: **19 passed**. They verify that existing-session reuse reaches the production interactive spawner with exact workspace, Node, CLI, `--resume` session ID, and provider env, plus error delivery when the TUI runtime is absent. They are not desktop E2E evidence.

## Required acceptance answers

| Question | Evidence-based answer |
|---|---|
| 1. Latest real click: reuse or rebuild? | Neither: the 12:11 log says `action=new` and starts a headless init. |
| 2. Did `open_learning_session` run? | The 12:14 marker and code path show it was entered; the old log cannot prove spawn completion. A later direct production call was independently executed. |
| 3. Actual spawn argv? | Historical click: not logged. The resolved production argv is shown above; an independent `wt.exe` probe used it. |
| 4. Provider env on interactive spawn? | Production code injects both variables, verified by test. Direct CLI without them fails earlier. |
| 5. Real PID? | Headless init PID 39084 in the click log. Independent terminal-launcher probe PID 37368. No historical TUI PID or persistent Node TUI PID was established. |
| 6. Did PID exit immediately? | The `wt.exe` launcher PID exit code was not captured; no matching Node TUI remained after three seconds. Direct Node `--resume` exited 1. |
| 7. Root cause? | Installed Z Code CLI lacks resolvable `@zcode/tui`; interactive `--resume` exits before displaying a usable classroom. |
| 8. Minimal repair? | Runtime preflight, truthful failure display, spawn markers, reuse PID propagation, and focused regression coverage. The missing external TUI package was not copied or fabricated. |
| 9. Did the user see the correct course TUI? | No verified sighting; the reported desktop attempt showed none, and the direct CLI probe failed. |
| 10. Did a second continue reopen the same session? | Not verified in the real desktop. The binding and Z Code DB still reference the same session; the code/test reuse path preserves it, but visual E2E is blocked. |

`process_opened` and `learning_visible_in_zcode` are unverified/failed for a usable interactive classroom. The six-part final gate is therefore unmet. A complete, official Z Code interactive CLI/TUI runtime is required before repeating the clean Firefly click, visual course check, answer in TUI, close, and same-session reopen. Existing unrelated terminal windows and Firefly background processes were left running because this blocker prevents a meaningful PASS run.

INTERACTIVE_TUI_E2E_BLOCKED
