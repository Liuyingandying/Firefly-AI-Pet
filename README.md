# Firefly AI Companion — Core v1 + Phase 7A

A frameless, transparent, always-on-top Windows desktop companion built with
Python 3 and PySide6. A pure-Python state broker merges per-agent source
states (Claude, Codex, manual) into one display state that drives a looping
GIF animation. A short click now opens an interactive Companion panel for
workspace selection, launching Codex / Claude / ChatGPT, and local Quick Ask.

## Companion panel (Phase 7A)

- **Short left-click:** show/hide the Companion panel.
- **Left-drag:** move Firefly without opening the panel.
- Pick a workspace and keep up to five recent workspaces.
- Open **Codex** or **Claude Code** in the selected workspace.
- Open **ChatGPT** through the system URL handler.
- Quick Ask with Codex or Claude without adding any API key.
- Optional in-memory chat context makes successive Quick Asks conversational.

Quick Ask is intentionally conservative: Codex runs with a read-only sandbox;
Claude starts in plan permission mode. Use the full Codex/Claude buttons when
you want an agent to edit a project. See `README_PHASE7A.md` for the Windows
acceptance checklist and implementation notes.

## Requirements

- Windows
- Python 3 (tested on 3.13.9)
- PySide6 (installed into the project `.venv`)

## Setup

```powershell
cd E:\Firefly_AI_Pet
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Run

```powershell
.\start_pet.ps1
```

The pet appears at the bottom-right of the primary monitor. It is
frameless, transparent, always on top, and can be dragged with the left
mouse button. Short-click it to open the Companion panel.

## Stop

```powershell
.\stop_pet.ps1
```

## Right-click menu

| Item            | Action                              |
|-----------------|-------------------------------------|
| Open Companion Panel | Show/hide the AI launcher and Quick Ask UI |
| Open Codex / Claude / ChatGPT | Launch the selected AI entry point |
| Pause / Resume  | Freeze / resume the current animation |
| Reset position  | Move back to bottom-right of screen |
| Quit            | Exit the pet                        |

## Simulating state changes

```powershell
# manual override (writes runtime/sources/manual.json, effective ~10 s)
.\.venv\Scripts\python.exe tools\simulate_event.py working

# per-agent
.\.venv\Scripts\python.exe tools\simulate_event.py working --agent claude
.\.venv\Scripts\python.exe tools\simulate_event.py waiting --agent codex
.\.venv\Scripts\python.exe tools\simulate_event.py error --agent claude --source hook --silent
```

## Claude Code Hooks

Hooks are configured in the user-global `~/.claude/settings.json`, so the
pet responds to Claude Code in every project and directory. They observe
Claude Code lifecycle events and write the matching pet state. They never
block Claude and never change its permission decisions.

| Event                | State      | Animation          |
|----------------------|------------|--------------------|
| `SessionStart`       | `idle`     | `idle.gif`         |
| `UserPromptSubmit`   | `thinking` | `review.gif`       |
| `PreToolUse`         | `working`  | `running.gif`      |
| `PermissionRequest`  | `waiting`  | `waiting.gif`      |
| `PostToolUseFailure` | `error`    | `failed.gif`       |
| `StopFailure`        | `error`    | `failed.gif`       |
| `Stop`               | `success`  | `waving.gif`       |
| `SessionEnd`         | `sleeping` | `idle.gif` (frozen) |

`PreToolUse` is limited by `matcher: Edit|Write|Bash|NotebookEdit`, so
read-only tools (`Read`, `Glob`, `Grep`) do not trigger `working`.

Hook commands use absolute forward-slash paths to the venv Python and
`simulate_event.py`, so they keep working regardless of the session cwd.
Each hook runs `simulate_event.py <state> --agent claude --source hook --silent`,
writing `runtime/sources/claude.json`, printing nothing, and always exiting 0.

## State → animation mapping

| State      | Animation      | Notes                          |
|------------|----------------|--------------------------------|
| `idle`     | `idle.gif`     |                                |
| `thinking` | `review.gif`   |                                |
| `working`  | `running.gif`  |                                |
| `waiting`  | `waiting.gif`  |                                |
| `success`  | `waving.gif`   | auto-returns to `idle` after ~3 s |
| `error`    | `failed.gif`   | auto-returns to `idle` after ~4 s |
| `sleeping` | `idle.gif`     | frozen on the first idle frame |

## Project structure

```text
E:\Firefly_AI_Pet
├── .claude
│   └── settings.local.json # empty; hooks live in user-global settings.json
├── assets
│   └── animations          # copied GIF assets
├── runtime
│   ├── sources             # per-agent source states
│   │   ├── claude.json
│   │   ├── codex.json
│   │   └── manual.json
│   └── state.json          # resolved display state (written by app.py)
├── ui
│   ├── companion_panel.py  # interactive launcher + Quick Ask UI
│   ├── process_launcher.py # safe QProcess launch / Quick Ask
│   └── workspace_store.py  # recent workspace persistence
├── config                  # local UI settings (created at runtime)
├── tools
│   ├── run_cli.ps1         # safe Windows .cmd/.bat argv bridge
│   ├── simulate_event.py   # write a state into runtime/sources/<agent>.json
│   ├── stop_pet.py         # graceful shutdown client
│   ├── test_phase7a_core.py # Qt-free Phase 7A core tests
│   └── test_state_broker.py # broker unit tests
├── state_broker.py         # pure-Python multi-agent resolver
├── app.py                  # the desktop pet
├── start_pet.ps1           # launch the pet
├── stop_pet.ps1            # stop the pet
├── requirements.txt
└── .gitignore
```

## How it works

- Each agent (Claude, Codex, manual) writes only its own
  `runtime/sources/<agent>.json` atomically.
- `app.py` polls the sources every 150 ms and calls `state_broker.resolve()`,
  which merges them into one display state by priority + newest timestamp.
- `app.py` writes the resolved result to `runtime/state.json` and plays the
  matching animation. It only acts when the resolved state changes.
- Corrupt / half-written / missing source files are ignored without crashing.

## State broker

`state_broker.py` merges per-agent source states into one display state.

- Priority (high → low): `waiting`, `error`, `success`, `working`,
  `thinking`, `idle`, `sleeping`.
- Same priority: the agent with the newer timestamp wins.
- Transient TTL: `success` → `idle` after ~3 s, `error` → `idle` after ~4 s.
- Manual override TTL: the `manual` source stops affecting resolution after 10 s.
- Stale fallback: `working`/`thinking`/`waiting` older than 30 min are
  demoted to `idle` (crash recovery; normal `SessionEnd` → `sleeping` is the
  primary path).

## Process management

- Single instance is enforced with a Windows named mutex (auto-released by
  the OS on exit, so there is never a stale lock).
- `start_pet.ps1` launches `app.py` detached via `pythonw.exe` and returns
  immediately; the pet keeps running after the launching shell exits.
- `stop_pet.ps1` sends a `quit` command over the pet's `QLocalServer`
  control channel — it never kills processes by PID or guesses at
  `python.exe`.
- `runtime/pet.pid` records the running instance's PID for observability and
  is removed on graceful shutdown.

## Roadmap

- **Done (Phase 4):** `error` → `failed.gif` is wired to the official
  `PostToolUseFailure` and `StopFailure` events (no Bash-output parsing).

## Releases

### [v0.1.0-firefly-demo — Birth of Companion](docs/releases/v0.1.0-firefly-demo.md)
