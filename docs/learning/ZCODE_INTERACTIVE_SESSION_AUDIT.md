# Z Code Interactive Session Audit（0.16.9 实测）

日期：2026-09-27
方法：CLI `--help` 实跑 + `zcode.cjs`（14.8MB bundle）字符串级源码搜索 + `app.asar`（326MB，Electron 主进程）二进制字符串搜索 + 真实命令复现。全部结论有实测依据，无猜测。

---

## 1. 桌面 UI「新建任务」内部调用什么？

桌面端为 Electron 应用（`resources/app.asar`）。主进程含：

- `requestSingleInstanceLock(gb(process.argv))` + `second-instance` 事件（3 处）：二次启动 `ZCode.exe` 时 argv 被转发给既有实例；
- `open-url`（3 处）+ `setAsDefaultProtocolClient`（2 处）+ deep-link 子系统（47 处，含 Linux AppImage 图标、workspace 打开确认文案、`zcode://oauth/callback`）；
- **workspace deep link**：存在"打开某工作区目录"的请求处理（`canOpenWorkspace` / `onWorkspaceOpen` / `cl(d, ...)` 打开路径）。

**未发现**"创建 task 并注入 initial prompt"的公开入口（无 CLI 参数、无可确认的 deep-link 参数格式带 prompt）。

## 2. 能否通过 CLI/URI 创建新 task？

**CLI 可以**：`zcode -p <prompt>` 每次执行都会创建一个持久化的新 session（实测连续多次 `-p` 各产生独立 `sess_*`，会话文件与 db 均持久）。URI 方式：仅确认 `zcode://oauth/callback`（OAuth 用），未确认通用 task 创建 scheme。

## 3. 能否带 initial prompt？

- CLI：**能**（`-p/--prompt` 即 initial prompt，headless 执行一轮）。
- 桌面 GUI：未发现注入口。
- TUI：无 initial prompt 参数（`-p` 明确是"without opening the TUI"）。

## 4. 能否指定 cwd/workspace？

**能**：`--cwd <path>`（CLI 全模式）；deep-link 的 workspace 打开面向目录，但参数格式属内部实现、无稳定文档化格式。

## 5. 能否随后由桌面 UI 打开该 task？

未发现稳定的"按 session 打开"的桌面入口（app.asar 内有 `sessionId` 字样的 zod schema（interaction/通知载荷）与 workspace deep-link，但无公开的 `open-session` URI）。**可用的官方打开方式是 CLI TUI：`zcode --resume <sessionId>`**（实测：参数被解析并进入 TUI 加载分支；真实 TTY 下打开该会话的全屏交互界面）。

## 6. 能否通过 session/task id 精确恢复？

**能**：`--resume <sessionId>`（实测进一步确认：`--resume <sess> -p "..." --json` 可在 headless 下恢复**同一** session 续问，`sessionId` 保持不变——会话跨调用持久且精确）。

## 关键实测记录

| 命令 | 结果 |
|---|---|
| `node zcode.cjs -p "只回复OK" --json` | exit 0；stdout JSON：`{"sessionId":"sess_9d27…","response":"OK",...}` —— **官方 sessionId 获取途径** |
| `node zcode.cjs --resume sess_9d27… -p "复述上一条" --json` | exit 0；sessionId 不变、response 正确复述 —— 会话持久 + 精确恢复 |
| `node zcode.cjs --resume sess_9d27…`（无 TTY） | 进入 TUI 加载分支（非 TTY 环境报 `@zcode/tui` 导入错，证明参数已解析并选择 TUI 运行方式；真实终端下即全屏 TUI） |
| 干净环境 provider 行为 | 见 ZCODE_CLI_PROVIDER_AUDIT.md（launcher 已注入官方 env） |

## 结论与方案判定

- **方案 A（Desktop deep-link 创建+注入 prompt）**：当前版本**不成立**——无 prompt 注入入口；workspace deep-link 参数属内部实现且无 prompt/session 语义。
- **方案 B（CLI 初始化 session + 精确打开）**：**成立且全部要素官方支持**——
  1. `-p <bootstrap> --cwd <ws> --json` 初始化/推进一轮学习（skill + teach-mcp 照常工作），并官方取得 `sessionId`；
  2. `--resume <sessionId>` 在新终端窗口打开该课程专属可交互 TUI，用户继续学习。
- **方案 C（Interactive TUI）**：与 B 合流——最终交互面就是 CLI TUI（真正 interactive）。

落地组件：`launcher.launch_learning_mode`（init run，`--json`，stdout→`bridge/init.json`）、`launcher.read_init_session_id`、`launcher.open_learning_session(course_dir, session_id)`、`bridge_session`（interactive 轮询编排：init 完成 → binding.zcode_session_id → 打开 TUI）。
