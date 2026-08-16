# Firefly AI Companion — Phase 7A upgrade

This upgrade turns the existing Claude/Codex status pet into an interactive
local AI launcher and lightweight Quick Ask panel.

## New interactions

- **Short left-click Firefly:** show/hide the Companion panel.
- **Left-drag:** move Firefly (click and drag are separated using Qt's drag threshold).
- **Right-click:** open the panel, launch Codex/Claude/ChatGPT, pause/resume, reset, quit.

## Companion panel

The panel provides:

- current resolved Agent + state;
- current workspace and up to five recent workspaces;
- **Open Codex** in the selected workspace;
- **Open Claude** in the selected workspace;
- **Open ChatGPT** in the system browser/app association;
- Quick Ask with **Codex** or **Claude**;
- Stop / clear controls;
- optional in-memory conversational context for the current pet process.

Quick Ask is intentionally read-only:

- Codex uses `codex exec --sandbox read-only --ephemeral ...`. If the selected
  workspace is not a Git repo, the launcher also supplies
  `--skip-git-repo-check` while keeping the sandbox read-only.
- Claude uses `claude -p --permission-mode plan --no-session-persistence`.

No OpenAI API key or Anthropic API key is introduced.

## Why `tools/run_cli.ps1` exists

On Windows, npm-installed CLIs are often `.cmd` files. `QProcess` ultimately
uses Windows process creation and should not build a shell command from the
user's prompt. The fixed PowerShell wrapper receives the executable and every
CLI argument as separate parameters and invokes the CLI with argument splatting.
User prompts are never concatenated into PowerShell source text.

## First run after upgrading

Use your existing virtual environment:

```powershell
cd E:\Firefly_AI_Pet
.\stop_pet.ps1
.\start_pet.ps1
```

Then short-click Firefly.

## Suggested Windows acceptance test

1. Drag Firefly: it should move without opening the panel.
2. Short-click Firefly: panel opens; click again or press Esc to hide it.
3. Choose a workspace, close/restart the pet, and verify it is remembered.
4. Click Codex / Claude: a terminal should open in that workspace.
5. Click ChatGPT: ChatGPT should open via the system URL handler.
6. Quick Ask Codex: ask `请只告诉我当前工作目录名称，不修改任何文件。`
7. Quick Ask Claude with the same question.
8. Run a Quick Ask and drag Firefly while it is running; UI should stay responsive.
9. Press Stop during a Quick Ask; the pet itself should remain running.
10. Verify your existing Claude/Codex hooks still drive the pet animations.

## Notes

- Quick Ask conversation history is held **in memory only**. It is not written to disk.
- The current implementation sends recent transcript text with the next Quick Ask
  when `上下文：开启` is enabled. It is not a persistent native Codex/Claude session yet.
- Closing/hiding the panel does not kill the pet. If a Quick Ask is running, it
  continues in the background; quitting the pet terminates it.
