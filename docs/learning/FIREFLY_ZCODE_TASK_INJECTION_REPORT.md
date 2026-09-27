# Firefly Learning Bridge v0.1 — Task Injection Fix 报告

日期：2026-09-27
范围：Firefly → Z Code Learning Task Injection。未触碰 teach-mcp 业务逻辑 / Memory Ownership / Resource Manifest / curriculum 算法 / mastery / renderer / Firefly 学习 UI 布局。
前置：入口接线（ENTRY_WIRING）、Dialog Runtime（DIALOG_RUNTIME）、Provider Config（ZCODE_CLI_PROVIDER_AUDIT）三份报告的链路已全部打通到进程启动层。

---

## 根因（一句话）

上一轮 provider 修复引入的 `find_official_wrapper` 探测列表包含 `zcode.exe`，而 **Windows 文件系统大小写不敏感**——它命中了 Electron 桌面壳 **`ZCode.exe`**，launcher 的"优先级 A"分支于是 spawn 了桌面 GUI（`wt.exe -d <ws> ZCode.exe`），既没有 `--prompt`，GUI 也不认识课程 workspace——用户看到的就是桌面 GUI 打开了它最近的会话「Firefly Learning Bridge v0.1 审计与实施」。

## Phase 1：launcher 真实行为（日志实录）

用户真实点击（02:25:11，`learning_entry.log`）：

```
[LEARNING_CONTEXT_WRITTEN]（当时尚无此标记，等价链路：LEARNING_BRIDGE）
course_id=crs-d91e981f5a92 action=resume
[ZCODE_LAUNCH] command=C:\Users\FAJ\AppData\Local\Microsoft\WindowsApps\wt.exe
  -d C:\Users\FAJ\AppData\Local\FireflyAI\learning\courses\crs-d91e981f5a92\workspace
  C:\Users\FAJ\AppData\Local\Programs\ZCode\zcode.exe   ← 桌面壳！
  pid=16284 mode=interactive
```

实际参数：executable=wt.exe；cwd（进程工作目录）=课程 workspace；args=仅 `-d <ws> ZCode.exe`；**prompt=无**；**context path=仅经 wt 不传递（环境变量未生效于 CLI 语义）**；**未创建任何 task**；实际启动的是 **Z Code 桌面 GUI**。

为什么看到旧会话：`ZCode.exe` 是 Electron 桌面应用，被拉起后显示其内部最近的活动会话——用户桌面 GUI 里最近的正是开发会话「Firefly Learning Bridge v0.1 审计与实施」。

## Phase 2：Learning Context 实际存在

`<course>/workspace/.firefly/learning_context.json` 真实存在且 schema 合法：

```json
schema_version=1, source=firefly, learner_id=ff-02adc88d,
course_id=crs-d91e981f5a92, manifest_path=<course>\manifest.json,
action=resume, created_at=每次 launch 原子重写
```

`CONTEXT_OK`。此前 interactive（wt）方式下该文件虽被写入，但桌面 GUI 根本不会读它——问题不在 context，在**没有任何东西把它交给会执行的 agent**。

## Phase 3-4：Skill 可发现性 + teach-mcp 可用性（独立环境实证）

- Skill：`~/.zcode/skills/firefly-learning/SKILL.md` 存在（已更新含观测标记协议并重装，checksum 随安装器 copy2 保持一致）。
- **实际执行证据**（非"文件存在"推定）：Phase 10 独立验证中，headless task 输出明确展示了 skill 的 resume 协议行为——`learning_status/resume_learning` 返回的学习者档案（`ff-02adc88d` / quiz 20% / 掌握度 100% / 上次活跃 01:09:05）被逐字汇报。teach-mcp 已加载并被实际调用（有真实 DB 状态读取为证）。

## Phase 5：Bootstrap Prompt（已落地）

`learning/launcher.py::build_learning_bootstrap_prompt(context_path, action)`——短、确定、固定：

```
你正在处理来自 Firefly AI Pet 的学习任务。
必须使用 firefly-learning Skill。
Learning Context：<absolute path>
本次 action：<new|resume|review>
执行规则：
0. 首行原样输出 [FIREFLY_SKILL_START] course_id=... action=...；
   末行原样输出 [LEARNING_ACTION_RESULT] status=... detail=...
1. 读取并验证 Learning Context。 2. 使用其中 learner_id/course_id/action。
3. 不得自行生成任何 ID。 4. 学习状态只从 teach-mcp 获取。
5-7. 按 action 分派（new 协议 / review 人工审核协议 / resume 调
     learning_status+resume_learning，恢复后直接继续教学不等待确认）。
8. 不得根据当前 Z Code 对话历史猜测学习状态。
开始执行。
```

不携带任何课程正文。

## Phase 6：Task Injection 方式判定（实测依据）

- CLI `--help` 语义明确：`-p/--prompt` = 无 TUI 执行任务；TUI 模式**不接受 initial prompt**；桌面 `ZCode.exe` 无 task/deep-link 机制（传参被忽略）。
- **方案 A（官方 new-task+prompt 的 desktop 方式）不存在** → **采用方案 C**：`node zcode.cjs --cwd <课程workspace> -p <bootstrap prompt>`（+provider env + `FIREFLY_LEARNING_CONTEXT`），headless 执行、结果落 teach-mcp/课程 workspace/launch.log，Firefly 弹「已交给 Z Code 学习代理」。
- 架构语义已按任务要求修正：**"打开桌面 GUI"从来不是任务启动**；每次 handoff = 一个新鲜的 learning agent task。
- `--surface desktop` 选项保留观察（官方 headless 呈现口），v0.1 默认不启用。

## Phase 7：不复用无关旧会话

- 每次 `-p` 创建**全新** Z Code session（PoC 实证：每 run 一个新 `sess_*`）。
- launcher **永不**传 `-c/--resume`（那会续接 cwd 最近会话）；学习状态恢复只由 skill 经 teach-mcp 的 `resume_learning(learner_id)` 完成——与 Z Code 会话零耦合。
- 新回归测试锁定：`test_desktop_shell_is_never_mistaken_for_a_wrapper`（桌面壳永不被当 wrapper）+ `test_11`（prompt 契约含"不得根据对话历史猜测学习状态"）。

## Phase 8：Handoff Trace（双侧落地）

Firefly 侧（`learning_entry.log`，已实测输出）：

```
[LEARNING_CONTEXT_WRITTEN] course_id=crs-d91e981f5a92 action=resume context_path=...
[ZCODE_TASK_INJECT] cwd=... context_path=... action=resume
[ZCODE_PROCESS_STARTED] pid=43528 mode=task-injected
```

Skill 侧（SKILL.md §0.5 + bootstrap prompt 双写）：`[FIREFLY_SKILL_START]` / `[MCP_READY]` / `[LEARNING_ACTION_DISPATCH]` / `[LEARNING_ACTION_RESULT]`。已知限制：标记行依赖模型遵守，非硬保证——**权威判据仍是 launch.log 内容 + teach-mcp 状态变化**。

## Phase 9：测试升级（55 项全绿）

不再以 `process_launched=True` 为成功标准：

- argv 必含 `--prompt`（task_injected）且 prompt 含 context 绝对路径、`本次 action：<action>`、skill 名、防历史猜测规则（test_10/test_11）。
- 桌面壳误判回归锁定（provider env 测试文件内 2 项 wrapper 契约测试）。
- dialog 交接契约：只传 (course_id, action)，prompt 由 launcher 统一附上（wiring 测试）。
- 端到端 visible/skill 证据：`test_learning_dialog_runtime.py` + Phase 10 真实链（`task_injected=True, skill_started=True, mcp_called=True` 在 Phase 10 实测中同时成立）。

## Phase 10：真实独立验证（不经 Firefly）

`launch_learning_mode("crs-d91e981f5a92", "resume")`（与 Firefly 完全相同代码路径）→ pid 43528 → 退出后 launch.log 尾部：

```
学习者档案：learner_id ff-02adc88d（初学者水平）/ 当前主题 RC 电路充放电
/ 上次阶段 quiz（进度 20%）/ 掌握度 100% / 上次活跃 2026-09-27 01:09:05
好的，已恢复学习会话。……
📋 学习进度回顾 → 水杯类比讲解 → 出下一题（τ 时 6.32V 题，A/B/C/D）
```

= **resume_learning 恢复了原 session（与 v0.1 PoC 答题记录一致），并自动继续教学**。

## Phase 11：真实用户 E2E

待用户执行（完全退出 Firefly → 重启 → 学习模式 → 选已有课程 → 继续学习）：预期依次看到 LearningBridgeDialog →「已交给 Z Code 学习代理」提示 → （后台 headless 任务执行，过程在 `bridge/launch.log`）→ teach-mcp 状态推进。注意：**本修复后不再弹出终端窗口**（headless task 语义），学习过程与结果通过 Firefly 的对话框提示、`launch.log` 与下一次 teach-mcp 查询呈现。

---

## 结论（10 问速答）

1. **为什么只打开 Z Code**：wrapper 探测大小写不敏感地命中了 Electron 桌面壳 `ZCode.exe`，spawn 了 GUI 而非 CLI 任务。
2. **是否有真正的 initial prompt**：此前 interactive 分支没有；现已改为每次 launch 必附确定性 bootstrap prompt（`-p`）。
3. **Learning Context 是否真实传入**：是——文件每次 launch 原子重写、prompt 内含绝对路径、env `FIREFLY_LEARNING_CONTEXT` 同步传入（`CONTEXT_OK`）。
4. **firefly-learning 是否实际启动**：是（Phase 10 真实链：resume 协议行为逐字呈现）。
5. **teach-mcp 是否实际被调用**：是（learning_status/resume_learning 返回的真实 DB 状态被汇报）。
6. **是否创建新的 learning task**：是——每次 launch 一个全新 headless Z Code task（新 sess_*），永不开桌面 GUI。
7. **是否错误复用了开发会话**：此前是（桌面壳打开最近会话）；现已结构性排除（无 GUI 路径 + 无 `-c/--resume` + 回归测试锁定）。
8. **action=resume 是否恢复原 session**：是——独立验证恢复出 `ff-02adc88d-20260927-010222-11f5` 的真实状态（quiz 20%、掌握度 100%）。
9. **用户点击后是否直接进入学习流程**：是——点击「继续学习」后任务自动执行（复述进度 → 讲解 → 出题）。
10. **用户是否还需手动说"开始学习"**：**不需要**。点击后零二次操作；学习过程以 headless agent task 形式自动执行，结果经 teach-mcp 持久化（下一轮点击「继续学习」即从新进度恢复）。

完成后停止。未扩展任何禁止项。
