# Firefly AI Companion — Phase 7B Fast Chat

Phase 7B focuses on Quick Ask latency and conversational feel without adding API keys.

## What changed

- Codex Quick Ask now uses JSONL events and remembers the `thread_id` per workspace.
- Follow-up Codex questions use `codex exec resume <thread_id>` instead of rebuilding the entire chat prompt.
- Claude Quick Ask now uses `stream-json` with partial messages and remembers `session_id` per workspace.
- Follow-up Claude questions use `claude -p --resume <session_id>`.
- Quick Ask has three effort presets: Fast=`low`, Standard=`medium`, Deep=`high`.
- Default is Fast.
- Added elapsed-time and progress status messages.
- Added `New chat`, which resets the selected Agent's session for the current workspace.
- Turning continuous conversation off makes each request independent (`--ephemeral` for Codex, `--no-session-persistence` for Claude).
- Manual history concatenation was removed, reducing repeated prompt size.
- Quick Ask remains read-only: Codex uses `--sandbox read-only`; Claude starts in `--permission-mode plan`.

## Important limitation

The GUI still launches a CLI process for each turn. Persistent sessions avoid re-sending conversation history and improve cache/context reuse, but they do not eliminate CLI startup or network latency. If that remains the dominant delay, a future phase can replace per-turn CLI launches with a long-lived app-server / Agent SDK connection.

## First tests on Windows

1. Start Firefly and open the Companion panel.
2. Keep `连续对话：开启` and `模式：⚡ 快速`.
3. Ask Codex a short question twice. The second request should say it is continuing the conversation.
4. Ask Claude a short question. Text should begin appearing while the answer is generated.
5. Click `新对话`, then ask again; a new session should be created.
6. Switch workspace and verify sessions are isolated by workspace.
7. Toggle continuous conversation off and verify one-shot mode still works.
8. Run `verify_phase7b.ps1` if needed.
