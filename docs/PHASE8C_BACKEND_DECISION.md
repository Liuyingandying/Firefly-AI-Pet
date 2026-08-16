# Phase 8C — Agent Communication Backend: Benchmark & Architecture Decision

> 审计 + 测量 + 决策文档。Phase 8C Preflight，只读阶段。
> 生产代码零改动。仅新增本文件、`tools/benchmark_phase8c_backend.py`、`docs/phase8c_benchmark_results.json`。
> 日期：2026-08-15。Claude 2.1.233 · codex-cli 0.147.0 · PowerShell 5.1.26100.9168。

---

## 1. Executive Summary

Firefly Quick Ask 目前采用 **每轮全新进程链**：

```
QProcess → powershell.exe → run_cli.ps1 → claude.CMD / codex.CMD → cmd.exe → 原生 CLI → 网络 → 模型
```

本轮实测把一个**极短** Claude 请求（`Reply exactly: OK`，effort low，经本地代理 → DeepSeek V4 Flash）完整跑完：
**冷启动 total ≈ 6.5s，--resume ≈ 4.2s**。

延迟分解的结论非常明确：

| 阶段 | 实测值（Claude 冷启动，中位数） |
|---|---|
| 进程链 + CLI 启动 + session 初始化（spawn→first event） | **≈ 0.85s** |
| 网络 + 模型首 token（first event→first text） | **≈ 4.6s** |
| 收尾（first text→total） | ≈ 1.2s |
| PowerShell wrapper 相对直连原生 exe 的总开销 | **≈ 0.13–0.29s（约 2–5%）** |

因此：

1. **之前观察到的 Quick Ask 等待 100s+，主要不是进程/包装层造成的。** 进程链全部本地工作 <1s；剩下的全部是网络 + 模型/代理时间。真实提问（触发工具、读文件、长输出、代理排队）主导了那 100s。
2. **PowerShell wrapper 不是瓶颈**（约 0.13–0.29s，占 2–5%，在 100s 请求里占 0.2%）。不要为了“优化”先删 wrapper。
3. **`--resume` 是当前唯一显著可用的低延迟杠杆**：total 6.5s→4.2s（省 ~35%），且全部省在模型阶段（`ttft` 4338ms→1952ms，典型 prompt-cache 命中）。**session reuse ≠ process reuse，成立** —— resume 不省任何进程启动（first_event 851ms vs 923ms）。
4. Claude CLI（2.1.233）**没有暴露**长驻 server 接口。Codex CLI（0.147.0）暴露了 **`app-server`（experimental）**，本轮用其本地 `generate-json-schema` 拿到了**真实协议证据**：JSON-RPC + 长驻 daemon + 流式事件 + thread/turn 生命周期 + `TurnInterrupt` 取消 + 审批请求发往客户端。
5. Claude Agent SDK **当前环境未安装**，且无可用的本地接口证据 → 标记 `Needs separate official-doc verification`。
6. **权限所有权必须保持原生 Agent**。Firefly 维持 observer-only（检测/通知/聚焦）。app-server 会把审批请求发给客户端 —— 如果 Firefly 成为 app-server 客户端，就会被迫成为 approval authority，这是明确成本/风险。

**推荐（由实测推导）：**

- **Claude**：每轮原生 `claude.exe` 直连（去掉 powershell/cmd 层）+ `--resume`，SessionManager 继续持有 session id。预期 trivial prompt 冷 ≈ 6.3s / resume ≈ 4.2s；真实提问耗时由模型主导，任何 backend 都无法改变单请求模型延迟。
- **Codex**：`app-server` 是唯一真正的长驻候选（有协议证据），但 experimental + 审批责任转移 + 当前 token 紧张 → 放到受控 prototype（后续切片），本轮不做在线请求。
- **Firefly UI**：保留“Short Ask / Command entry”作为轻量入口；**不要**把 Firefly 做成完整聊天宿主。长对话交给原生界面（Claude Code / Codex）。
- **Session ownership**：SessionManager。
- **Permission ownership**：原生 Agent（Firefly 永不批准）。
- **Future Router**：Provider adapter 接口（只设计，不实现）。

Phase 8C.1 最小实施切片见第 17 节。**本阶段不做任何实现。**

---

## 2. Current Quick Ask Architecture

### 2.1 关键审计发现

- `QuickAskRunner` 位于 `ui/process_launcher.py:97`，被 `ui/companion_panel.py:27,45` 构造。
- **重要：当前运行的 VisualShell（`app.py`，Phase 8A.2+）没有导入/显示 CompanionPanel**（`app.py:5` 注释明确写了 legacy CompanionPanel 保留但未导入）。也就是说 **Quick Ask 链路在当前版本处于休眠状态**；生产里只有 `ProcessLauncher.launch_agent`（PermissionCard “View” / session 续接）在跑。
- 本轮 benchmark 直接测量的是 Quick Ask 底层那套进程链，因此结论对重新启用它仍然成立。

### 2.2 真实调用链（Claude / Codex 相同外壳）

```
用户 Ctrl+Enter
  → CompanionPanel._send_quick_ask            (ui/companion_panel.py:352)
  → QuickAskRunner.ask()                       (ui/process_launcher.py:136)
       session_id = SessionManager.get_native_id(agent, workspace)   # 若有则 resume
       build_claude_args()/build_codex_args()  (ui/quick_chat_protocol.py)
       _wrapped_cli_args(exe, cli_args, interactive=False)           (ui/process_launcher.py:41)
  → QProcess.start()                           # powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -NonInteractive -File run_cli.ps1 <exe> <argv...>
  → run_cli.ps1                                (tools/run_cli.ps1)
       & $Executable @CliArgs                  # PowerShell 执行 .CMD 时隐含再派 cmd.exe
  → claude.CMD / codex.CMD                     (E:\npm-global\node_modules\@anthropic-ai\claude-code\bin\claude.exe | node codex.js)
  → 原生 CLI → 本地代理 127.0.0.1:15721 → DeepSeek V4 Flash
```

### 2.3 14 个审计问题的答案

1. **QuickAskRunner 在哪？** `ui/process_launcher.py:97`。当前 app.py 未接线（CompanionPanel 未导入）。
2. **QProcess 如何启动？** `process_launcher.py:189-216`：`setProgram(powershell.exe)` + `setArguments(list)` + `setWorkingDirectory(workspace)` + `SeparateChannels`，`readyReadStandardOutput/Error` 接 `_read_stdout/_read_stderr`，异步 `start()`，不用 shell。
3. **为什么经过 powershell.exe？** 因为 `find_executable("claude"/"codex")` 在 Windows 解析到 **`.CMD` shim**（实测 `shutil.which` → `E:\npm-global\claude.CMD` / `codex.CMD`）。QProcess/CreateProcess 不能直接跑批处理文件，必须经 cmd/powershell。wrapper 同时避免把 CLI 子命令/flag 误绑定为脚本参数。
4. **run_cli.ps1 的职责？** `tools/run_cli.ps1`：读 `$args[0]` 为可执行文件，`$args[1..]` 原样 splat 到 `& $Executable @CliArgs`，`exit $LASTEXITCODE`。刻意不用 `param()`，保证 argv 透传不被参数绑定吞掉。
5. **Claude CLI argv？** `quick_chat_protocol.py:102`：`-p --permission-mode plan --effort <low|medium|high> --output-format stream-json --verbose --include-partial-messages [--resume <sid>] [--no-session-persistence] <prompt>`。全部与 `claude --help` 一致（2.1.233）。
6. **Codex CLI argv？** `quick_chat_protocol.py:68`：`exec --sandbox read-only --json -c model_reasoning_effort=<effort> [--skip-git-repo-check] [resume <thread_id> <prompt> | --ephemeral <prompt>]`。与 `codex exec --help` 一致。
7. **session/thread ID 如何保存？** `core/session_manager.py`（内存态，key=(agent, workspace)，Claude=session / Codex=thread）。`_save_session()` 从 CLI 输出流解析 `session_id`/`thread_id` 后写入。磁盘持久化被刻意延后（内存注记）；legacy `SessionRegistry` 仍存在但未被当前 runner 使用。
8. **resume 如何发生？** 每轮都新建进程，仅当 `SessionManager.get_native_id` 有值且 persistent 时，给新进程加 `--resume <sid>` / `exec resume <thread_id>`。**只有 provider 端 session 被续接，进程/网络/TLS/node 运行时全部重建。**
9. **streaming 由哪层支持？** 由 CLI/网络层。Claude `stream-json --include-partial-messages` 发 `content_block_delta(text_delta)`，QProcess 读行 → `parse_claude_event` → `partial` 信号 → UI 实时追加。Codex `--json` 只给结构化事件，正文仅在 `item.completed(agent_message)` 一次性出现（**非逐 token 流式**）。
10. **UI 首次能看到什么 event？** Claude：`system` 事件（状态“Claude 已连接，正在思考…”），首 token 之前只有状态、没有文字。Codex：`thread.started` → `turn.started` → `item.started`…，正文到 `item.completed` 才出现。
11. **每轮重建什么？** 新 QProcess、新 powershell、新 cmd、新原生 CLI 进程、全新 CLI session 初始化（含读 settings / CLAUDE.md / 初始化 hooks）、stdout/stderr 缓冲与 runner 内部态全部重置。
12. **实际复用什么？** `QuickAskRunner` 对象本身、`SessionManager`（内存里的 session id）、provider 端会话历史/cache（经 --resume）、以及用户既有登录态（代理 env）。不复用：任何进程/内存/网络连接。
13. **Cancel 如何实现？** `stop()`（`process_launcher.py:313`）：Windows 上 `QProcess.startDetached("taskkill.exe", ["/PID", pid, "/T", "/F"])` 硬杀进程树；非 Windows `terminate()` + 1.5s `kill()`。没有 CLI 级优雅取消。
14. **taskkill / process tree 行为？** `/T` 杀 powershell 为根的整棵子树（cmd → claude.exe），`/F` 强杀。provider 端会话可能留下“被中断”状态；Claude session 通常仍可 `--resume`。

---

## 3. Latency Decomposition

本轮把一次请求拆为 T0–T6（基准 prompt `Reply exactly: OK`，经本地代理，模型 deepseek-v4-flash）：

| 段 | 含义 | 实测锚点（Claude 冷，中位数） |
|---|---|---|
| T0 | UI/QProcess dispatch（CreateProcess） | `spawn_ms` ≈ **6ms** |
| T1 | PowerShell 进程启动 | 本地 A 基准 ≈ **132ms** |
| T2 | CLI 进程启动（claude.exe boot / node boot） | 本地 B/D 基准 ≈ **155–320ms** |
| T3 | CLI/session 初始化（settings/hooks/context） | 并入 first event |
| T0+T1+T2+T3 | **first structured event** | **851ms** |
| T4+T5 | 网络 + 模型首 token | **5415 − 851 ≈ 4564ms** |
| T0…T6 | total completion | **6565ms** |

CLI 自报指标（`result` 事件）交叉印证：

- 冷启动：`time_to_request_ms ≈ 241–244`、`ttft_stream_ms ≈ 485–534`、`ttft_ms ≈ 4273–4338`、`duration_ms ≈ 4589–4671`。
- resume：`time_to_request_ms ≈ 266–270`、`ttft_stream_ms ≈ 641–712`、`ttft_ms ≈ 1924–1981`、`duration_ms ≈ 2202–2253`。

**结论：本地所有工作（进程+初始化）< 1s；模型/网络阶段是 4–5s+，且随提问复杂度放大。** 不要混淆“命令启动完成”与“模型首 token”。

---

## 4. Local Process Benchmarks

`tools/benchmark_phase8c_backend.py --local`，每类 5 次，`time.perf_counter()`，min/median/max（完整数据在 `docs/phase8c_benchmark_results.json`）。

| 命令 | median total (ms) | 说明 |
|---|---|---|
| powershell -Command "exit 0" | **131.6** | T1 纯启动/退出 |
| claude.exe --version（直连原生 exe） | **185.4** | T2 claude boot 下界 |
| claude.exe --help（直连） | **317.4** | |
| claude.CMD --version（经 run_cli.ps1） | **341.0** | 生产链路 = powershell+cmd+claude |
| claude.CMD --help（经 run_cli.ps1） | **466.3** | |
| claude.exe --version（经 run_cli.ps1，不经 .CMD） | **339.7** | 去掉 cmd 层 |
| codex.CMD --version（经 run_cli.ps1） | **320.1** | T2 codex (node) boot |
| codex.CMD --help（经 run_cli.ps1） | **315.3** | |

**Wrapper 分解**（中位数）：

- powershell 启动基线：**132ms**
- run_cli.ps1 脚本/拼装开销：≈ 340 − 132 − 185 ≈ **23ms**
- cmd.exe 层（.CMD → claude.exe）：341 − 340 ≈ **1ms**（可忽略）
- 相对直连原生 exe 的**总包装开销：≈ 130–290ms**（见第 7 节在线对比）

`spawn_ms`（CreateProcess 耗时）：powershell ≈ 5ms；claude.exe（320MB 原生二进制）≈ 32ms；wrapper 路径 ≈ 5ms。

**这些测试零模型 token。**

---

## 5. Claude Cold Benchmark

方式：生产 wrapper 全链路（powershell → run_cli.ps1 → claude.CMD → cmd.exe → claude.exe），prompt `Reply exactly: OK`，effort low，`--permission-mode plan`，cwd=E:\Firefly_AI_Pet。2 次。

| run | first event | first text | total | exit | session 前缀 |
|---|---|---|---|---|---|
| cold#1 | 842.8ms | 5449.6ms | 6595.1ms | 0 | 3634a673 |
| cold#2 | 859.1ms | 5379.7ms | 6534.3ms | 0 | 03e43a1c |
| **median** | **851.0ms** | **5414.6ms** | **6564.7ms** | 0 | — |

CLI 自报：`ttft_ms≈4338/4273`，`ttft_stream_ms≈485/534`，`time_to_request_ms≈241/244`，`duration_ms≈4671/4589`。

---

## 6. Claude Resume Benchmark

同一 native session，新进程 `--resume <sid>`，相同 prompt。2 次。

| run | first event | first text | total | exit | session 前缀 |
|---|---|---|---|---|---|
| resume#1 | 906.2ms | 3132.2ms | 4208.4ms | 0 | 3634a673 |
| resume#2 | 939.1ms | 3136.8ms | 4216.6ms | 0 | 3634a673 |
| **median** | **922.7ms** | **3134.5ms** | **4212.5ms** | 0 | — |

CLI 自报：`ttft_ms≈1981/1924`，`ttft_stream_ms≈712/641`，`time_to_request_ms≈266/270`，`duration_ms≈2253/2202`。

**resume 到底省了什么？** 只省模型阶段（ttft 4338→1952ms，典型 prompt-cache 命中）：total 省 ~2.35s（~35%）。**不省进程启动**：first event 851→923ms（略增，噪声内）。→ 直接证据：**session reuse ≠ process reuse**。

---

## 7. PowerShell Wrapper Impact

对同一个 Claude 极短请求，直连原生 `claude.exe`（不经 powershell/cmd）对比 wrapper 全链路：

| 指标 | 直连 claude.exe | wrapper 全链路 | 差值 |
|---|---|---|---|
| first event | 745.5ms | 851.0ms | +106ms |
| first text | 5143.4ms | 5414.6ms | +271ms |
| total | 6278.8ms | 6564.7ms | **+286ms** |

**结论：wrapper 相对模型时间几乎可忽略**（+130–290ms ≈ 总耗时 2–5%；若 total 100s 则占 0.2%）。wrapper 提供安全的 .CMD argv 透传，**不要为了优化先删它**。若将来想省这 150–290ms，可以改为把 `find_executable` 解析到原生 exe 直连——但那属于优化，不改变架构结论。

---

## 8. Codex Local Capability Audit

- `codex --version` → **codex-cli 0.147.0**；`codex --help` 确认子命令：`exec`、`resume`、`review`、`mcp`、`mcp-server`（stdio）、**`app-server` [experimental]**、**`exec-server` [EXPERIMENTAL]**、`remote-control` [experimental]、`app`、`doctor`、`sandbox`、`debug`、`apply`、`cloud`。
- `codex exec --help` 确认：`--json`、`--sandbox read-only`、`-c key=value`、`--ephemeral`、`--skip-git-repo-check`、子命令 `resume <id>`。与 Quick Ask 使用的 `build_codex_args` 一致。
- 能力对照：

| 能力 | 本地证据 |
|---|---|
| app-server | ✅ `codex app-server --help`（daemon / proxy / generate-ts / generate-json-schema；`--listen stdio://|unix://|ws://IP:PORT|off`） |
| exec | ✅ `codex exec --help` |
| resume | ✅ `codex exec resume` / `codex resume` |
| JSON / JSONL | ✅ `--json`（exec） |
| streaming | ✅ app-server 协议事件 `item/agentMessage/delta`（见第 9 节） |
| session/thread | ✅ app-server `thread.*` 方法 + `thread_id` 概念 |
| cancel | ✅ app-server `TurnInterrupt`（exec 非交互轮次无优雅取消，仍 taskkill） |
| 长驻 | ✅ app-server daemon（bootstrap/start/restart/stop/version） |

`quick_chat_protocol.py` / `process_launcher.py` 与以上能力对应，无冲突。

---

## 9. Codex App-Server Assessment

**app-server 存在且是真实长驻协议。** 本轮用完全本地的 `codex app-server generate-json-schema --out <tmp> --experimental` 生成了协议 schema（约 700KB，JSON-RPC），拿到了**直接接口证据**：

- **协议**：JSON-RPC（`JSONRPCRequest/Response/Notification`，LSP 风格，客户端发 `initialized`）。
- **传输**：`--listen stdio://`（默认）/ `unix://` / `ws://IP:PORT` / `off`；ws 鉴权 `--ws-auth capability-token | signed-bearer-token`。
- **长驻**：`codex app-server daemon start/restart/stop/bootstrap/version`；`remote-control pair/start/stop`（配对码）。
- **session/thread API**：`ThreadCreate/Start/Resume/Read/List/Archive/Delete/Fork/Rollback/Search/InjectItems/ThreadTurnsList/ThreadSetName/ThreadSettingsUpdate` 等。
- **streaming/event API**：服务器通知 `thread/started`、`turn/started/completed`、**`item/agentMessage/delta`（逐 token 流式）**、`item/plan/delta`、`item/completed`、`command/exec/outputDelta`、`process/outputDelta`、`error`。
- **cancel API**：`TurnInterrupt`（明确存在）。
- **approval / sandbox 模型**：服务器→客户端请求：`CommandExecutionRequestApproval`、`PermissionsRequestApproval`、`FileChangeRequestApproval`、`ExecCommandApproval`、`ApplyPatchApproval`、`ToolRequestUserInput`、`McpServerElicitationRequest`。**客户端必须回包决定 → 谁当客户端谁就是 approval authority。**
- **stability**：官方标记 `[experimental]`；协议存在 v1/v2 两套 schema。

**未实测/未知**：真实延迟、daemon 稳定性、Windows 可靠性、approval 回包在无人值守下的超时行为 —— 需受控 prototype。**本轮未启动 daemon、未发任何模型 prompt**（机器上已有一个预先存在的 OpenAI 桌面版 app-server 进程，pid 2992，非本轮启动，保持原样）。严格按任务：只说“值得做受控 prototype”，不声称“一定适合”。

---

## 10. Claude Backend Options

| 候选 | 本地证据 | 评价 |
|---|---|---|
| A. 每轮 cold CLI 进程 | ✅ 现状，实测 total ≈ 6.3–6.6s（trivial） | 基线 |
| B. CLI + `--resume`，仍每轮进程 | ✅ 实测 total ≈ 4.2s，省 ~35%（纯模型/缓存侧） | **当前 Claude 最现实路线** |
| C. 长驻 CLI 进程 | ❌ `claude --help`（2.1.233）无 app-server/server 交互接口；`--bg` 是后台任务管理（`claude agents`），非低延迟聊天原语 | 当前版本不可行 |
| D. Claude Agent SDK | ❌ 环境未安装（venv/conda/npm 均无 `claude-agent-sdk`），无本地接口证据 | 标记 **Needs separate official-doc verification** |

---

## 11. Native Agent Surface Option

认真评估“Firefly 不做聊天宿主，只做 Launch + Monitor + Notify + Focus + Session 快捷”：

- 完整长对话仍在 Claude Code / Codex 原生界面完成；Firefly 只保留 **Short Ask / Command entry**（甚至暂不保留 Quick Chat）。
- **优点**：最可靠；permission UI 原生；streaming 原生；session 原生；不重复造 terminal/chat UI；符合现有 hook+PermissionCard observer 架构。
- **缺点**：角色沉浸感下降；用户要在窗口间切换；Firefly “Talk”能力受限；无法在宠物里看到逐 token 文本（除非再引流）。

**结论**：不能因为写过 Quick Ask 就默认保留它。**推荐：长对话走原生界面（G 为默认主路径），Short Ask 作为可选轻量入口保留，但定位为“顺手小问”，不为它把 Firefly 做成完整聊天宿主。**

---

## 12. Authentication Comparison

目标：复用用户现有 Claude/Codex CLI 登录/代理配置，Firefly 不存任何 API Key。

| 方案 | 复用现有登录 | 独立 credentials | Firefly 管 secret |
|---|---|---|---|
| CLI（-p / exec） | ✅ 复用 CLI 登录态（实测走 `~/.claude/settings.json` env → 本地代理 127.0.0.1:15721，`ANTHROPIC_AUTH_TOKEN=PROXY_MANAGED`；Codex 复用 `~/.codex` 登录） | 否 | 否 |
| Codex app-server | ✅ 同 CLI 的 `~/.codex` auth（schema 含 `LoginAccount`/`ChatgptAuthTokensRefresh`/`GetAccount`；`remote-control` 用配对码） | 可能（配对/ws-token） | 可能（Firefly 若管 daemon 需持有/传递 token 文件）→ **部分 Unknown** |
| Claude Agent SDK | 依赖 SDK 的 host auth / apiKeyHelper 机制 | 未知 | 未知（未安装）→ **Unknown** |

**结论**：CLI 路线完美复用登录态、Firefly 零 secret。app-server 路线在鉴权上基本复用但引入 token 文件/配对管理，属额外责任面。未读取/未输出任何真实 API Key。

---

## 13. Permission Comparison

| 方案 | permission 出现在哪 | 谁决定 | Firefly 角色 |
|---|---|---|---|
| CLI（现状，`--permission-mode plan` / `--sandbox read-only`） | 原生 CLI 界面 | 用户 | **observer-only**：PermissionCard 只 detect/notify/View ✅ |
| Native Agent Surface | 原生 Agent 界面 | 用户 | observer-only ✅（维持现状） |
| Codex app-server | 服务器把 approval **发给客户端**（`CommandExecutionRequestApproval` 等） | **客户端**（若 Firefly 是客户端 → Firefly 必须回包） | **会变成 approval authority** ⚠️ 明确成本/风险 |
| Claude Agent SDK | 取决于 SDK 事件（本地未装，未证实） | Unknown | Unknown |

**Permission 是核心约束。** 推荐：Firefly 始终 observer-only，不实现任何 approval engine。任何要求 Firefly 回包审批的方案（app-server 客户端模式）都要额外评估。

---

## 14. Backend Decision Matrix

评分 1（差）– 5（优），有理由。在线 App-server/SDK 因未实测的部分标“?”，计分按已证实能力 + 明确风险折中。

| 维度 | A. per-turn CLI | B. CLI+resume | C. 长驻 CLI | D. Codex app-server | E. Claude SDK | G. Native Surface |
|---|---|---|---|---|---|---|
| Latency | 2（6.5s） | 3（4.2s，trivial） | ? | 2（未实测；理论省本地 ~1s，模型仍主导） | ? | 4（原生自带复用） |
| Streaming | 2（Claude 有；Codex 无逐 token） | 2 | ? | 4（`item/agentMessage/delta`） | ? | 5 |
| Session continuity | 2 | 4（resume+缓存） | ? | 5（thread/turn） | ? | 5 |
| Cancel | 2（taskkill 硬杀） | 2 | ? | 4（`TurnInterrupt`） | ? | 5 |
| Permissions | 4（原生/plan） | 4 | ? | 2（客户端变 approval authority） | ? | 5 |
| Auth reuse | 5 | 5 | ? | 3（配对/token 文件） | ? | 5 |
| Implementation complexity | 5（已有） | 5 | ? | 2（JSON-RPC 客户端+daemon 管理） | ? | 5 |
| Windows reliability | 4 | 4 | ? | 1（experimental，未证） | ? | 5 |
| Crash recovery | 3（进程死→重建，session 残留） | 3 | ? | 2（daemon 状态需管理） | ? | 4 |
| Provider coupling | 2（每 agent 独立 argv/parse） | 2 | ? | 2 | ? | 1（宿主化） |
| Token/cost | 3 | 4（缓存省 token） | ? | 4（长驻省重复 init/上下文） | ? | 5 |
| Security | 4（无新面） | 4 | ? | 2（本地 daemon+ws token 新面） | ? | 5 |
| Future Router 兼容 | 3 | 4 | ? | 4（已是结构化协议） | ? | 3 |

**加权结论**：G（原生面）在可靠性/权限/流式全面领先；**B（CLI+resume）是当前 Claude 唯一低风险提速**；**D（app-server）是唯一真长驻候选但成本/风险最高，留 prototype**。

---

## 15. Recommended Architecture

```
Firefly（observer-only）
 ├─ 状态/权限：hooks → state_broker → PermissionCard(只 View)          [现状，保留]
 ├─ SessionManager：唯一 session owner（agent+workspace → native id）  [现状，保留]
 ├─ Short Ask（可选轻量入口）：
 │     Claude → 原生 claude.exe 直连 + --resume（可省 wrapper 层）
 │     Codex  → 8C 先沿用 exec resume；app-server 走受控 prototype
 └─ 长对话 → 原生 Agent 界面（Claude Code / Codex），Firefly 只 Launch+Monitor
```

理由（由实测推导）：

1. 本地/进程不是延迟主因 → 不值得为进程层做复杂重写。
2. `--resume` 是已证实、低风险、还省钱（缓存）的唯一杠杆 → 立即采用。
3. Claude 无长驻接口 → 不硬造。
4. Codex app-server 有真接口证据但 experimental+审批责任+token 约束 → prototype 化，不默认接入。
5. 权限必须原生 → 任何把 Firefly 变 approval authority 的方案都降级。

---

## 16. Provider Adapter Contract（只设计，不实现）

统一 backend 时的目标接口（本轮**不写代码**）：

```python
# 概念契约，非生产代码
class AgentProvider:
    def capabilities(self) -> ProviderCapabilities: ...      # streaming / resume / cancel / approval
    def start(self, context: ProviderContext) -> SessionRef: ...
    def send(self, session, prompt, policy) -> Iterator[AgentEvent]: ...
    def resume(self, session) -> SessionRef: ...
    def cancel(self, turn_id) -> None: ...
    def close(self, session) -> None: ...

# AgentEvent: status / text_delta / tool / permission / final / error
```

- `SessionManager` 继续持有 `SessionRef`。
- **QWidget 不解析 provider JSON**：所有 provider 差异收敛在 adapter 层。
- 本轮只在此文档固化接口形状，实施在后续切片。

---

## 17. Phase 8C Implementation Plan（切片，均可独立回滚）

| 切片 | 内容 | 依据 |
|---|---|---|
| **8C.1** | 把 Short Ask 轻量入口接回 VisualShell（当前 dormant），backend = Claude 直连原生 exe + `--resume`（SessionManager 不变）；保留 wrapper 兼容开关。给 `QuickAskRunner` 加 T0–T6 阶段计时日志（每轮记录 first event / first text / total）。 | 实测：B 是唯一低风险提速；需要真实使用场景延迟。 |
| **8C.2** | 统一 `AgentEvent` adapter（按第 16 节契约），Claude/Codex 各自实现，UI 只消费 AgentEvent。 | 消除每 agent 各自 argv/parse 的耦合。 |
| **8C.3** | Short Talk UX 打磨（首 token 尽快可见、连接状态、resume 提示）。 | streaming 体验是用户感知的主要延迟。 |
| **8C.4** | Session resume / cancel 硬化：Claude `--resume` 断线重试；Codex 若 app-server prototype 通过则 `TurnInterrupt`。 | 现状 cancel 是 taskkill 硬杀。 |
| 8C.5（可选/门控） | Codex app-server 受控 prototype：仅本地 daemon + 不发模型 prompt，验证延迟/稳定性/审批回包语义。 | 第 9 节证据；experimental+审批责任需专项验证。 |

每步可独立回滚；**本阶段只做计划，不开始实现。**

---

## 18. Explicit Non-Goals

- ❌ 不做 Firefly 全量聊天宿主/terminal/chat UI 重造。
- ❌ 本轮不实现任何 Provider adapter / AgentProvider 代码。
- ❌ 不删除 PowerShell wrapper 作为“优化”（实测非瓶颈；仅作 8C.1 可选直连开关）。
- ❌ 不把 Firefly 变成 approval authority；不实现 approval engine。
- ❌ 不在未授权情况下发起 Codex 在线模型请求。
- ❌ 不安装任何 package / SDK / MCP / plugin；不改 DeepSeek 配置。
- ❌ 不为“统计稳定性”连续烧十几次在线调用。

---

## 19. Risks

1. **模型延迟主导**：任何 backend 无法消除单请求的模型/代理时间（实测 4.5s+ 且随复杂度放大）。不要把 backend 决策当延迟解药。
2. **resume 依赖缓存命中**：真实工作区上下文切换/上下文被改写时，缓存收益可能衰减；需在 8C.1 真实使用中复核。
3. **app-server 是 experimental**：协议 v1/v2 并存、daemon 稳定性/Windows 可靠性未证；接入前必须受控 prototype。
4. **审批责任转移**：若 Firefly 成为 app-server 客户端，将被迫回包审批 → 违反 observer-only 原则。
5. **hooks 在 -p 模式确实触发**（本轮实测推翻了 Phase 7B“可能不触发”的旧观察）：Quick Ask 每轮会写 `runtime/sources/claude.json`，让 pet 动画；重新启用 Quick Ask 时要注意与真实 agent 状态争夺同一文件。
6. **token 预算**：Codex 在线测试被显式禁止；任何 Codex prototype 必须先获授权。

---

## 20. Open Questions

- Claude 是否有/将来有长驻交互 server？（2.1.233 无；`--bg`/`agents` 是否为可用原语需官方文档。）
- Claude Agent SDK 的 Windows 支持、登录复用、事件模型？（本地无包，`Needs separate official-doc verification`。）
- Codex app-server 在 Windows 上 daemon 启动/退出的真实稳定性与首事件延迟？（需 8C.5 prototype。）
- app-server approval 回包超时/无客户端时行为？
- 真实工作区 + 真实提问下，resume 的缓存命中率与 total 分布？

---

## 21. Raw Benchmark Summary

见 `docs/phase8c_benchmark_results.json`（无 prompt 转录；native session id 只存 8 字符前缀；无 API key）。关键数字：

- 本地：powershell 启动 **131.6ms**；claude.exe boot **185.4ms**；codex(node) boot **~320ms**；wrapper+cmd 总包装开销 **~130–290ms**。
- Claude cold（wrapper，median）：first event **851ms** / first text **5415ms** / total **6565ms**。
- Claude direct（无 wrapper，1 次）：745ms / 5143ms / 6279ms。
- Claude resume（wrapper，median）：923ms / 3135ms / **4213ms**（省 ~35%，全在模型阶段）。
- CLI 自报：cold `ttft_ms≈4338`、`ttft_stream_ms≈485`、`time_to_request_ms≈244`；resume `ttft_ms≈1952`。
- **本轮 Claude 在线调用：6 次**（1 次脚本 bug 浪费 + 2 cold + 1 direct + 2 resume），全部为 `Reply exactly: OK` 极小 prompt。
- **Codex 在线调用：0 次。**
- hooks 在 `-p` 模式确实触发（旧观察已修正）；runtime 状态已恢复至用户真实状态（waiting），生产状态未被扰动。

---

## 22. Phase 8C.1 Outcome（实现 + 实测）

> 日期：2026-08-15。Short Ask 轻量入口 + T0–T6 telemetry + Hook isolation。Claude 2.1.233 · codex-cli 0.147.0。
> 本文档上一节（第 1–21 节）只做测量与决策，未改动生产代码；**本节记录实际实现。**

### 22.1 Hook collision 根因（确认）

全局 `~/.claude/settings.json` 的 `hooks` 在每个 lifecycle event（SessionStart / UserPromptSubmit / PreToolUse / PermissionRequest / PostToolUseFailure / StopFailure / Stop / SessionEnd）调用
`tools/simulate_event.py <state> --source hook --agent claude`，把 `runtime/sources/claude.json` 写成对应状态。
Claude Code 2.1.233 在 `-p` 模式也会触发这些 hooks（Phase 8C Preflight 已推翻旧观察）。因此 Firefly 内部 Quick Ask 与用户真实 Claude session 会并发覆盖同一个 source 文件。

### 22.2 本机 Claude 是否提供 per-invocation hook suppression（审计结论）

**是，`--safe-mode`。** 依据全部来自本机安装的 2.1.233，未凭记忆假设：

| 事实 | 证据 |
|---|---|
| `--safe-mode` 文档化 "hooks … disabled" | `claude --help`（2.1.233 原生 binary 自报） |
| `--safe-mode` 文档化 "Auth, model selection, built-in tools, and permissions work normally" | 同上 |
| `--safe-mode` 设置 `CLAUDE_CODE_SAFE_MODE=1` | 同上 + 二进制字符串搜索命中 `CLAUDE_CODE_SAFE_MODE` / `CLAUDE_CODE_SIMPLE` / `settingSources` |
| `--safe-mode` 下登录态完好 | `claude --safe-mode auth status` → `{loggedIn: true, authMethod: oauth_token, apiProvider: firstParty}` |

备选（未采用，记录在案）：`--setting-sources project,local` 可排除 user source（hooks 所在），但需 Firefly 自己回填 settings.json `env`（代理配置）且丢失 `"model": "haiku"` 等 user 设置；比 `--safe-mode` 多活动部件。`--bare` 把 auth 限制为 `ANTHROPIC_API_KEY`/apiKeyHelper，与本机代理 env 方案不匹配，风险更高。

### 22.3 最终 isolation 方案

**Firefly 内部 `claude -p` 一律追加 `--safe-mode`（per-invocation）**：

- 该 invocation 不触发任何 global lifecycle hook → 不写 `runtime/sources/claude.json` → 不污染 external lifecycle。
- 用户正常启动的 Claude Code（不带该 flag）继续正常触发 hooks。
- 零修改 `~/.claude/settings.json`（只读复用其 `env` block）；无时间窗口 hack；无 PID 猜测；无竞态 —— 身份契约就是 flag 本身。
- 由于 `--safe-mode` 是否应用 settings.json `env` 在静态上无法 100% 断言，launcher 额外把 `~/.claude/settings.json` 的 `env` block 注入子进程环境（`QProcessEnvironment`），保证一定走本地代理（127.0.0.1:15721）。env 值为配置（`PROXY_MANAGED` sentinel + localhost + model 映射），非真实 secret。

实现点：`build_claude_args(..., isolated=True)` → 追加 `--safe-mode`；`QuickAskRunner.ask(..., isolated=True)` → 注入 env + 记录里程碑。

### 22.4 新增 / 修改文件

| 文件 | 变更 |
|---|---|
| `core/quick_ask_metrics.py` | **新增**。Qt-free T0–T6 telemetry + 派生 timing + allowlist 校验 + `runtime/metrics/` latest + 20-entry ring buffer。 |
| `ui/quick_chat_protocol.py` | `build_claude_args` 增 `isolated` 参数（追加 `--safe-mode`）；新增 `classify_short_ask`（简单/复杂 prompt 分类）。 |
| `ui/process_launcher.py` | `QuickAskRunner`：`isolated` 透传、代理 env 注入、`telemetry` signal + T0–T6 里程碑、`_claude_settings_env()`、`_model_active_event()`。 |
| `ui/short_ask.py` | **新增**。`ShortAskPanel`（单行输入/短回答/Stop/Open agent/Esc 取消）与 `AskPill`（挂在 greeting bubble 角上的独立点击入口）。 |
| `ui/overlay_coordinator.py` | 接入 ask_pill + short_ask：定位、显隐、优先级（PermissionCard / business popover 高于 Short Ask）、outside-click dismiss、close。 |
| `app.py` | 构造 runner/metrics/pill/panel，连接信号，Short Ask 编排（agent 可用性 / complex steering / telemetry 落盘）。 |
| `tools/test_phase8c1_short_ask.py` | **新增**。15 项验收测试。 |
| `tools/smoke_phase8c1_short_ask.py` | **新增**。在线 smoke（恰好 2 次 Claude 极短调用，0 次 Codex）。 |
| `docs/PHASE8C_BACKEND_DECISION.md` | 本第 22 节。 |

未改动：`state_broker.py`、StateMonitor 外部 lifecycle 语义、Claude/Codex hooks、`~/.claude`、`~/.codex`、`run_cli.ps1`、`quick_chat_protocol.py` 的协议解析主体、start/stop 脚本、requirements.txt。

### 22.5 Short Ask UI

- 入口：greeting bubble 右上角一个小玻璃 `Ask…` pill（独立顶层窗口；bubble 本身保持 input-transparent，不挡宠物拖拽）。
- 面板：紧凑玻璃 popover。header 显示当前 Agent（AgentDock 选中项）+ 状态；单行 QLineEdit（Enter 发送 / Esc 取消）；短回答 QLabel（最多 ~600 字符 + "…"）；Stop / Open Claude 上下文按钮。
- Agent 可用性：claude → 真实 Short Ask（isolated）；codex → "Codex Short Ask 将在后续阶段开放。" + Open Codex（**不发起在线调用**）；chatgpt → "ChatGPT direct talk is not available yet." + Open ChatGPT（不伪造 backend）。
- 复杂 prompt（`classify_short_ask`，含 fix/edit/rewrite/run/install 等关键词）→ 推荐 "Open Claude"，不自动发送（提供 "Ask anyway" 保留自主权）。
- 长回答截断显示，完整结果保留在内存；权限类失败/超时 → 建议打开原生 Agent。内部状态只驱动 Short Ask 面板，**绝不**改写 AgentDock/PermissionCard/Notification。

### 22.6 Backend / wrapper / session

- **Backend 仍是 PowerShell wrapper 全链路**（`powershell.exe → run_cli.ps1 → claude.CMD → cmd.exe → claude.exe`），未为性能删除 wrapper（实测非瓶颈）。
- **Session 仍归 SessionManager**：首次 ask 新建 native session，后续 `--resume`；跨 workspace 不 resume；native session id 不落盘。
- `--safe-mode` 下 `--resume` 复用同一 native session **已验证**（smoke call 2 复用同一 session 前缀）。

### 22.7 Telemetry 实测（smoke，经本地代理）

| 指标 | call 1（new session） | call 2（resume） |
|---|---|---|
| first_event_ms（spawn→首个结构化事件） | 3962 | 3961 |
| first_text_ms | 5847 | 5666 |
| total_ms | 6434 | 6295 |
| exit | 0 | 0 |
| cancel_requested | false | false |

- 文件：`runtime/metrics/quick_ask_latest.json`（latest）+ `quick_ask_history.json`（ring buffer，最多 20 条）。字段仅 agent / workspace token / session_exists / session_hash(8 位) / isolated / 里程碑 / exit / error_category / derived。**无 prompt、无 response、无 key、无 env、无完整 session id。** `AskTelemetry.validate_safe()` 单元校验通过。
- 观察：`first_event ≈ 3.9s`（Phase 8C 非 safe-mode 基准 ≈ 0.85s），`total ≈ 6.3s`（基准 cold ≈ 6.5s）。`--safe-mode` 可能移动了 first-event（连接/初始化相位），但用户可见 total 与基准相当；resume 未复现基准的 ~35% 缓存加速（safe-mode 精简 context 后缓存命中路径不同）。诊断用途，不作性能声明。

### 22.8 internal ask 是否污染 claude.json / PermissionCard 是否误触发

- **未污染**。smoke 两次 ask 前后 `runtime/sources/claude.json` 字节级 unchanged。且 smoke 期间用户真实外部 Claude session 正在每 ~20s 刷新 claude.json（waiting），两次内部 ask 窗口内外部写入零交错 —— 最强的 isolation 实证。
- **PermissionCard 未误触发**。内部 ask 不产生 `waiting` lifecycle（不写 source），StateMonitor 与 PermissionCard 完全不受影响；单测 `test_internal_ask_no_permission_card` 断言 coordinator `_waiting` 为空、PermissionCard 不可见。

### 22.9 在线调用次数与 regression

- **Claude 在线调用：2 次**（smoke call 1 new session + call 2 resume，prompt 均为 `Reply exactly: OK`，effort low，经本地代理）。
- **Codex 在线调用：0 次。**
- 全部测试（含既有 regression + 新增 test_phase8c1）：**12/12 通过**（test_state_broker / 7a / 7b / 8a1 / state_monitor / 8a3 / 8b1 / 8b2 / 8b3 / single_instance / 8b4 / 8c1）。运行方式：`$env:PYTHONPATH="E:\Firefly_AI_Pet"; python tools\<test>.py`。

### 22.10 已知限制

1. `--safe-mode` 同时禁用 CLAUDE.md / MCP / plugins / output styles —— Short Ask 无项目级 CLAUDE.md 上下文；需要上下文的任务应走原生 Agent（本设计有意为之）。
2. resume 在 safe-mode 下未获得基准的缓存加速（见 22.7），Short Ask 每次 ~6.3s 为常态。
3. `first_event_ms` 在 safe-mode 下偏高（~3.9s）—— 归类为诊断观察，未定位根因。
4. 取消仍是 `taskkill /T /F` 硬杀进程树（Phase 8B.1 现状），无 CLI 级优雅取消。
5. `classify_short_ask` 是粗关键词启发式，可能误判（如含 "fix" 的解释性问题会先推荐 Open Claude，但保留 "Ask anyway"）。
6. ChatGPT/Codex 路径仅提示 + 打开原生，本阶段无 backend（按预算）。

### 22.11 Phase 8C.2 建议

**值得继续。** 8C.1 已证明：hook isolation 可用 `--safe-mode` 安全实现、Short Ask 可从 VisualShell 使用、telemetry 有效、external lifecycle 与 PermissionCard 不被污染。建议 8C.2 做 **AgentEvent adapter 统一（第 16 节契约）**：把 Claude/Codex 各自的 argv/parse 收敛到 adapter，UI 只消费 AgentEvent；顺带把 `--safe-mode` 的 first_event 延迟根因与 resume 缓存路径一并复核。

---

## 23. Phase 8C.2 Outcome（AgentEvent Adapter 统一）

> 日期：2026-08-15。统一 provider protocol → AgentEvent。Claude 2.1.233 · codex-cli 0.147.0。
> 目标：provider-specific JSON 解析收敛到 adapter 层，UI / Short Ask / telemetry 只消费中立 AgentEvent。本阶段未更换 backend。

### 23.1 架构

```
Claude stream-json ─┐                        ┌→ SessionManager（SESSION event → session/thread id）
                     ├→ core/agent_events.py ─┼→ QuickAskRunner（adapter 分发 + 事件转发）
Codex JSONL      ───┘   (中立 AgentEvent)     └→ status/partial/finished/failed + agent_event signal → ShortAsk UI
```

- **`core/agent_events.py`**（新增，Qt-free）：`AgentEventType`（STARTED/SESSION/STATUS/TEXT_DELTA/TOOL/PERMISSION/FINAL/ERROR/CANCELLED）、`ErrorCategory`（process_start/transport/protocol/auth/timeout/cancelled/provider/unknown）、`AgentEvent`（agent_id/type/timestamp + 可选 text/status/session_id/tool_name/error_code/metadata）、`AgentCapabilities`、`AgentEventAdapter` 契约（`feed_line` / `feed_event` / `finalize`）、语义 STATUS token（connecting/thinking/generating/…）与 `WORKING_STATUSES`。**无中文 UI 文案。**
- **`core/agent_adapters.py`**（新增，Qt-free）：`ClaudeStreamAdapter`、`CodexJsonlAdapter`、`make_adapter(agent)` 工厂、`CLAUDE_CAPABILITIES` / `CODEX_CAPABILITIES`。不启动进程、不管理 session/UI、不生成展示语言。
- **Adapter 不持有 session**：SESSION event 由 runner 转发给 `SessionManager.set(agent, workspace, session_id)`（kind 仍由 `AGENT_KINDS` 决定：Claude=session，Codex=thread）。
- **事件语义**：SESSION=本地已识别 native id；TEXT_DELTA=可展示增量文本；STATUS=短生命周期 token（connecting/thinking/generating/organizing/reading/processing/running_tool/reconnecting/completed/error）；TOOL=工具活动（本阶段 Codex 侧）；FINAL=本轮最终文本；ERROR=结构化错误（短 error_code，默认不送 traceback）；CANCELLED=用户取消。

### 23.2 QuickAskRunner 收缩

- 从「process + provider parsing + session 提取 + UI 文案 + telemetry」收缩为「process transport + adapter 分发 + 事件转发」。
- 每轮 `ask()` 通过 `make_adapter(agent)` 选择一次 adapter；`_consume_json_line` 只调用 `adapter.feed_line(line)` → 逐事件处理，**不再出现 `if agent == "claude" / elif "codex"` 的 provider JSON 分支**。
- 兼容 shim：若调用方绕过 `ask()` 直接设 `_agent` 并调 `_consume_json_line`（Phase 7B/8B.1 旧测试），按当前 `_agent` 惰性创建/切换 adapter。
- 新增 `agent_event` Qt signal：转发每个统一 `AgentEvent`（统一事件流，非原始 provider dict）。
- STATUS token → 中文显示文案的映射（`STATUS_DISPLAY`）留在 `ui/process_launcher.py`（UI 层），并做 token 去重（同一状态不重复 emit）。
- stderr 网络重连状态改为 `STATUS("reconnecting")` → 同一显示通道。

### 23.3 Telemetry 事件驱动

- T2 = 首个 AgentEvent（任意）；T3 = 首个 SESSION；T4 = 首个 TOOL/TEXT_DELTA/FINAL 或 working STATUS；T5 = 首个 TEXT_DELTA/FINAL；T0/T1/T6 保持本地 process 语义。
- `quick_ask_latest.json` / `quick_ask_history.json` 安全要求不变：无 prompt、无完整 response、无 API key、无完整 native session id（仅 8 位 hash 前缀）。

### 23.4 Cancel / Error / Compat

- Cancel：用户 Stop → transport kill（taskkill 保持现状）→ `_on_finished` 发出 `AgentEvent(CANCELLED)`。adapter 不做进程管理。
- Error taxonomy：`finalize(exit_code)` 在非零退出且无已报错误时合成 `ERROR(provider)`；malformed JSON 行 → `ERROR(protocol)`（runner 容错，不因此判定整轮失败）。
- 兼容：`parse_claude_event` / `parse_codex_event` / `parse_json_line` / `ParsedEvent` 保留为 **thin wrapper**（调用 adapter → `_parsed_from_events`），Phase 7B/8B.1 测试零改动通过。

### 23.5 受保护范围（未改动）

`state_broker.py`、`core/state_monitor.py` external lifecycle 语义、Claude/Codex Hooks、`~/.claude`、`~/.codex`、permissions/approval/sandbox/trust、动画、WorkspaceManager/SettingsManager schema、start/stop 脚本、requirements.txt、`run_cli.ps1`。未进入 Codex app-server / Claude SDK / persistent backend / Agent Router / multi-agent workflow。

### 23.6 文件清单

| 文件 | 变更 |
|---|---|
| `core/agent_events.py` | **新增**。中立模型 + adapter 契约 + error taxonomy + capabilities。 |
| `core/agent_adapters.py` | **新增**。ClaudeStreamAdapter / CodexJsonlAdapter / make_adapter / capabilities。 |
| `ui/quick_chat_protocol.py` | `parse_claude_event` / `parse_codex_event` 改为 adapter thin wrapper；新增 `_parsed_from_events`；`build_*_args`、`parse_json_line`、`classify_short_ask`、`SessionRegistry` 不变。 |
| `ui/process_launcher.py` | QuickAskRunner：移除 provider 分支解析与 `_model_active_event`；新增 `agent_event` signal、`_adapter`、`_handle_event`、`STATUS_DISPLAY`、T2–T5 事件驱动、CANCELLED 事件、`finalize` 转发、agent 切换兼容 shim。 |
| `tools/test_phase8c2_agent_events.py` | **新增**。24 项验收 + 1000-line parser 性能基准。 |
| `tools/smoke_phase8c2_agent_events.py` | **新增**。1 次 Claude 在线端到端验证（真实 stream → AgentEvent → UI），0 次 Codex。 |
| `docs/PHASE8C_BACKEND_DECISION.md` | 本第 23 节。 |

### 23.7 在线调用与 regression

- **Claude 在线调用：1 次**（smoke：`Reply exactly: OK`，isolated safe-mode）。真实流解析出 `STARTED/SESSION/STATUS/TEXT_DELTA/FINAL` 全事件链；`source_unchanged=True`（hook isolation 保持）；telemetry T5 由首次 TEXT_DELTA 驱动。
- **Codex 在线调用：0 次。**
- 全部测试（13/13 通过）：test_state_broker / 7a / 7b / 8a1 / 8a3 / 8b1 / 8b2 / 8b3 / 8b4 / single_instance / state_monitor / 8c1 / **8c2**。
- 性能：Claude 1000 行 ≈ **5.8ms**，Codex 1000 行 ≈ **5.3ms**（目标远小于 10–50ms）。

### 23.8 已知限制 / 下一步（8C.3）

1. Claude adapter 未加 `tool_use` 事件解析（safe-mode plan 下 read-only 工具不产生交互 tool UX），TOOL 事件当前主要由 Codex 侧体现 —— 保持与既有已测行为一致，未扩面。
2. Cancel 仍是 taskkill 硬杀（无 CLI 级优雅取消），CANCELLED 事件在进程结束回调时发出。
3. 建议 8C.3：Short Talk UX 打磨（首 token 尽快可见、连接状态、resume 提示），并复核 safe-mode 下 first_event 延迟根因与 resume 缓存路径。
