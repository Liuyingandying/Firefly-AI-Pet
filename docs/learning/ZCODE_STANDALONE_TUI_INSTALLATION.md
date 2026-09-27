# Z Code Standalone CLI/TUI Runtime Installation

日期：2026-09-27。结论：**ZCODE_STANDALONE_TUI_READY**。本结论依据本机 Windows Terminal 可见画面、真实键盘输入、进程命令行、Z Code/teach-mcp 数据库及 Firefly 日志；测试通过本身不作为桌面验收。

## 安装与构建

| 项目 | 本机事实 |
| --- | --- |
| 源码 | 官方 `https://github.com/zai-org/ZCode.git`，`v3.14.3`，commit `29628c9acdb81b703bbd4080c207a0e7ce5e276e`，位于 `E:\AI_Workspace\ZCode-Official` |
| 发行包 | `E:\AI_Workspace\ZCode-Official\dist\zcode\releases\3.14.3\zcode-3.14.3.tar.gz`，SHA256 `c054fad037fd989e8f502317a41e3b7280b410f3fda7ba89acdf7f87084cba0f` |
| 独立安装 | `E:\AI_Workspace\ZCode-Standalone\zcode`；官方统一入口 `bin\zcode.mjs`；Windows UTF-8 启动脚本 `bin\zcode.cmd` |
| Node | 官方 `mise.toml` 指定 24.14.0；下载的 Node ZIP 经官方 SHASUMS256 核对，仅用于本构建与安装。`zcode\node.exe` 是同一二进制的本地副本；系统 Node 未改动。 |
| pnpm | 构建使用官方指定 10.33.2；系统 pnpm 未改动。 |
| CLI 版本 | 发行包版本 3.14.3，内部 Agent CLI `--help` 显示 0.16.9；这两个数字分别属于发行包与 Agent CLI，不是 Desktop/CLI 混装证据。 |

官方 Windows `pnpm build:zcode` 脚本在 `spawnSync("pnpm")` 处报 `ENOENT`（Windows 上实际 shim 是 `.cmd`）；依赖安装的 postinstall 还遇到 Electron 下载 `ECONNRESET` 与可选 `cpu-features` 编译失败。未改官方源码：用 `pnpm install --frozen-lockfile --ignore-scripts`，按官方脚本的组件顺序构建 CLI/TUI、server、web，并补跑 shared 的 TypeScript 输出，再执行官方 `node scripts/build-zcode.mjs --skip-build --base-url https://downloads.example.com/zcode/` 组装发行包。占位 URL 只写入本地发行元数据，未发布。真实 TUI gate 证明必需的 Windows TUI 原生库在产物中可用。

`bin\zcode.cmd` 只运行 `chcp 65001 >nul`，然后以安装内的 Node 调用同目录官方 `zcode.mjs` 并原样转发 `%*`。首次直接从 Windows Terminal 运行 `.mjs` 时，窗口按 CP437 显示乱码；换此启动脚本后截图中的中文、题目与框线正常。Desktop 安装目录未修改。

## 独立 Gate

在课程工作区 `C:\Users\FAJ\AppData\Local\FireflyAI\learning\courses\crs-d91e981f5a92\workspace`，以 `node bin\zcode.mjs --resume sess_0d75c470-cacd-465f-99e3-c319f4c81af4` 启动全屏 TUI。PTY 中输入“你好”后得到模型回复。随后在真实 Windows Terminal 中以 UTF-8 脚本启动，屏幕可见 Z Code TUI；输入 `hello` 得到包含 RC 课程、quiz 40% 与 RC-E02 的回复。截图：`E:\AI_Workspace\ZCode-Standalone\terminal_gate_after_enter.png`。这比仅有 `--help`、进程存在或 headless 响应更强。

standalone 未设置 `ZCODE_DATA_BASE_DIR`，默认读取现有 `C:\Users\FAJ\.zcode`。`skills list` 实际列出 `C:\Users\FAJ\.zcode\skills\firefly-learning\SKILL.md`；现有 `cli\config.json` 包含 `teach-mcp`，现有 `cli\db\db.sqlite` 中可查到旧 session。独立 TUI 使用真实 `.firefly\learning_context.json`，调用 `mcp__teach-mcp__learning_status` 和 `mcp__teach-mcp__resume_learning` 均显示 completed，返回学习者 `ff-02adc88d`、会话 `ff-02adc88d-20260927-010222-11f5`、RC 电路充放电、quiz 40%、题目 RC-E02。未复制 skills、MCP 配置或 session DB。

## Firefly 最小接线与真实 E2E

`learning/launcher.py` 的 `find_zcode_runtime()` 优先接受 PATH 上对应官方发行布局的 `zcode.cmd`，其次读取 `FIREFLY_ZCODE_RUNTIME`（本机用户环境变量为 `E:\AI_Workspace\ZCode-Standalone\zcode\bin\zcode.cmd`）；interactive 缺失时报 `ZCODE_TUI_RUNTIME_MISSING`。仅显式 `interactive=False` 的兼容路径允许 Desktop 内部 CLI 做 headless。首次/重建 init 的 `--prompt --cwd --json` 与后续 `--resume` 都使用所选 standalone 入口；TUI 前置检查验证发行包中的 `@zcode/tui` 与 Windows 原生库文件。内置 provider 文件取自同一 standalone `agent\provider`，个人 provider 配置仍取现有 `~/.zcode/v2`。

真实 UI 曾将这门课程显示为“新资料”：binding 只有有效 `zcode_session_id`，而入口状态推导只认 `curriculum_id`。`learning/bridge_state.py` 现将已绑定 Z Code 会话呈现为“继续学习”；审核中的 pipeline 仍优先呈现待审核。没有伪造 curriculum ID，也没有改变 teach-mcp、skill、session reuse 或学习状态所有权。

最终桌面链：关闭原 Firefly、Z Code Desktop 与测试终端，重启 Firefly；从系统托盘实际打开“学习模式”，选择 `RC 电路充放电`，可见“有进行中的学习：可继续 / 继续学习”。点击后，13:06:51 的 `learning_entry.log` 同时记录 `ZCODE_SESSION_REUSE`、`ZCODE_TUI_SPAWN_ATTEMPT` 和 `ZCODE_TUI_PROCESS_STARTED`：cwd 为该课程 workspace，argv 为 `wt.exe -d <workspace> E:\AI_Workspace\ZCode-Standalone\zcode\bin\zcode.cmd --resume sess_0d75c470-cacd-465f-99e3-c319f4c81af4`。Windows 进程树中是 WindowsTerminal → cmd → standalone node → 官方 `bin\zcode.mjs`。截图：`E:\AI_Workspace\ZCode-Standalone\firefly_e2e_tui_recorded.png`。

在该 Firefly 打开的 TUI 中直接输入 `C`，可见 `mcp__teach-mcp__evaluate_answer` 和 `record_result` completed；权威 DB 的最新结果是 `RC-E02, correct=0, recorded_at=2026-09-27T13:08:07+08:00`，quiz progress 由 0.4 到 0.6，随后产生下一题 RC-E01。关闭该终端后，13:10:04 再从 Firefly 点击“继续学习”：日志、进程 argv、binding 与 Z Code DB 均仍是同一 `sess_0d75c470-cacd-465f-99e3-c319f4c81af4`，DB `directory` 精确等于该课程 workspace。第二次打开的 TUI 屏幕显示新题；截图：`E:\AI_Workspace\ZCode-Standalone\firefly_e2e_second_response.png`。第二个 TUI 随后关闭，供最终重启验证。

最后一处 headless 兼容参数调整后，重新启动 Firefly 并再次实际点击同一课程“继续学习”。13:14:59 的最新日志仍为 `ZCODE_SESSION_REUSE` 与 standalone `zcode.cmd --resume`；Node 进程 PID 1760 的命令行是官方 `bin\zcode.mjs --resume sess_0d75…`。屏幕上可见该 Z Code 全屏 TUI，截图：`E:\AI_Workspace\ZCode-Standalone\firefly_final_loaded_tui_exact.png`。最终窗口保持打开，供用户继续输入。

回归：`tests/test_real_tui_spawn.py`、`test_zcode_cli_provider_env.py`、`test_learning_bridge.py`、`test_learning_return_delivery.py` 共 **45 passed**。其中测试模拟不代替上述桌面证据。新课程的 init 命令构造由测试覆盖；本次没有为验收另建一门真实课程和新的 teach-mcp pipeline。

## 指定问题逐项回答

1. **安装位置**：`E:\AI_Workspace\ZCode-Standalone\zcode`。
2. **官方版本/commit**：发行版 v3.14.3，`29628c9acdb81b703bbd4080c207a0e7ce5e276e`。
3. **`@zcode/tui`**：存在于 `agent\node_modules\@zcode\tui\dist\index.js`，约 2.19 MB；Windows OpenTUI 原生包也存在。
4. **独立 TUI 肉眼可交互**：是，真实 Windows Terminal 截图与 `hello` 回复已验证；UTF-8 脚本解决了直接启动的 CP437 乱码。
5. **现有 `~/.zcode`**：直接复用同一目录、配置、skill 与 DB，无第二套状态。
6. **`firefly-learning`**：standalone `skills list` 可发现，真实 TUI 按该 skill 读取课程 context 并继续教学。
7. **`teach-mcp`**：真实 TUI 的 `learning_status`、`resume_learning`、`evaluate_answer`、`record_result` 均完成；DB 证实结果。
8. **旧 session ID**：兼容，旧 Desktop-internal CLI 建立的 `sess_0d75…` 被 standalone 精确 `--resume`，两次 Firefly 打开均未换 ID。
9. **Firefly 最终 executable**：Windows Terminal 执行本机 UTF-8 `zcode.cmd`，脚本使用同安装目录的 Node 24.14.0 运行官方 `bin\zcode.mjs`。Desktop `ZCode.exe` 不在 Agent 链中。
10. **Firefly 正确课程 TUI**：是，真实点击、画面、日志、进程 argv、Z Code DB `directory`、teach-mcp RC 会话及直接答题一致。

**最终状态：ZCODE_STANDALONE_TUI_READY**。


