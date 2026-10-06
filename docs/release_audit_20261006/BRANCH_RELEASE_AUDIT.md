# Incremental branch release audit

## Protected baseline and scope

Baseline: `df18cc414c7abcc92ff5a56480021c1b7b5ee8fd` (`main`).
Target: `recovery/original-feel-plus-features-20261006`.

Work was assembled in an isolated managed worktree. The source checkout remains on its original branch and HEAD, with existing WIP preserved. 4,980 generic source snapshot hashes were verified unchanged. No real user store was edited, imported, cleared or reset. No merge, tag or package release is part of this task.

## Published feature scope

| Module | Included |
|---|---|
| Vision | Camera FAST forwards full original Character style; existing provider routing and attachment paths |
| Memory | Confirmed mutations, supersede/history, authoritative identity recall, semantic-index lifecycle and UI confirmation |
| Model | Provider management, model profiles/router, fallback, credential resolver and GUI/CLI synchronization |
| Learning | Skill 2.0, context/panel, Visual Lab, curriculum interfaces, student presentation ACK and worker |
| Control | Status/model/control CLI and host IPC |
| UI | Existing console/chat integration, scrolling, SpeechBubble correction and crash diagnostics |
| Voice | Existing host/client/service integration; external service source and assets remain separate |
| Security | Paired PageLens access, MCP prevalidation, archive/path boundaries and architecture checks |

The full frozen-path classification is in [UPLOAD_FILE_AUDIT.md](UPLOAD_FILE_AUDIT.md). Retired Persona 2–8/reviewer/relationship/affordance research, private course drafts, runtime data and independent WIP were retained locally and excluded.

## Original Character invariant

All four Character YAML blobs match the baseline. Ordinary chat composes original Character, Bond, Memory, Narrative, conversation history and user input. It does not inject the retired Persona system or run a second reviewer. The public compatibility reader exposes only bounded existing data. Optional pacing behavior was retained without expanding its scope.

`tests/test_original_character_prompt_contract.py` checks actual provider-ready chat composition, Memory injection, Camera FAST style forwarding and baseline Character hashes. It tests ownership rather than romantic wording.

## Regression evidence and limits

Actual command receipts are published in [TEST_COMMANDS.json](TEST_COMMANDS.json). The final targeted groups cover Memory, Character/Conversation, Bond, Vision, Learning, Provider/Router, CLI, UI, Voice host and Security. Counts are per command and overlap; they must not be summed as unique tests.

- Memory/Character/Conversation/Bond/Suggestions: 787 passed across the final six groups.
- Learning: 1172 passed, 8 skipped, 3 Qt disconnect warnings.
- Provider/Model/CLI/Security: 209 passed; launcher portability retest 3 passed (2 overlap).
- Root receipted groups: Vision 222 passed; UI 63 passed/2 skipped; Voice 18 passed/15 skipped; Character/pacing 24 passed. Earlier broader selections are retained separately in the command report, with their missing command capture disclosed.
- The proposed CI extension file list, combined on local Windows: 243 passed, 2 warnings. This does not certify the remote Linux/Python matrix.

Initial failures were retained in local logs. They exposed optional asset omissions, obsolete experimental-reviewer expectations, non-isolated fixtures and test lifetime issues. Test fixtures were updated to current owner contracts; production Persona was not reconnected to satisfy them.

The per-batch staged whitespace gate rejected extra EOF blank lines in five candidate files. Only those EOF blank lines were removed in the release checkout; no functional source or original WIP was changed for that gate.

One earlier broad Qt test process terminated with a Windows access violation. A session-owned QApplication now survives the batch; the final Learning batch exited normally. This does not establish the cause of the historical desktop python313.dll access violation.

### Explicit limitations

- Full repository regression: NOT RUN; targeted regression only.
- New-branch real desktop, external provider, live camera/voice and formal teach-mcp session acceptance: NOT RUN.
- Live Voice and external MCP integration require explicit opt-in; they are skipped offline.
- The external Camera plugin's asynchronous device-probe revision is not bundled. Its acceptance test is explicitly skipped.
- Formal TutorTurn REMEDIATE resume remains unsupported; a full real adaptive course is not claimed complete.
- GitHub CI is separate from local Windows tests. The optional CI extension was not uploaded because current OAuth authorization lacks workflow-write scope. The unchanged baseline workflow triggers on main/PR, so this recovery-branch push does not trigger its matrix.

## Publication receipt

The seven feature commits below contain 385 changed paths, 58,665 insertions and 224 deletions. The documentation/CI/contract commit follows them. Every batch used an exact path list, checked staged names against it, and passed `git diff --cached --stat` / `git diff --cached --check` before commit. No bulk add was used.

| Commit | Module | Paths |
|---|---|---:|
| 688328e039ef9dd9bdd144b3f60b847743651ac4 | Provider/model routing | 19 |
| d733e6ed5f2a0c42be3a37d214e1f67272b0bb6d | Memory/identity lifecycle | 56 |
| 45a71db1bbc666f63e50566e3182294e499aa468 | Learning/presentation | 242 |
| 905247ee38a76074051c2d0a39911d9775d39050 | Vision Character context | 17 |
| f758c51c138eba26234e205f9588d337eca02c3b | Control CLI | 6 |
| 532a9ff04c6e4e96690d34f263dfc087680ef911 | Console/host UI | 16 |
| 9c975ef98623c8b4e6472885d5666339c63623cb | Archive/plugin/MCP boundaries | 29 |
| 4f0ae81a74e99189c85a1fe947ca789af7469a9b | Character contract, test isolation, privacy/branch audit and ignore rules | 13 |

### Verified publication

- The eight module/audit commits were pushed normally to the existing recovery branch, without force.
- After fetch, local HEAD and remote branch HEAD both equal `4f0ae81a74e99189c85a1fe947ca789af7469a9b`.
- Remote `main` remains `df18cc414c7abcc92ff5a56480021c1b7b5ee8fd`.
- The verified feature/audit diff is 398 changed paths, 67,646 insertions and 226 deletions. This documentation-only receipt follows it and changes only the two audit Markdown files.
- Original checkout: branch/HEAD unchanged; all 4,980 snapshotted generic file hashes still match. The 2,446 entries without source hashes were not opened by that check.
- The post-fetch full committed-tree scan has zero private-pattern matches and the same three known synthetic guard fixtures. Gitleaks scanning the new commit range has zero findings. See [PRE_PUSH_PRIVACY_AUDIT.md](PRE_PUSH_PRIVACY_AUDIT.md).
- Remote branch workflow runs returned an empty list. The unchanged main/PR workflow is not triggered by this recovery-branch push. The proposed expanded file list passed locally; remote expanded CI is NOT RUN.

The final documentation receipt is pushed and reverified by the release executor. Its exact tip is the latest commit on the branch; the final executor report records that SHA because a document cannot contain its own Git commit hash.

[Review branch](https://github.com/Liuyingandying/Firefly-AI-Pet/tree/recovery/original-feel-plus-features-20261006) · [Compare against main](https://github.com/Liuyingandying/Firefly-AI-Pet/compare/main...recovery/original-feel-plus-features-20261006)

### Final gate scope

| Gate | Status |
|---|---|
| BRANCH_PUSHED | YES |
| MAIN_UNCHANGED | YES |
| PRIVATE_DATA_LEAK | NO detected leak; all scanner findings individually reviewed |
| ORIGINAL_CHARACTER_CONTRACT | PASS (offline provider-ready ownership tests) |
| CAMERA_PERSONA_FIX | PASS (offline complete style-context propagation) |
| MEMORY_FEATURES | PASS (isolated owner/lifecycle/index/recall/UI tests) |
| LEARNING_FEATURES | PASS offline; LIMITED real formal-session acceptance |
| MODEL_ROUTER | PASS offline |
| VOICE_HOST | PASS offline; live host/device acceptance NOT RUN in this task |
| TARGETED_REGRESSION | PASS |
| FULL_REGRESSION | PARTIAL_PASS: whole-repository regression NOT RUN |
| EXPANDED_REMOTE_CI | NOT RUN: workflow extension excluded by authorization boundary |

No main merge, release tag, packaging or publication of v0.2 was performed.

### Workflow publication boundary

The initial push was rejected by GitHub before the remote branch changed. The unpublished documentation commit was safely replaced to omit the optional workflow edit; its original snapshot and patch remain local. The published history retains the main workflow without changes. No reset/clean, force push, alternate credential or main update was used.
