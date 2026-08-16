# Phase 9 Preflight — Agent Router & Workflow Architecture

Status: **PREFLIGHT / ARCHITECTURE ONLY**. No production code changes.
Date: 2026-08-15
Scope: full audit of `core/`, `ui/`, `app.py`, `state_broker.py`; router / workflow design; safety boundaries; implementation slices (9A–9E). Nothing here is implemented in this round.

---

## 1. Executive Summary

**我们现在是否 ready for Router？——部分 ready。**

- **Ready today（Phase 9A 可立即开始）**：确定性推荐层。Router 不碰 QWidget / Hooks / provider JSON / runtime JSON，只消费结构化 core model，这一约束在当前代码里完全成立。推荐逻辑可以做到 100% 可解释、无 LLM、无在线调用、纯函数可单测。
- **Not ready（还缺 primitives）**：任何 **自动多 Agent workflow 编排（Level 3）**。三条硬事实决定这一点：

  1. **Codex 的 Firefly 托管执行路径尚未启用**（Short Talk 在线调用数 = 0；`app.py` 对 codex 显示 "Codex Short Talk isn't available yet."）。`build_codex_args` 与 `CodexJsonlAdapter` 存在且结构完整，但从未在线验证。
  2. **Workflow 级完成检测不存在**。现有 `AgentEvent` 是"单个 agent 单次 turn"的事件；"这条 workflow 走到哪一步、下一步该是谁"没有模型。原生 Codex 的生命周期 hooks 存在但**无失败信号**（`Stop → success` 会在工具级失败时也报 success），无法可靠判定"实施步真的成功"。
  3. **没有 artifact 层**。跨 Agent 安全交接所需的 `runtime/artifacts/<workflow_id>/`、结构化摘要、文件清单枚举（git status/diff）都不存在。

**推荐起点：Level 1（Recommendation only）。** 理由见 §16 与 §20：诚实的能力不对称（Claude 在线、Codex 离线、ChatGPT 无后端）、安全优先（Router 只 Recommend 不 Launch）、以及现有 `ShortAskPanel.show_recommendation()` 已证明的 UX 模式可以零成本复用。

---

## 2. Current Capability Inventory

### 2.1 core/ 层（全部 Qt-free，均可被 Router 安全复用）

| 模块 | 提供什么 | 对 Router 的可复用性 |
|---|---|---|
| `core/models.py` | `AgentState` / `LifecycleState` / `ResolvedState` 类型化生命周期 | 只读观测基础；Router 不写 |
| `core/agent_events.py` | `AgentEventType`（STARTED/SESSION/STATUS/TEXT_DELTA/TOOL/PERMISSION/FINAL/ERROR/CANCELLED）、`ErrorCategory`、`AgentEvent`、`AgentCapabilities`、adapter 契约 | **核心事件词汇**；WorkflowCoordinator 消费的输入事件 |
| `core/agent_adapters.py` | `ClaudeStreamAdapter` / `CodexJsonlAdapter` / `make_adapter` / `capabilities_for` | provider JSON 解析唯一归属；Router 永不直接解析 |
| `core/session_manager.py` | 单一会话所有者（agent×workspace，Claude=session / Codex=thread），`get/has/clear/for_workspace`，`classify_stale_resume` | Router 通过它查 `session_resume` 能力；**不直接写 sessions** |
| `core/session_store.py` | `config/sessions.json` 原子/版本化/封顶持久化 | **workflow 持久化应镜像此模式**（§15） |
| `core/state_monitor.py` | 150ms 轮询 `runtime/sources/*.json` → `agent_state_changed` / `resolved_state_changed` | 生命周期观测唯一入口；外部 Hooks 状态只读 |
| `core/workspace_manager.py` | 当前 workspace + recents | Router 取 workspace 上下文 |
| `core/notification_manager.py` | success/error `NotificationEvent`（episode 去重） | 不直接复用；Coordinator 可另发 WorkflowEvent |
| `core/keep_awake.py` | 聚合 busy 状态保活 | workflow 运行期可挂钩（未来） |
| `core/settings_manager.py` | `config/pet_preferences.json`（notifications/keep_awake/greeting） | 未来可加 `prefer_low_cost` 偏好（§18） |
| `core/quick_ask_metrics.py` | T0–T6 延迟遥测，allowlisted 载荷，`session_hash_prefix` | 遥测模式样板（不存 prompt/response/key） |

### 2.2 ui/ 层

| 模块 | 提供什么 | 对 Router 的意义 |
|---|---|---|
| `ui/process_launcher.py` | `ProcessLauncher`（wt.exe 原生启动）、`QuickAskRunner`（QProcess 异步、每 turn 一个 adapter、`agent_event` 信号、取消=taskkill） | **用户确认交接（Level 2）要用的单次执行原语**；Router 不拥有进程 |
| `ui/quick_chat_protocol.py` | `build_claude_args`（`--safe-mode` + `--permission-mode plan` 只读）、`build_codex_args`（`exec --sandbox read-only --json`；**刻意不加 `--skip-git-repo-check`**）、`classify_short_ask`（关键词 simple/complex） | `classify_short_ask` 是确定性 Router 的**种子**（§7） |
| `ui/short_ask.py` | `ShortAskPanel`（AgentEvent 驱动状态机）、**`show_recommendation()` 推荐 UX 已存在**（"Open Claude" + "Ask anyway"） | 9B 推荐卡直接复用此交互模式 |
| `ui/agent_dock.py` | claude/codex/chatgpt 三 dock | 用户手动选 Agent 的现有入口 |
| `ui/permission_card.py` | **observer-only**（detect/notify/View，永不合规/批准） | 9 节权限边界的不变基线 |
| `ui/overlay_coordinator.py` | z-order 单点、popover 优先级、`suspend_short_ask()`（挂起不取消） | 9B/9C 推荐卡与进度卡沿用此挂起策略 |
| `ui/session_popover.py` | 会话概览（hash 前缀显示，原生 id 不出现在 UI） | 会话存在性的 UI 呈现 |
| `ui/workspace_popover.py` / `settings_popover.py` / `speech_bubble.py` / `pet_overlay.py` / `vertical_toolbar.py` / `theme.py` / `popover_base.py` / `workspace_store.py` / `companion_panel.py` | 视觉岛、锚定、Light Glass tokens | 9B/9C 视觉基线；CompanionPanel 仍在盘但未导入 |

### 2.3 编排层与外部状态

- **`app.py`（VisualShell 组合根）**：`state_monitor` → {dock, session_popover, coordinator, notification_manager, keep_awake}；`quick_ask.agent_event → short_ask.on_agent_event`；Short Talk 编排在 `_on_short_ask_requested/_send/_force_send/_failed/_finished`。codex/chatgpt 走 `show_notice`；复杂 prompt 走 `show_recommendation`；stale resume 一次性回退。**所有状态写入都经各 Manager，没有任何 UI 直写 JSON。**
- **`state_broker.py`**：纯 Python 优先级/TTL 解析 → `runtime/state.json`。只读观测，Router 不碰。
- **外部 Hooks（observer-only，本轮只读审计，不修改）**：
  - **Claude**：`~/.claude/settings.json` 8 hooks（SessionStart/UserPromptSubmit/PreToolUse/PermissionRequest/**PostToolUseFailure/StopFailure**/Stop/SessionEnd）→ 经 `simulate_event.py` 写 `runtime/sources/claude.json`。**有失败信号。**
  - **Codex**：`~/.codex/hooks.json` 7 hooks（SessionStart/UserPromptSubmit/PreToolUse/**PostToolUse**/PermissionRequest/Stop/SessionEnd）→ 写 `runtime/sources/codex.json`（`source: "hook"`，已验证）。**无 PostToolUseFailure/StopFailure**：工具级失败也会被 `Stop → success` 报为 success。
- **模型事实（本机 config 实证）**：Claude 走本地代理，haiku→`deepseek-v4-flash`（廉价、快）；Codex model=`gpt-5.6-sol`、`service_tier="priority"`（昂贵）。→ 成本控制必须把 Claude 当廉价路径、Codex 当昂贵路径（§18）。

### 2.4 直接可复用 vs 缺失 vs 禁止接管

**Router 可直接复用：**
- `AgentEvent` + adapters（观测单 agent turn）
- `SessionManager`（resume 能力查询）、`WorkspaceManager`（workspace 上下文）
- `classify_short_ask` 的启发式模式（升级为 core 规则引擎）
- `ShortAskPanel.show_recommendation()` 的 UX 模式
- `SessionStore` 的持久化模式（workflow 持久化照抄）
- `QuickAskRunner.ask()`（Level 2 交接的托管执行原语）

**缺失的 primitives（§3）：** TaskRequest / 能力注册表 / AgentRecommendation / WorkflowPlan+Step / WorkflowEvent / WorkflowCoordinator / artifact 层 / workflow 持久化 / 完成检测 + 文件清单枚举。

**Router 绝对不接管：**
- QWidget 与任何 UI 控件
- Hooks（`~/.claude/settings.json`、`~/.codex/hooks.json`）与权限/审批/sandbox/trust
- provider JSON 解析（留在 adapters）
- `runtime/sources/*.json`、`runtime/state.json`、`config/sessions.json` 直读直写（只经 Manager）
- 自动批准 permission、自动启动原生 surface 而不确认
- ChatGPT（无后端，不伪造能力）

---

## 3. Missing Primitives

按实施顺序（含预计归属模块）：

| Primitive | 模型/职责 | 归属 |
|---|---|---|
| `TaskRequest` | 用户意图的最小结构化描述（§4） | `core/task_request.py` |
| 能力注册表 | 每个 Agent 在 Firefly 托管路径下的真实能力（§5） | `core/agent_capabilities.py` |
| `AgentRecommendation` | 推荐结果：agent、intent、理由、风险、确认要求（§6） | `core/agent_recommendation.py` |
| Router | 确定性推荐引擎（§6-7） | `core/agent_router.py` |
| `WorkflowPlan` / `WorkflowStep` | 有序步骤（§10） | `core/workflow_plan.py` |
| `WorkflowEvent` | workflow 级生命周期事件（§11） | `core/workflow_events.py` |
| `WorkflowCoordinator` | 步骤转移 / 确认门 / artifact 交接 / 事件聚合 / 失败处理（§9） | `core/workflow_coordinator.py`（9D 才实现） |
| artifact 层 | `runtime/artifacts/<workflow_id>/` 安全交接（§12） | `core/artifact_store.py` |
| 完成检测 + 文件清单 | 判定 agent 步结束、枚举 changed files | git 只读小工具 + 生命周期信号 |
| workflow 持久化 | `config/workflows.json`（§15） | `core/workflow_store.py`（9E） |

---

## 4. TaskRequest Model

目标只回答"用户现在想做什么"。**不设计几十个字段。**

```python
# core/task_request.py (concept, 不实现)
class TaskIntent(str, Enum):
    EXPLAIN = "explain"        # 解释 / 总结 / 分析
    REVIEW = "review"          # 审查 / 复核
    IMPLEMENT = "implement"    # 修改代码 / 工程实施
    BUGFIX = "bugfix"          # 定位并修复
    QUESTION = "question"      # 一般提问
    VISION = "vision"          # 视觉 / UI 相关
    UNKNOWN = "unknown"

class RiskLevel(str, Enum):
    LOW = "low"      # 只读分析、解释
    MEDIUM = "medium"  # 可能改动但范围小 / 可逆
    HIGH = "high"    # 写文件、跑命令、不可逆

@dataclass(frozen=True, slots=True)
class TaskRequest:
    text: str                     # 原始用户输入（Router 只读，不存储）
    workspace: Path               # 当前工作区（来自 WorkspaceManager）
    requested_agent: str | None   # 用户显式指定的 agent，可空
    intent: TaskIntent            # 意图（Router 内部由 text 推导，或外部预判）
    requires_write: bool          # 是否可能写 workspace（写 = 高风险）
    risk: RiskLevel               # 风险等级
    expected_output: str | None   # 用户期望的产物描述（可选）
```

要点：
- `intent` 允许外部预判（UI 层可先用关键词提示），但 Router 会以**自己的规则重新判定**为准。
- `requires_write` 与 `risk` 是 Router 决策的**硬输入**：任何 `requires_write=True` 或 `risk>=MEDIUM` 的步骤都强制 `requires_confirmation=True`（§13）。
- 不包含：API key、secret、provider session id、完整 transcript。

---

## 5. Agent Capability Model

现有 `AgentCapabilities`（streaming/resume/cancel/tools/permissions/managed_session）是**能力标志**，太薄，且是静态 provider 属性。Router 需要的是**行为能力的注册表**，并且必须区分 **托管路径（Firefly 可直接调用）** 与 **原生路径（需启动 native surface）**。

能力键（对齐需求 §5）：`chat / analysis / coding / review / vision / file_read / file_write / terminal / long_task / session_resume`，再加 `managed_exec`（Firefly 能否托管执行）。

**诚实的能力真相（基于真实代码 + 已验证事实）：**

| 能力 | Claude（托管 Short Talk） | Codex（托管） | Codex（原生） | ChatGPT |
|---|---|---|---|---|
| `managed_exec` | ✅ 已验证（8C 在线 smoke） | ❌ 未启用（在线 0） | —（native surface） | ❌ |
| `chat` | ✅ | ❌ | ❌ | ❌（Firefly 无后端） |
| `analysis` | ✅ | — | ✅（能分析但非首选） | ❌ |
| `coding` | ⚠️ 托管路径 ❌（safe-mode + `--permission-mode plan` = 只读，无交互工具 UX） | — | ✅（`exec --sandbox read-only` + 原生） | ❌ |
| `review` | ✅（读上下文给结论） | — | — | ❌ |
| `vision` | ❓ 托管路径未验证 | — | ⚠️ 原生有 visualize/browser/computer-use 插件（config 实证），但未经 Firefly 验证 | ❌ |
| `file_read` | ⚠️ plan 模式只读工具可读，但 `--safe-mode` 关 CLAUDE.md/MCP | — | ✅ | ❌ |
| `file_write` | ❌（plan 模式只读） | — | ✅ | ❌ |
| `terminal` | ❌ | — | ✅ | ❌ |
| `long_task` | ❌（复杂任务 steer 到原生） | — | ✅ | ❌ |
| `session_resume` | ✅（8C.4 跨重启验证） | — | ✅（thread，结构支持） | ❌ |

结论：**三者能力不对称是硬事实，注册表必须如实编码，不得假装对称。** 例如 `vision` 不得对任何 Agent 标 True（托管路径未验证），只允许"推荐打开原生 surface 尝试"。

```python
# core/agent_capabilities.py (concept)
@dataclass(frozen=True, slots=True)
class AgentCapability:
    agent_id: str
    managed_exec: bool
    chat: bool
    analysis: bool
    coding: bool
    review: bool
    vision: bool            # 托管路径已验证才为 True
    file_read: bool
    file_write: bool
    terminal: bool
    long_task: bool
    session_resume: bool
    cost_tier: str          # "low" | "high"   (claude=low, codex=high)
```

注册表是 **core 内单一声明处**，Router / Coordinator / 未来 UI 都从它读；不再在 adapters 里硬编码能力（`CLAUDE_CAPABILITIES` 保留供 QuickAskRunner 旧路径使用，二者不冲突：adapters 描述"这次调用支持什么"，注册表描述"这个 agent 能做什么"）。

---

## 6. Router Design

**约束（写死）：** Router 不操作 QWidget、不直读 runtime JSON、不改 Hooks、不自动批准 permission、不持有 API key、不直接解析 provider JSON。Router 只消费结构化 core model，产出 `AgentRecommendation`。**Qt-free、无副作用、可纯函数单测。**

建议关系（保持需求 §3 的流向）：

```text
UI (Short Ask / 推荐卡)
   ↓  TaskRequest
AgentRouter  (core/agent_router.py, deterministic rules only)
   ↓  AgentRecommendation
   └── 用户确认门（Level 2/3 才启动 agent）
   ↓  AgentManager? → 否（§8）→ 直接
QuickAskRunner / ProcessLauncher   (执行原语，已有)
   ↓  AgentEvent (每 turn)
WorkflowCoordinator  (9D 才实现)
   ↓  WorkflowEvent
Firefly feedback (SpeechBubble / 进度卡)
```

```python
# core/agent_recommendation.py (concept)
@dataclass(frozen=True, slots=True)
class AgentRecommendation:
    agent_id: str
    intent: TaskIntent
    reason: str              # 机器可读代码 + 人读短句（UI 决定显示语言）
    risk: RiskLevel
    requires_confirmation: bool   # 恒为 True when risk>=MEDIUM or requires_write
    mode: str                # "managed" | "native" | "unavailable"
    fallbacks: tuple[str, ...]    # 备选（如 review 备选 same-agent）
    estimated_steps: int     # 若作为 workflow，预计 agent 调用数（§18）
```

Router 输出必须是**排序的推荐列表**（首推 + 备选），永远只 Recommend。启动与否由用户确认门决定。

---

## 7. Recommendation Rules

**第一版：纯确定性规则，不用 LLM。** 规则全部可在 `core/agent_router.py` 用表驱动实现；`classify_short_ask` 的关键词启发式是它的种子，但 Router 的规则更宽（意图 + 风险 + 能力 + 成本 + 用户偏好）。

规则表（优先级从高到低）：

| 条件 | 推荐 |
|---|---|
| 用户显式 `requested_agent` | 直接尊重（仍校验能力；不可用则提示） |
| `intent == VISION` 或提示词含 UI/视觉 | Codex（native，vision-capable path，注明未验证）→ 否则 Claude |
| `requires_write` 或 `intent in {IMPLEMENT, BUGFIX}` | Codex（native 实施）→ 或 Claude analysis first → 或 "Ask Claude first" |
| `intent == REVIEW` | Claude（review） |
| `intent in {EXPLAIN, QUESTION}` | Claude（analysis / chat） |
| 无法判定 | Claude（默认，低成本） |

附加规则：
- **`prefer_low_cost`**（来自偏好/用户/会话）：同为可选时倾斜 Claude（deepseek-v4-flash 廉价；Codex gpt-5.6-sol 昂贵，§18）。
- 任何 `requires_write=True` 或 `risk>=MEDIUM` → `requires_confirmation=True`，推荐文案必须带风险提示。
- `managed_exec=False` 的 agent 只能给 `mode="native"` 或 `mode="unavailable"` 的推荐。
- 不自动发起任何 agent 调用；不自动跨 Agent。

`classify_short_ask` 保留在 `ui/quick_chat_protocol.py` 供 Phase 8C 旧路径使用；9A 的 Router 是独立 core 规则引擎，未来可反向让旧分类器委托 Router（不在本轮做）。

---

## 8. AgentManager Decision

**结论：Phase 9 不建立 `core/agent_manager.py`。** 审计证据：

- 会话所有权已由 `SessionManager` 单点承担（8C.4 已验证跨重启）。
- 进程执行已由 `ProcessLauncher` + `QuickAskRunner` 承担。
- 生命周期观测已由 `StateMonitor` 承担。
- 能力真相需要一个**新**声明处，但那是**能力注册表**（§5），不是"manager"。

如果现在建一个 AgentManager 聚合上述四项，它只会是一个**空 facade**——每项职责都已有一个 owner，聚合层除了转发信号没有真实业务。需求 §17 说"不要为了名字好看而创建空 facade"，这与审计结论一致。

**替代方案：** 需要的唯一新增是 `core/agent_capabilities.py` 注册表，作为"这个 agent 能做什么 / 成本档 / 托管可用性"的单一声明处。分工保持不变：

- `ProcessLauncher` / `QuickAskRunner` → 执行
- `SessionManager` → 会话
- `StateMonitor` → 生命周期观测
- `AgentRouter` → 决策（只读注册表）
- `WorkflowCoordinator`（9D）→ 编排（只读注册表、消费 AgentEvent）

---

## 9. WorkflowCoordinator Decision

**结论：需要 `core/workflow_coordinator.py`，但只在 9D 实现，且职责严格收敛。**

必要性判断：
- **9A/9B（推荐层）**：不需要。Router 产出推荐即可。
- **9C（用户确认交接）**：是单次转移（confirm → 启动一个 agent），可由 app.py 编排，不需要完整 Coordinator。但若 9C 顺手搭一个极薄的"交接门"抽象，9D 可平滑升级。
- **9D（第一条 workflow）**：必要。步骤转移、确认门、artifact 交接、AgentEvent 聚合、失败处理是真实多步状态机，塞进 app.py 会让组合根爆掉。

职责（必须）：
- step transition（按 `WorkflowStepStatus` 前进）
- confirmation gate（每步 `requires_confirmation`）
- artifact handoff（把上一步产物路径传给下一步）
- AgentEvent aggregation（把每步的单 agent 事件归类到 workflow 视角）
- failure handling（§14：失败 → 停 workflow → Firefly summary）

**不得（写死）：** provider JSON 解析、UI 渲染、permission 批准。

设计约束：Qt-free core service（回调 listener 模式，同 SessionManager/NotificationManager 风格），由 app.py 注入 `QuickAskRunner`（执行）与 `WorkflowStore`（9E 持久化）。UI 只消费 `WorkflowEvent`（§11）。

---

## 10. WorkflowPlan Model

```python
# core/workflow_plan.py (concept, 不实现)
class WorkflowStepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"     # 等待用户确认 / 等待 permission
    SUCCESS = "success"
    ERROR = "error"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"

@dataclass(frozen=True, slots=True)
class WorkflowStep:
    agent_id: str
    intent: TaskIntent
    input_prompt: str            # 传给该 agent 的提示（显式用户任务，不是转录）
    input_artifacts: tuple[str, ...]   # 上一步产物引用（绝对路径）
    expected_output: str | None
    requires_confirmation: bool  # 写/高风险步恒为 True
    status: WorkflowStepStatus
    artifact_outputs: tuple[str, ...]  # 本步产物（完成时填充）
    started_at: int | None
    updated_at: int | None

@dataclass(frozen=True, slots=True)
class WorkflowPlan:
    workflow_id: str
    workflow_type: str          # e.g. "plan_implement_review"
    workspace: Path
    steps: tuple[WorkflowStep, ...]
    prefer_low_cost: bool
    status: str                 # pending/running/waiting/success/error/cancelled/skipped
    created_at: int
    updated_at: int
```

示例（§17 第一条 workflow）：

```text
Step 1  claude      analysis   input=user task          → plan.md
Step 2  codex       implement  input_artifacts=[plan.md] → changed files
Step 3  claude      review     input_artifacts=[plan.md, changed_files.txt]
```

---

## 11. WorkflowEvent Model

**结论：新增独立 `core/workflow_events.py`，不改 `AgentEvent` 语义。**

理由：`AgentEvent` 的契约是"**一个 agent 的**一次 turn 内发生了什么"（provider 无关的交互事件）。workflow 事件是"**跨多个 agent 的编排**"发生了什么。把 `WORKFLOW_*` 塞进 `AgentEvent` 会污染它，且会让 `ShortAskPanel`（只认 AgentEvent）收到不该显示的编排噪音。两者正交，分开定义、由 Coordinator 把 AgentEvent 翻译成 WorkflowEvent。

```python
# core/workflow_events.py (concept)
class WorkflowEventType(str, Enum):
    WORKFLOW_STARTED = "workflow_started"
    STEP_STARTED = "step_started"
    STEP_WAITING = "step_waiting"
    STEP_COMPLETED = "step_completed"
    STEP_FAILED = "step_failed"
    STEP_CANCELLED = "step_cancelled"
    STEP_SKIPPED = "step_skipped"
    WORKFLOW_COMPLETED = "workflow_completed"
    WORKFLOW_FAILED = "workflow_failed"
    WORKFLOW_CANCELLED = "workflow_cancelled"
    WORKFLOW_PAUSED = "workflow_paused"   # Firefly 重启中断（9E）

@dataclass(frozen=True, slots=True)
class WorkflowEvent:
    workflow_id: str
    step_index: int          # -1 = workflow 级
    type: WorkflowEventType
    timestamp: int
    agent_id: str | None
    text: str | None         # 结构化摘要/短讯，无 display language
    metadata: dict | None    # allowlisted（artifact 路径、耗时等），禁 secret
```

映射：STEP 运行期间，Coordinator 把该步的 AgentEvent 汇总（保留 start/end 时间、错误类别、artifact 输出），只在状态跃迁时发 WorkflowEvent。UI 端 `ProgressCard` 只消费 WorkflowEvent。

---

## 12. Artifact Handoff

**原则：Artifact-first，不是 Transcript-first。**

禁止默认把完整 transcript、环境变量、secret、全部 workspace 文件直接复制给下一个 Agent。只传：

1. **artifact reference**（绝对路径 / `runtime/artifacts/<workflow_id>/` 下的文件）
2. **structured summary**（上一步的结论摘要，有界文本）
3. **file paths**（changed files 清单）
4. **explicit user task**（原始用户目标）

**布局（9D 实现时）：**

```text
runtime/artifacts/<workflow_id>/
    plan.md            ← Claude Plan 步产物（Short Ask FINAL 文本）
    changed_files.txt  ← Codex 步后 Firefly 用只读 git 枚举
    summary.json       ← 每步结构化摘要（allowlisted，无 secret）
```

**交接链（§17 原型）：**

```text
Claude (Short Ask) → 产出 plan 文本 → Firefly 写入 plan.md
Codex (实施)       → input_artifacts=[plan.md] + 用户任务 → 完成后
Firefly 只读跑 git status --porcelain / git diff --stat → changed_files.txt
Claude (review)    → input_artifacts=[plan.md, changed_files.txt] + 用户任务
```

细节规则：
- Claude Plan 的产物是 **Short Ask 的 FINAL 文本**（有界、用户可见、非全转录），由 Firefly 写入 artifact 目录；**默认不写用户 workspace**（除非用户确认）。
- 传给 Codex 的 prompt 是"读 `<plan.md 绝对路径>` 并按它实施"+原始用户任务，**不是**把 Claude 完整会话复制过去。
- `changed_files.txt` 由 Firefly 在 Codex 完成后**只读**枚举（`git status`/`git diff --stat`，无 force、无写操作）。若 workspace 非 git 仓库，退化为"用户手动指定文件或跳过 review 步"。
- 新引入的最小工具：git 只读读取 helper（`core/git_reader.py`，只读，永不 `--force`/`reset`/`clean`）。

**为什么 artifact-first 更安全：** transcript-first 会把上一 Agent 的环境、中间失败、无关对话、潜在 secret 全部泄漏给下一 Agent；artifact-first 只暴露有界、用户可见的产物，且每步都有明确边界可审计、可回滚。

---

## 13. Permission Boundary

**基线（维持现状，写死不破）：**

- **Native Agent = approval authority。** 真实 permission 决定与批准只发生在 Claude/Codex 原生表面。
- **Firefly = Detect / Notify / Focus。** `PermissionCard` 是 observer-only：检测 `waiting`、通知、"View"（启动原生 surface）；永不合规、批准、改策略。
- **Router / WorkflowCoordinator 永不自动批准。**

具体到 workflow：

- 任何 `requires_write=True` 或 `risk>=MEDIUM` 的 `WorkflowStep` 必须 `requires_confirmation=True`（§10）。
- Coordinator 的确认门**只决定"要不要启动该 agent 步骤"**，不代替 agent 内部权限。
- 启动 Codex 原生实施时，沿用现有约束：**不加 `--skip-git-repo-check`**（不能绕过 trust），未信任目录由 Codex 原生拒绝。
- 不新增第二套 permission 系统；不新增 approval 入口。

---

## 14. Failure / Cancel Semantics

**状态机（每步）：** `pending → running → (waiting → running) → success | error | cancelled | skipped`。workflow 级同构（§10 status）。

**失败规则（第一版，保守）：**
- 任一步 `error` → **停止 workflow** → Coordinator 发 `STEP_FAILED` + `WORKFLOW_FAILED` → Firefly 汇总（已完成的 artifact 保留、失败步原因给出、下一步选项）。
- **默认不自动换 Agent 重试。** 自动重试只有一种（可选）：同一 Agent 同一 step 重跑 ≤1 次（例如 transport 抖动），且必须用户确认。
- **不默认"失败就换另一个 Agent"**：会掩盖根因、放大 token 成本、混淆责任。

**取消语义（沿用 8C.3 的挂起-不-取消策略）：**
- 正在运行的 agent turn：用户 Stop → CANCELLING → transport 真实结束 → `STEP_CANCELLED` + `WORKFLOW_CANCELLED`。
- Firefly 重启（9D）：workflow 标记 `interrupted`（paused），用户手动决定恢复/放弃；9E 才做持久化恢复。

**Codex 失败不可靠的特判：** Codex 生命周期无失败 hook，`Stop → success` 会在工具级失败时误报 success（§2.3）。因此 Codex 步的"成功"判定必须**双信号**：生命周期 success + 用户确认门（"Codex 报告完成。请确认结果？"）。这不是敷衍，是已知不对称的诚实暴露。

---

## 15. Persistence Strategy

**结论：workflow 应当跨 Firefly restart 恢复**（多步 workflow 可能跑分钟级，中途崩溃让用户重来不可接受）。

**存储：`config/workflows.json`**，完全镜像 `SessionStore` 模式（原子写、版本化、坏文件降级为空、单记录容忍、上限 ~20 条、只存元数据）。

**存什么（仅元数据）：**

```text
version, workflows: [
  { workflow_id, workflow_type, workspace,
    status, steps: [
        { agent_id, intent, status, input_artifact_refs,
          output_artifact_refs, requires_confirmation,
          started_at, updated_at } ],
    created_at, updated_at } ]
```

**禁止存：** 完整 conversation、transcript、API key、secret、session id、prompt/response 正文（artifact 本身存 runtime/artifacts，workflows.json 只存路径引用）。

**恢复语义（9E）：** 启动时读入 → 未完成 workflow 标 `paused` → 用户确认后从**失败/中断步骤**继续（**不自动继续**）。每步恢复都重新过确认门。artifact 若缺失（中途被删）→ 该步标 error，用户选择重跑该步。

---

## 16. UX Concept

**约束：不新增大窗口、不恢复大型 Companion Panel、不做 dashboard。** 保持 Light Glass、低密度、Firefly-centered。

**A. Task Recommendation Card（9B）**

一个小锚定卡（锚定方式同 PermissionCard / ShortAskPanel，`OverlayCoordinator` 单点管 z-order），直接复用 `show_recommendation()` 的交互心智：

```text
┌───────────────────────────────┐
│ ⚡ This looks like a coding task.  │
│ Recommended: Codex               │
│ [Open Codex]  [Ask Claude first] │
└───────────────────────────────┘
```

- 触发：Short Talk 输入 → `classify_short_ask/Router` 判定复杂/写意图。
- `[Open Codex]` = 用户确认 → 启动 Codex（native 或托管）。`[Ask Claude first]` = 改走 Claude 分析。可 `[×]` 关闭。
- 有风险时文案加一行："This step may modify files in <workspace>."
- 挂起策略沿用 `suspend_short_ask`（被更高优先级 overlay 打断时隐藏不取消）。

**B. Workflow Progress Card（9D）**

紧凑进度卡，只消费 `WorkflowEvent`，显示步骤列表 + 状态点（success/error/pending），无日志、无 transcript：

```text
┌───────────────────────────────┐
│ Plan        ● Claude   ✓      │
│ Implement   ● Codex    ⟳      │
│ Review      ● Claude   ○      │
│     3 steps · Claude×2 + Codex×1│
└───────────────────────────────┘
```

- 每步展开一行；失败步给短因（来自 WorkflowEvent.text 的简短摘要）。
- Firefly 汇总：workflow 结束后 SpeechBubble 一句 "Done · 3 steps · 6m 12s"（工具提示给出每步耗时，来自元数据）。
- **不展示** transcript、完整 diff、权限细节。

---

## 17. First Workflow Prototype

**唯一 workflow：`plan_implement_review`（Claude Plan → Codex Implement → Claude Review）。**

八个问题的答案：

**1. Claude Plan 用 Short Ask 还是 native surface？**
→ **托管 Short Ask（`QuickAskRunner` + `claude -p --safe-mode`）**。这是唯一在线验证过的路径（8C.1/8C.3/8C.4 smoke），有返回文本、有 resume、有隔离（不污染外部 claude.json）。native surface 无返回值，无法作为 workflow 输入。

**2. Plan 如何保存？**
→ Firefly 把 Short Ask 的 FINAL 文本写入 `runtime/artifacts/<workflow_id>/plan.md`（Firefly 自有目录，默认不碰用户 workspace；用户确认可另存到 `PLAN.md`）。

**3. Codex 如何获得 plan？**
→ prompt = "Read `<绝对路径>/plan.md` and implement it." + 原始用户任务。Codex 运行在 workspace cwd，可读同机绝对路径。若 Codex 托管路径在 9D 仍不可用，则实施步走 **native surface**（Codex 原生启动）+ 生命周期检测。

**4. 如何知道 Codex 完成？**
→ 双信号：
   - 托管：`AgentEvent.FINAL` / 进程 exit（若 9D 启用了 Codex 托管）。
   - native：Codex 生命周期 hooks（thinking/working → Stop success → SessionEnd sleeping）经 StateMonitor，**配合**用户确认门（因无失败 hook，见 §14）。
   - 佐证：Firefly 只读 git 变化检测（工作区 mtime / git status）。

**5. 如何获得 changed files / artifact？**
→ Codex 步结束后，Firefly 只读跑 `git status --porcelain` + `git diff --stat`（core/git_reader.py），写出 `changed_files.txt`。非 git 仓库 → 退化为用户指定或跳过 review。

**6. Claude 如何 review？**
→ 托管 Short Ask：prompt = plan.md 路径 + changed_files.txt 路径 + 原始用户任务 + "Review the changes."。只读。

**7. 每一步需要哪种用户确认？**
→
   - Workflow 启动（看到 plan 摘要后）——确认。
   - Step 2 Codex implement（写 workspace，`requires_confirmation=True`）——确认。
   - Step 3 Claude review（只读，低风险）——默认 yes、可跳过（轻确认）。
   - 永不自动批准原生 permission；PermissionCard 只 View。

**8. 中途 Firefly 重启怎么办？**
→ 9D：workflow 标 `interrupted`，用户手动决定（重新开始 / 从某步继续 / 放弃）。9E：持久化到 `config/workflows.json`，恢复后从失败/中断步重跑（需再确认）。

**原型边界（9D 明确不做）：** 不做自动编排（Level 3 全自动）；不做失败重试；不做第二 workflow；不启用 ChatGPT。

---

## 18. Cost Controls

已知成本事实（§2.3）：Claude=deepseek-v4-flash（廉价）；Codex=gpt-5.6-sol + priority tier（昂贵）。

规则：
1. **`prefer_low_cost`**：作为偏好字段（可放 `SettingsManager`，或按次传入 TaskRequest），推荐时倾斜 Claude。
2. **`manual agent override`**：`TaskRequest.requested_agent` 永远优先（用户说了算）。
3. **step 数预估必须在启动前展示**：Router 在推荐里带 `estimated_steps`（§6）；workflow 确认时 UI 显示 "This workflow will call Claude ×2 (flash) + Codex ×1."。
4. **可跳过步骤**：review 步默认可跳过（省一次 Claude 调用）。
5. **不做多 Agent 自动重试**：失败即停（§14），杜绝"自动换 Agent"造成的成本放大。
6. **每条 workflow 有 step 上限**（第一版 ≤3 步，硬编码在 plan 类型定义里），超过不自动扩展。

---

## 19. Risks

| 风险 | 影响 | 缓解 |
|---|---|---|
| 错误路由（把写任务推给只读 Claude，或把分析任务推给 Codex） | 用户困惑 / 浪费调用 | 确定性规则 + 显式确认门；`requested_agent` 优先；9A 起全量规则表可单测 |
| Agent 能力不对称被掩盖 | 推荐 Codex 结果无托管路径 | 注册表如实编码 `managed_exec=False`（§5）；9A 测试断言不对称 |
| 跨 workspace session 污染 | 一个项目的问题会话串到另一个项目 | 沿用 `workspace_key(agent, workspace)` 隔离；workflow 记录绑定 workspace |
| permission boundary 被突破 | 安全风险 | §13 写死：Router/Coordinator 永不批准；`requires_confirmation`；PermissionCard 只 View；不加 `--skip-git-repo-check` |
| secret leakage（transcript-first 把 key 传给下个 agent） | 严重泄露 | artifact-first（§12）；Coordinator 断言只传 artifact 路径 + 结构化摘要，`validate_safe` 式检查 |
| transcript explosion（全量复制上下文） | token / 内存爆炸 | artifact 有界；prompt 只含路径引用 + 原始用户任务；`estimated_steps` 前置展示 |
| workflow crash recovery（中途重启丢状态） | 用户重来 | 9E：workflows.json 元数据持久化 + paused 恢复；artifact 保活 |
| Agent process disappearance（agent 进程消失） | workflow 卡死 | 步骤超时（第一版：固定每步 timeout，超时标 error）；Codex 双信号确认 |
| user manually changes files mid-workflow | review 步基于过期状态 | changed_files 快照记录时间戳；确认门提示"检测到文件变更于 Codex 完成后" |
| multi-agent state race（两个 agent 同时写） | 状态互相覆盖 | 一次只有一个 running step（Coordinator 单步串行，天然无并发）；keep-awake 聚合沿用 |
| token/cost explosion（自动多 Agent 调用） | 费用失控 | §18：prefer_low_cost、step 预估、可跳过、无自动重试、step 上限 |

---

## 20. Phase 9 Implementation Plan

**可回滚的小阶段（顺序由审计决定），每一阶段独立可验收：**

| 阶段 | 内容 | 验收 | 涉及新文件（概念） |
|---|---|---|---|
| **9A** | Capability registry + TaskRequest + AgentRecommendation + 确定性 Router（纯 core，无 UI、无 workflow、无 LLM、无在线调用） | 规则表全量单测；能力不对称断言；不 import Qt | `core/agent_capabilities.py`, `core/task_request.py`, `core/agent_recommendation.py`, `core/agent_router.py`, `tools/test_phase9a_router.py` |
| **9B** | Task Recommendation Card（Light Glass，复用 show_recommendation 心智；OverlayCoordinator 管 z-order；挂起不取消） | 视觉验收（Light Glass/低密度）；触发/确认/关闭全路径；无大窗口 | `ui/recommendation_card.py` + app/coordinator 接线 |
| **9C** | 用户确认交接（recommend → confirm → 启动单一 agent；托管路径优先，native 路径走 lifecycle 观测） | 交接只做推荐卡确认后的启动，无自动编排；Codex 仍可关闭 | app 层薄接线（无新 core） |
| **9D** | 第一条 workflow `plan_implement_review` + WorkflowCoordinator + WorkflowEvent + artifact 层 + git_reader | Claude 托管 Plan/Review 在线验证；Codex 实施步生命周期+确认双信号；中断标 paused | `core/workflow_plan.py`, `core/workflow_events.py`, `core/workflow_coordinator.py`, `core/artifact_store.py`, `core/git_reader.py`, `ui/workflow_progress_card.py` |
| **9E** | workflow 持久化/恢复（config/workflows.json + paused 恢复 + 每步重确认） | 跨重启恢复 smoke；坏文件降级；无 secret 存盘 | `core/workflow_store.py` |

**9A 具体范围（本轮不实现）：** 只建能力注册表 + 三个纯模型 + 确定性 Router，配纯 Python 测试。UI、workflow、LLM、持久化全部在 9A 之外。

---

## 21. Explicit Non-Goals

本轮（Phase 9 Preflight）明确不做：

- 不实现 Agent Router（不写任何生产代码）。
- 不修改任何生产代码（core/、ui/、app.py、state_broker.py、tools/）。
- 不修改 Hooks（`~/.claude/settings.json`、`~/.codex/hooks.json`）、Claude/Codex configs、runtime、sessions.json、workspace settings、permissions、approval、sandbox、trust。
- 不做 Codex 在线调用；Claude 在线调用也不需要。
- 不调用 LLM 做路由。
- 不自动发起任何 Agent / 不自动跨 Agent 交接 / 不自动编排 workflow。
- 不自动批准权限；不新增 approval 系统。
- 不做 dashboard；不恢复大型 Companion Panel。
- 不建 AgentManager facade。
- 不实现 workflow 持久化、artifact 层、git_reader。
- 不为 ChatGPT 伪造能力。

---

## 22. Final Answers

1. **现在是否 ready 做 Router？** 部分 ready。**确定性推荐层（9A）ready**；**自动编排（Level 3）不 ready**——缺 Codex 托管执行（在线 0）、workflow 完成检测、artifact 层。
2. **Router 第一版调用 LLM 吗？** 不。确定性规则，可解释、可单测、零在线成本。
3. **AgentManager 必要吗？** 否。会话/执行/观测各有 owner；唯一新增是能力注册表，不建空 facade。
4. **WorkflowCoordinator 必要吗？** 是，但只在 **9D** 实现；9A/9B 不需要。Qt-free，职责=转移/确认门/交接/聚合/失败。
5. **AgentEvent 需要修改吗？** 不需要，语义保持"单 agent 单 turn"。
6. **需要 WorkflowEvent 吗？** 需要，独立新类型（§11），由 Coordinator 翻译，不污染 AgentEvent。
7. **第一条 multi-agent workflow？** `plan_implement_review`：Claude Plan → Codex Implement → Claude Review。
8. **自动运行 Claude→Codex→Claude？** 不。第一版每步用户确认；Level 1 起步，不自动编排。
9. **用户必须确认的节点？** workflow 启动、写/高风险步（Codex implement）、每步交接；review 步轻确认可跳过；原生 permission 永远由原生表面处理，Firefly 只 View。
10. **最安全的 context handoff？** Artifact-first：artifact 路径 + 结构化摘要 + changed-files 清单 + 原始用户任务；禁止全 transcript / env / secret。
11. **Phase 9A 具体做什么？** 能力注册表 + TaskRequest + AgentRecommendation + 确定性 Router（纯 core、无 UI、无 workflow、无 LLM、可单测）。
12. **哪些功能现在明确不做？** 自动编排、自动交接、自动批准、LLM 路由、dashboard、AgentManager facade、workflow 持久化/artifact 层、ChatGPT 后端、任何在线调用。

---

*本文件仅架构设计与审计结论。除本文件外，本轮未新增或修改任何文件。*

---

# Phase 9A Outcome

Status: **DONE**. Capability registry + deterministic Agent Router (pure core, zero UI, zero workflow, zero LLM, zero online calls). Appended 2026-08-15.

## 1. New Models

Two new core modules (Qt-free, stdlib-only, no side effects):

- **`core/routing_models.py`** — `TaskIntent` (chat/explain/analyze/code/review/debug/vision/unknown), `HandoffMode` (SHORT_TALK / OPEN_NATIVE / UNAVAILABLE), `Confidence` (high/medium/low), `ReasonCode` (stable machine-readable tokens only — core never emits display prose), `AgentCapability` (15 members; VISION deliberately unassigned), `TaskRequest`, `AgentProfile`, `CapabilityRegistry` (+ `DEFAULT_CAPABILITY_REGISTRY`), `AgentRecommendation`. All frozen dataclasses / str-Enums.
- **`core/agent_router.py`** — `AgentRouter.recommend(request) -> list[AgentRecommendation]`, pure deterministic rule engine. Injects `CapabilityRegistry` for testing.

`TaskRequest` is the prompt-specified minimal shape: `text`, `workspace`, `requested_agent`, `intent`, `requires_write`, `prefer_low_cost`. No conversation, session id, key, provider state, QWidget, or AgentEvent. `AgentEvent` was not modified. `classify_short_ask` was not modified (its job stays "is this Short Talk-safe"; the Router is a separate core engine).

## 2. Capability Registry — Current Truth (asymmetric, audited)

| | claude | codex | chatgpt |
|---|---|---|---|
| `managed_short_talk` | ✅ online-verified | ❌ | ❌ |
| `available` (task-execution mode) | ✅ | ✅ | ❌ (dock/URL entry only) |
| routed strengths | CHAT/ANALYSIS/EXPLANATION/REVIEW (+ FILE_READ, SESSION_RESUME, NATIVE_LAUNCH, LIFECYCLE_MONITOR) | CODING/DEBUGGING/FILE_READ/FILE_WRITE/TERMINAL/LONG_TASK (+ SESSION_RESUME, NATIVE_LAUNCH, LIFECYCLE_MONITOR) | NATIVE_LAUNCH only |
| notes | managed Short Talk is read-only (`--safe-mode` + plan mode); long/write tasks go native | no managed Short Talk yet; thread resume structurally supported but not online-verified | no managed backend; never fabricate direct chat/task execution |
| VISION | ❌ (no verified backend) | ❌ | ❌ |

`available` is static truth ("can Firefly execute a task through this agent right now"), distinct from live `working/waiting/error` state which stays with `StateMonitor`.

## 3. Router Rule Priority (highest first)

1. explicit `requested_agent` (field, or "用/让/交给/打开/open/use <agent>" phrasing)
2. vision intent → honest unavailable (no managed route; never fabricate)
3. write / coding → Codex, OPEN_NATIVE
4. review → Claude, SHORT_TALK
5. explain-error / debug → Claude (analysis direction), SHORT_TALK
6. analysis / explain / summarize / chat → Claude, SHORT_TALK
7. unknown → Claude, SHORT_TALK (safe fallback, LOW)

Scoring is a small readable tier table (requested=100, coding=90, review/error-explain=82, analysis=80, chat=70, unknown=60, vision=40, multi-step 85/80), so higher-priority rules always outrank lower ones and the returned list is naturally sorted.

## 4. Coding → Codex

Write markers (`修改/实现/重构/新增/删除/编辑/修复/写/生成/解决/fix/implement/refactor/…`) with no read framing → **Codex, OPEN_NATIVE, requires_confirmation=True**, reason `BEST_FOR_CODING` (intent `DEBUG` when an error marker is present). Codex stays OPEN_NATIVE because managed Short Talk is not product-enabled.

## 5. Analysis / Review → Claude

- analysis/explain/summarize → **Claude, SHORT_TALK**, reason `BEST_FOR_ANALYSIS`.
- review (`检查/审查/评审/review/check/…`) → **Claude, SHORT_TALK**, reason `BEST_FOR_REVIEW`. Short reviews stay SHORT_TALK; the doc's "large content → native" upgrade is left to a later phase.

## 6. Debug Boundary

- `为什么这里报错` (why + error, no write) → **Claude, SHORT_TALK**, intent `DEBUG` (analysis direction).
- `修复这个报错` / `Fix this error` (write + error) → **Codex, OPEN_NATIVE**, intent `DEBUG`.
- `怎么/如何…修复` (how-to phrasing) → **Claude, EXPLAIN** — a question, not a command.

## 7. requested_agent

Field or detected phrasing wins and is capability-checked honestly:

- requested Claude + read → Claude SHORT_TALK (`USER_REQUESTED_AGENT`).
- requested Claude + write → Claude OPEN_NATIVE, confirm (`NATIVE_SURFACE_REQUIRED` — managed is read-only).
- requested Codex → Codex OPEN_NATIVE (no managed path), confirm only on write.
- requested ChatGPT + real task → ChatGPT **UNAVAILABLE** (`BACKEND_UNAVAILABLE`) — never a fabricated success.
- requested ChatGPT / Claude / Codex + `打开/open` → OPEN_NATIVE (`OPEN_REQUESTED`) — the Router only recommends; it never launches.

## 8. Vision

No verified vision backend in the runtime, so **no profile claims VISION**. A vision prompt (`截图/图片/照片/screenshot/image/…`) → intent `VISION`, **UNAVAILABLE**, LOW confidence, reason `VISION_MANAGED_UNAVAILABLE`. The Router names a route, never a capability.

## 9. Handoff Modes

`SHORT_TALK` (Firefly managed read-only ask) / `OPEN_NATIVE` (native surface) / `UNAVAILABLE` (no route). The same agent carries different modes by task — e.g. Claude analysis = SHORT_TALK, Claude write = OPEN_NATIVE — so a recommendation always states *how* the agent would be reached.

## 10. Determinism Guarantee

`recommend` is a pure function of the request + registry: only deterministic string matching over fixed keyword tables, `re`-based normalization, and frozen dataclass output. No time, random, network, subprocess, Qt, or process launch. Same input → identical output (asserted by test).

## 11. Files

- Added: `core/routing_models.py`, `core/agent_router.py`, `tools/test_phase9a_agent_router.py`.
- Modified: `docs/PHASE9_AGENT_ROUTER_ARCHITECTURE.md` (this append only).
- Not touched: `app.py`, `state_broker.py`, `core/state_monitor.py`, all `ui/`, `core/agent_events.py`, Hooks, `runtime/`, `config/`, permissions/approval/sandbox/trust, all launch scripts, `requirements.txt`.

## 12. Tests

`tools/test_phase9a_agent_router.py` — **42 tests** covering all 30 required scenarios plus English prompts, how-to-fix boundary, open-agent phrasing, plain-mention non-hijack, injected registry, and never-empty/no-crash. All pass.

## 13. Regression

All prior test suites pass (run via `.venv\Scripts\python.exe`): state_broker, phase7a_core, phase7b_core, state_monitor, phase8a1_visual_shell, phase8a3_workspace, phase8b1_sessions, phase8b2_permission, phase8b3_notifications_keepawake, phase8b4_settings, phase8c1_short_ask, phase8c2_agent_events, phase8c3_short_talk_ux, phase8c4_session_persistence, single_instance. (`test_phase7a_core` / `test_phase8a1_visual_shell` need the project root on `PYTHONPATH` — a pre-existing convention, unrelated to 9A.)

## 14. Online Calls

- Claude online calls: **0**
- Codex online calls: **0**

## 15. UI

**No UI files were modified.** Zero recommendation card; `ShortAskPanel`/`AgentDock`/`OverlayCoordinator`/`app.py` untouched.

## 16. Phase 9B Suggestions

1. Wire a small `AgentRouter` into `app.py`'s Short Talk path *only as a pure advisor*: before `classify_short_ask` decides simple/complex, let the Router's reason/handoff inform the existing `show_recommendation()` UX (Claude analysis stays SHORT_TALK; coding steers to Codex OPEN_NATIVE; ChatGPT/vision stay UNAVAILABLE).
2. Keep the Router and `classify_short_ask` separate (two responsibilities). A later phase may let the old classifier delegate to the Router — not 9B.
3. If cost preference becomes real, `prefer_low_cost` is already carried in `TaskRequest`; the 9A rules don't branch on it (rules already prefer Claude for non-write), so no pricing table was added.
4. No live-state awareness in 9A by design; if 9B needs "is the agent busy", feed it as a separate runtime context input rather than mutating the registry.

---

# Phase 9B Outcome

Status: **DONE**. The deterministic AgentRouter is wired into the existing Firefly Short Talk user path as a pure advisor. Router still **Recommends only** — it never launches anything, never auto-executes, and no workflow was built. Appended 2026-08-15.

## 1. Router Integration Point

`app.py` `_on_short_ask_send` now builds a `TaskRequest` and calls `AgentRouter.recommend()` before any execution:

```text
user prompt
   → TaskRequest(text, workspace)          # requested_agent deliberately NOT set from dock
   → AgentRouter.recommend()
   → top.handoff_mode
        SHORT_TALK   → _short_talk_claude(prompt)   # classify_short_ask gate + existing Short Talk
        OPEN_NATIVE  → RecommendationCard (user click required)
        UNAVAILABLE  → RecommendationCard (capability state, no error)
```

The Router was **not** placed in any adapter, `SessionManager`, `StateMonitor`, or `ProcessLauncher` transport. It is an application-level decision layer owned by the composition root.

## 2. selected_agent vs requested_agent

`VisualShell._task_request(prompt)` builds `TaskRequest(text, workspace)` with `requested_agent=None`. The dock's current selection is UI context only and is never written into `requested_agent`, so the Router keeps its recommendation power. Textual intent ("用 Claude", "让 Codex 做") is still detected by the Router and wins.

## 3. Claude SHORT_TALK Path

High-confidence Claude SHORT_TALK (`解释`/`分析`/`总结`/`review`/`为什么报错`/`如何修复`) passes straight into the existing Short Talk with **zero extra clicks** — no recommendation card. The panel title already reads "Ask Claude"; no additional header change was needed. `classify_short_ask` still runs as the second gate (Router decided *who*, classifier decides *how lightweight*).

## 4. Coding Recommendation Path

Write/coding tasks (`修改/实现/重构/写测试/修 bug/新增模块/改 UI`, `fix/refactor/implement/…`) route to the new `ui/recommendation_card.py`:

```text
┌──────────────────────────────┐
│ Coding task              ×   │
│ Recommended · Codex          │
│          Ask Claude first [Open Codex] │
└──────────────────────────────┘
```

Coding never enters the Claude Short Talk backend.

## 5. Ask Claude first

The card's secondary action re-enters the Claude Short Talk path with the **original prompt unchanged** (`app.py._on_recommendation_ask_claude` → `_short_talk_claude`). It still runs `classify_short_ask`: a complex prompt shows the existing long-task UX. No prompt transformation is performed in 9B.

## 6. Open Codex / Open Claude

The card's primary action calls `ProcessLauncher.launch_agent(agent_id, workspace)` using the agent **and workspace locked at recommendation creation time**. It only opens the native surface — no prompt injection, no auto-Enter, no task file, no auto-start. Label says "Open Codex" / "Open Claude", never "Send to / Run with".

## 7. ChatGPT / Vision UX

- ChatGPT (requested or dock) → UNAVAILABLE card: "Not available yet / Direct task routing isn't available for this agent yet." with an optional **Open ChatGPT** (existing `ProcessLauncher.open_chatgpt()`). No fake managed backend.
- Vision (`分析这张图片/截图/photo/…`) → UNAVAILABLE card: "Vision isn't available here yet", no action. Never sent to the Claude chain; no vision capability claimed.

## 8. Recommendation UI Implementation

A **new independent `ui/recommendation_card.py`** (a `PopoverBase`) rather than overloading `ShortAskPanel`. Justification: `ShortAskPanel` is the streaming-turn surface (its `ShortTalkState` machine is turn-coupled); a pre-turn routing decision has a different lifecycle, different actions (Open native / Ask Claude first), and different suspend semantics (hidden-pending-restore vs running-turn-resume). The card holds pending `(prompt, agent, workspace)` in memory only — never on disk, never restored across restart.

ReasonCode → copy mapping lives in the UI layer (`REASON_TITLE`), keeping core free of display prose.

## 9. Overlay Priority

`OverlayCoordinator` gained `show_recommendation()` / `suspend_recommendation()`:

- Recommendation shows → hides Greeting, closes idle business popovers, suspends Short Ask.
- PermissionCard / business popovers show → card is hidden, pending kept in memory.
- PermissionCard clears → **no auto-repop**. User clicks Ask → pending recommendation is restored.
- Clicking away from the card declines it (pending dropped).

## 10. Workspace / Agent Lock

- Agent: the card stores the recommended agent at creation; a dock change mid-card does not change who is launched.
- Workspace: the card stores the workspace at creation; `ProcessLauncher` is given that value, never a re-read of `current()`.
- Workspace switch clears the pending recommendation (`app.py._on_workspace_changed` → `clear_pending()`) rather than silently handing the task to the new workspace.

## 11. Router Telemetry

**None added.** The lightweight fields would be pure instrumentation with no near-term consumer; the spec allows skipping when there is no clear value.

## 12/13. Online Calls

- Claude online calls: **0**
- Codex online calls: **0**

Every test patches `QuickAskRunner.ask` and `ProcessLauncher.launch_agent`.

## 14. Files

- Added: `ui/recommendation_card.py`, `tools/test_phase9b_recommendation_ux.py`.
- Modified: `app.py` (router wiring, `_short_talk_claude`, recommendation handlers, workspace-clear hook), `ui/overlay_coordinator.py` (card param + priority hooks), `tools/test_phase8c1_short_ask.py` + `tools/test_phase8c3_short_talk_ux.py` (the long-task prompt "fix this bug in main.py" now legitimately routes to the Codex card by 9B design; the long-task UX tests now use "explain how to update this module", which stays Claude SHORT_TALK + classify-complex).
- Not touched (protected): `state_broker.py`, `core/state_monitor.py`, `AgentEvent` semantics, all Hooks, `~/.claude`, `~/.codex`, `run_cli.ps1`, animations, Workspace schema, Settings schema, start/stop scripts, `requirements.txt`.

## 15. Tests

`tools/test_phase9b_recommendation_ux.py` — **40 tests** covering all 40 required scenarios, including the three desktop-smoke states (A/B/C) asserted offscreen with no online calls.

## 16. Regression

All prior suites still pass (via `.venv\Scripts\python.exe`; `test_phase7a_core` / `test_phase8a1_visual_shell` with project root on `PYTHONPATH`, the documented pre-existing convention): state_broker, phase7a_core, phase7b_core, state_monitor, phase8a1_visual_shell, phase8a3_workspace, phase8b1_sessions, phase8b2_permission, phase8b3_notifications_keepawake, phase8b4_settings, phase8c1_short_ask, phase8c2_agent_events, phase8c3_short_talk_ux, phase8c4_session_persistence, phase9a_agent_router, phase9b_recommendation_ux, single_instance.

## 17. Phase 9C Suggestions

The natural next step is **user-confirmed handoff**: after `[Open Codex]`, 9C would turn the single confirmation into a real handoff (inject the prompt into the launched Codex native surface, or run the managed path once verified). The RecommendationCard action handlers in `app.py` are the single chokepoint to upgrade. Codex managed Short Talk remains offline (0 online calls) — 9C must keep the native-open fallback as the honest default until the managed Codex exec path is online-verified.

---

## 18. Phase 9C Outcome — User-Confirmed Native Handoff

Status: **IMPLEMENTED**. Date: 2026-08-15. Scope: user-confirmed handoff only — no automatic multi-agent workflows, no Claude→Codex automation, no WorkflowCoordinator, no auto-review, no auto permission approval.

### 18.1 Handoff transport

The recommendation card's primary action for a verified agent (Codex/Claude) is now **"Send to &lt;agent&gt;"**. On click it creates a `HandoffRequest` and launches the **real native executable directly via a QProcess argument vector**:

- `codex &lt;prompt&gt;` → `node.exe <npm>/node_modules/@openai/codex/bin/codex.js <prompt>` in the locked workspace
- `claude &lt;prompt&gt;` → `<npm>/node_modules/@anthropic-ai/claude-code/bin/claude.exe <prompt>` in the locked workspace

**Why direct native argv, not the existing PowerShell wrapper:** empirical audit on this host (claude 2.1.233 / codex-cli 0.147.0) proved that every intermediary that re-serializes the command line corrupts embedded double quotes / newlines in a user prompt:

- PowerShell 5.1 `-File` + npm `.ps1` shim → native: **corrupts** embedded `"` (10/12 preserved)
- npm `.CMD` shim `%*` → cmd.exe → native: **corrupts** embedded `"` and newlines (20/26 preserved)
- `wt.exe` re-parse: **corrupts** embedded `"` (9/12 preserved)
- **Direct QProcess → native executable: 24/24 preserved** — the only lossless transport

`run_cli.ps1` and the PowerShell wrapper remain untouched and are still used for **Open only** (no prompt, so the corruption is irrelevant). The wrapper was **not** modified (protected scope).

### 18.2 Security boundary

- **No shell, ever**: handoff argv is built as a Python list; `QProcess.startDetached(program, argv, workspace)` receives `program` and a list — no shell string, no `shell=True`, no `cmd /c`, no `Invoke-Expression`.
- **Injection samples verified** (real QProcess → node/python stand-ins): `hello "world"`, single quotes, newline, `; Write-Output HACKED; #`, `$(Write-Output HACKED)`, `& calc.exe`, `| Remove-Item test`, backticks, `&&`/`||`, `<`/`>`, Unicode Chinese, emoji, Windows paths — all arrive as **one literal argv item**.
- **Prompt visible in the local process command line** (inherent to the positional-prompt contract) — documented limitation.
- **Prompt guard** (`core/handoff.validate_handoff_prompt`): a prompt starting with `-` is refused (would be parsed as a CLI flag, e.g. `--dangerously-skip-permissions`); a prompt exactly equal to a CLI subcommand (e.g. `exec`) is refused. Both return an "Open only" message.
- **BLOCKED fallback**: if the npm shim cannot be resolved to a real native target, the handoff refuses (keeps Open only) rather than risk corrupting the prompt.

### 18.3 Native launch semantics

- `HANDOFF SENT ≠ AGENT TASK SUCCESS`. `HandoffState` has **no** success/completed state: `PENDING → LAUNCHING → HANDED_OFF | FAILED | CANCELLED`. `HANDED_OFF` means the native process started and the payload was handed over. The card shows **"Sent to Codex"** then fades after ~1.5 s; real task completion stays with external hooks / StateMonitor.
- `launch failure → FAILED` ("Couldn't start Codex"), never an Agent-error lifecycle, and never a `runtime/sources/*.json` write.
- Handoff creates **no** managed SessionManager session and **never** fabricates lifecycle state.
- **PermissionCard stays observer-only**: `View` only; Firefly never becomes the approval authority; native Codex/Claude keep their own permissions/sandbox/approval/session UI.
- Native handoff runs in a plain native console window (no Windows Terminal tab — wt.exe's re-parse corrupts quotes).

### 18.4 Known limitations

1. Prompt is visible in the local process command line.
2. Handoff opens a native console window rather than a Windows Terminal tab.
3. npm-shim installs are resolved by the standard npm `node_modules` layout; exotic installs fall back to Open only.
4. Interactive "first message auto-sent" semantics are per the CLI's documented positional-prompt contract; no online smoke was burned (0 Codex / 0 Claude online calls in 9C).
5. A prompt that is exactly a CLI subcommand word, or that starts with `-`, is refused via Open only.

### 18.5 Files

- Added: `core/handoff.py` (`HandoffState`, `HandoffRequest`, `make_handoff_request`, `validate_handoff_prompt`, `HandoffTelemetry`/`HandoffMetrics`), `tools/test_phase9c_native_handoff.py` (**50 tests**), `tools/smoke_phase9c_native_handoff.py`.
- Modified: `ui/process_launcher.py` (`launch_agent(agent, workspace, initial_prompt=None)`, `_handoff_command`, `_resolve_native`, `_spawn_native`), `ui/recommendation_card.py` (Send mode + light "Ask Claude first" + handoff state machine), `app.py` (send handler, workspace-drift guard, `HandoffMetrics`), `tools/test_phase9b_recommendation_ux.py` (copy updated to Send/Open/Ask; **42 tests**).
- Not touched (protected): `state_broker.py`, `core/state_monitor.py`, `AgentEvent` semantics, all Hooks, `~/.claude`, `~/.codex`, permissions/approval/sandbox, animations, `run_cli.ps1`, Workspace/Settings schema, start/stop scripts, `requirements.txt`.

### 18.6 Phase 9D Suggestions

9D (whenever attempted) should build on the now-proven **user-confirmed single handoff** and NOT auto-chain agents. Suggested order: (1) define a separate `WorkflowEvent` (not AgentEvent) for workflow-level progress; (2) a `WorkflowCoordinator` that treats each user-confirmed handoff as one step; (3) artifact-first handoff (`runtime/artifacts/<workflow_id>/`) as the cross-agent payload, still behind explicit user confirmation per step. The Codex managed Short Talk path remains offline (0 online calls) — keep the native handoff as the honest default until `codex exec` is online-verified.

---

## 19. Phase 9D.1 Outcome — Workflow Core + Confirmation Gates

Status: **DONE**. First real Workflow Core: Qt-free, provider-free models + a stateful coordinator that safely advances *logical* workflow state. It decides the next step, whether a step may start, which artifacts a step needs, and how success/failure/cancel transitions the workflow — but it **never launches Claude/Codex, never auto-chains agents, touches no UI, and writes nothing**. Appended 2026-08-15.

### 19.1 Workflow model

`core/workflow_models.py` (new) — frozen dataclasses + str-Enums, stdlib only, no Qt / subprocess / network / lifecycle / agent-event imports:

- `WorkflowKind` — exactly one: `PLAN_IMPLEMENT_REVIEW`.
- `WorkflowState` — `CREATED / RUNNING / WAITING_FOR_USER / SUCCEEDED / FAILED / CANCELLED`. `SUCCEEDED` requires every step to have met the coordinator's success conditions; it never means "a process was spawned".
- `WorkflowStepState` — `PENDING / AWAITING_CONFIRMATION / READY / RUNNING / SUCCEEDED / FAILED / CANCELLED / SKIPPED`.
- `WorkflowStepIntent` — `PLAN / IMPLEMENT / REVIEW` (deliberately distinct from routing `TaskIntent`).
- `ArtifactKind` — `PLAN / IMPLEMENTATION_SUMMARY / CHANGED_FILES / REVIEW` (fixed, small).
- `ArtifactRef` — `artifact_id / kind / producer_step_id / path / created_at / metadata`; a **reference contract only** — no file is created and no ArtifactStore exists yet.
- `CompletionSource` — `MANAGED_AGENT_RESULT / USER_CONFIRMED / ARTIFACT_VALIDATED`. **Deliberately no "external lifecycle" / observer member.**
- `StepCompletionEvidence` — `source / summary / metadata`; the record of *why* the coordinator accepted a step as succeeded.
- `WorkflowPlan` — `workflow_id / kind / original_request / workspace / steps / state / current_step_index / created_at / updated_at`. Holds the original `TaskRequest` (`original_request.text` is the user task); **never** a transcript, session id, or API credential.
- `WorkflowStep` — `step_id / agent_id / intent / handoff_mode / requires_confirmation / state / created_at / updated_at / title (short token) / expected_artifacts / required_artifacts / completion_sources / attached_artifacts`.
- `WorkflowEventType` + `WorkflowEvent` — application-level lifecycle events; never provider raw JSON, display language, session ids, or secrets.

### 19.2 Immutability

Option **A** (spec §34): every coordinator transition returns a **new `WorkflowPlan`** (`dataclasses.replace` under the hood) and registers it under its `workflow_id`. Steps are immutable; the coordinator owns the current instance. Tests assert state semantics directly.

### 19.3 PLAN_IMPLEMENT_REVIEW (fixed template)

| Step | agent | intent | handoff | requires_confirmation | expected artifacts | required artifacts |
|---|---|---|---|---|---|---|
| 1 | Claude | PLAN | SHORT_TALK | ✅ | PLAN | — |
| 2 | Codex | IMPLEMENT | OPEN_NATIVE | ✅ | IMPLEMENTATION_SUMMARY, CHANGED_FILES | PLAN |
| 3 | Claude | REVIEW | SHORT_TALK | ✅ | REVIEW | PLAN, CHANGED_FILES |

- Step 1 starts `AWAITING_CONFIRMATION`; steps 2–3 start `PENDING`; workflow starts `WAITING_FOR_USER`.
- Step N+1 advances to `AWAITING_CONFIRMATION` only after step N `SUCCEEDED` **and** N+1's `required_artifacts` are attached.
- No general DAG engine, no cycles, no parallel steps — strict linear order only.

### 19.4 Confirmation gate

`confirm_step(plan, step_id)` moves `AWAITING_CONFIRMATION → READY` **only**. It never starts an agent — an executor layer (9D.2+) will consume READY. Every step in the template is user-confirmed (conservative first version); Codex IMPLEMENT is always confirmed.

### 19.5 Artifact gate

A step may be marked succeeded only when every `expected_artifacts` kind is attached (`attach_artifact`), with `producer_step_id` matching the step and the kind being one the step expects. E.g. Claude Plan **cannot** succeed without a PLAN artifact — "Claude says it's done" is never enough to hand Codex a plan that does not exist.

### 19.6 Completion evidence & the Codex rule

`mark_step_succeeded` requires a `StepCompletionEvidence` whose `source` is allowed for that step (`completion_sources`):

- Claude Plan/Review → all three sources (a future managed `Claude FINAL` may be `MANAGED_AGENT_RESULT`).
- **Codex IMPLEMENT → `{USER_CONFIRMED, ARTIFACT_VALIDATED}` only.** No managed Codex path is online-verified, so `MANAGED_AGENT_RESULT` is rejected, and there is **no** lifecycle/external source in the enum at all. **Why Codex completion cannot rely on the observer lifecycle:** Codex hooks have no failure signal — `Stop → success` fires even on tool-level failure (Preflight §2.3). External Stop/success is never an authoritative workflow result; the model structurally cannot express "the external lifecycle said success".

### 19.7 Failure / cancel

- **STOP_ON_FAILURE**: `mark_step_failed` (RUNNING → FAILED) → workflow FAILED, later PENDING steps SKIPPED. No auto-retry, no fallback agent, no continuation. The coordinator exposes no retry surface.
- **Cancel**: `cancel_workflow` from a non-terminal state → current step (PENDING/AWAITING_CONFIRMATION/READY/RUNNING) CANCELLED, later PENDING steps SKIPPED, workflow CANCELLED. No process is killed — an executor owns real cancel in a later phase.
- Terminal workflows (SUCCEEDED/FAILED/CANCELLED) reject all further transitions.

### 19.8 WorkflowEvent vs AgentEvent vs Handoff

- `AgentEvent` = one agent's turn (provider-neutral interaction); unchanged.
- `HandoffState` = the *transport* lifecycle (PENDING→LAUNCHING→HANDED_OFF|FAILED|CANCELLED); unchanged, no task-success state.
- `WorkflowEvent` = application-level workflow lifecycle (`WORKFLOW_CREATED / STEP_CONFIRMATION_REQUIRED / STEP_READY / STEP_STARTED / ARTIFACT_ATTACHED / STEP_SUCCEEDED / STEP_FAILED / STEP_CANCELLED / WORKFLOW_SUCCEEDED / WORKFLOW_FAILED / WORKFLOW_CANCELLED`).
- Future relation is `AgentEvent FINAL → executor/completion policy → WorkflowCoordinator → STEP_SUCCEEDED`, **not** `AgentEvent FINAL == step success`. This phase does not wire the two.
- Event order is deterministic (spec §35), asserted by test.

### 19.9 Coordinator API

`core/workflow_coordinator.py` (new): `WorkflowTransitionError` + `WorkflowCoordinator(registry=DEFAULT_CAPABILITY_REGISTRY, clock=injectable)`.

`create_plan_implement_review(request)` · `get_plan(workflow_id)` · `get_current_step(plan)` · `required_artifacts_satisfied(plan, step_id)` · `request_confirmation(plan)` · `confirm_step(plan, step_id)` · `mark_step_started(plan, step_id)` · `attach_artifact(plan, step_id, artifact)` · `mark_step_succeeded(plan, step_id, evidence)` · `mark_step_failed(plan, step_id, error_code)` · `cancel_workflow(plan)` · `connect(listener)` / `disconnect(listener)`.

Capability validation is static-only (`Claude ANALYSIS/REVIEW + managed Short Talk`, `Codex CODING + native launch`); live state is never read and a busy Codex never blocks building a workflow.

### 19.10 Files / tests / online

- Added: `core/workflow_models.py`, `core/workflow_coordinator.py`, `tools/test_phase9d1_workflow_core.py`.
- Modified: `docs/PHASE9_AGENT_ROUTER_ARCHITECTURE.md` (this append only).
- Not touched (protected): `app.py`, `state_broker.py`, `core/state_monitor.py`, `core/agent_events.py`, `core/handoff.py`, `core/agent_router.py`, `core/session_manager.py`, all `ui/`, Hooks, `~/.claude`, `~/.codex`, `runtime/`, `config/`, launch scripts, `requirements.txt`, permissions/approval/sandbox/trust.
- Tests: **60** (59 required scenarios + `request_confirmation`), all pass; no Qt, no subprocess, no network, no config/runtime writes.
- Claude online calls: **0**. Codex online calls: **0**.

### 19.11 Phase 9D.2 suggestions

1. **Executor layer** that consumes `READY` and drives one real user-confirmed handoff per step (9C's `ProcessLauncher.launch_agent(agent, workspace, initial_prompt=...)`), translating `AgentEvent FINAL`/errors into `mark_step_started` / `attach_artifact` / `mark_step_succeeded` / `mark_step_failed` per this coordinator's evidence policy.
2. **ArtifactStore** (`runtime/artifacts/<workflow_id>/`): persist PLAN / IMPLEMENTATION_SUMMARY / CHANGED_FILES / REVIEW refs so the coordinator's `ArtifactRef` paths become real files; keep artifact-first handoff (paths + bounded summaries, never transcripts).
3. **Read-only git enumeration** for CHANGED_FILES (with a non-git fallback); never `git status/diff` from the workflow core itself.
4. **Workflow UI**: a compact progress card consuming only `WorkflowEvent` (per Preflight §16), wired through `OverlayCoordinator` with the hide-not-cancel suspend policy.
5. Persistence (`config/workflows.json`, paused-resume) stays out until the workflow is genuinely runnable (9E).

### 19.12 Phase 9D.2 Outcome — Artifact Store + Claude Plan Executor

**Scope.** This phase executes exactly one real workflow step: **Step 1 (Claude Plan)** of `PLAN_IMPLEMENT_REVIEW`. Codex Implement and Claude Review are never executed; Step 2 only advances to `AWAITING_CONFIRMATION` for the user. Zero UI, zero workflow persistence, zero changed-files.

**ArtifactStore (`core/artifact_store.py`).** Qt-free, provider-free, owns only the safe on-disk layout under `runtime/artifacts/<workflow_id>/`:
- `plan.md` = the explicit artifact content; `artifacts.json` = a tiny metadata index (`artifact_id`, `kind`, `producer_step_id`, relative `path`, `created_at`, optional `size`/`sha256`). Never session ids, AgentEvents, transcripts, API keys, env.
- Atomic writes (temp + `os.replace`), UTF-8; `write_text` returns an `ArtifactRef` only after the file is fully on disk, so a half-written plan can never be attached.
- Path safety is two-layered: every workflow/step/kind identifier must match `^[A-Za-z0-9][A-Za-z0-9._-]*$`, and every final resolved path is re-checked to stay inside the artifact root. `../../`, `..\`, absolute Windows/UNC/POSIX paths are rejected (tested).
- API: `write_text` / `read_text` / `exists` / `resolve_path` / `list_artifacts` / `remove_workflow`. No database, no search, no version control. No automatic GC (restart may leave orphan dirs; `remove_workflow` exists for cleanup).

**Transient workflow session.** Each managed workflow step runs on a fresh Claude session that never reads or writes the ordinary Short Talk `SessionManager` and never touches `config/sessions.json`. Implemented with the existing `QuickAskRunner` (`session_manager=None` → private in-memory `SessionManager`, no store) + `persistent=False` (no `--resume`, `--no-session-persistence`) + `isolated=True` (`--safe-mode`). No second runner, no session-policy enum was required — the runner already supported it.

**Managed Claude Plan execution (`ui/workflow_executor.py`).** A thin execution layer; the `WorkflowCoordinator` stays Qt-free/provider-free (dependency direction: executor → coordinator, never reverse).
- `PlanStepExecutor.execute(plan, step_id)` validates workflow non-terminal, current step, `READY`, and supports **only** `agent=claude + intent=PLAN + handoff=SHORT_TALK`. Anything else (Codex IMPLEMENT, Claude REVIEW) raises `UnsupportedStepExecution`; a second concurrent run raises `ExecutionBusy`.
- Sequence: `mark_step_started` (READY→RUNNING) → transient Claude Plan call → consume existing `AgentEvent`s (ClaudeStreamAdapter via the runner) → full FINAL/text collected → `ArtifactStore.write_text` → `attach_artifact` → `mark_step_succeeded(MANAGED_AGENT_RESULT)` → Step 2 `AWAITING_CONFIRMATION` → workflow `WAITING_FOR_USER`. Execution then stops; Step 2 is never started.
- Success requires a usable agent result AND a written PLAN artifact. `AgentEvent.ERROR`, non-zero exit, or exit-0-with-no-usable-text all fail the step (`mark_step_failed`); no automatic retry, no fallback agent. Cancel maps to `cancel_workflow` (minimal 9D.2 scope).
- The full Claude output (not the UI 600-char truncated answer) is what lands in `plan.md`.

**Read-only boundary.** The local claude 2.1.233 CLI help confirms an enforceable per-invocation read-only contract: `--permission-mode plan` (read-only permission mode, listed in choices) + `--safe-mode` (hooks/customizations disabled). No global settings, hooks, permissions, or trust are modified; no `bypassPermissions`/`acceptEdits`. The online smoke ran Claude in a temp workspace and confirmed the workspace files were byte-identical before/after.

**Artifact-first evidence.** Step success is evidenced by `MANAGED_AGENT_RESULT` plus the attached PLAN artifact path — the plan file, not a transcript, is the retained record.

**Online.** Claude online calls: **1** (the smoke). Codex online calls: **0**. The smoke verified READY→RUNNING→Claude→PLAN artifact→SUCCEEDED→Codex AWAITING_CONFIRMATION, `config/sessions.json` unchanged, `runtime/sources/claude.json` (external lifecycle) untouched, workspace unchanged, clean process exit, and removed its own test artifact.

**Files / tests.**
- Added: `core/artifact_store.py`, `core/workflow_prompt.py`, `ui/workflow_executor.py`, `tools/test_phase9d2_artifact_store.py` (24 tests), `tools/test_phase9d2_plan_executor.py` (43 tests), `tools/smoke_phase9d2_plan_executor.py`.
- Modified: `docs/PHASE9_AGENT_ROUTER_ARCHITECTURE.md` (this append only).
- Not touched (protected): `app.py`, `state_broker.py`, `core/state_monitor.py`, `core/agent_events.py`, `core/workflow_models.py`, `core/handoff.py`, `core/agent_router.py`, `core/session_manager.py`, all other `ui/`, Hooks, `~/.claude`, `~/.codex`, `runtime/` sources, `config/`, launch scripts, `requirements.txt`, permissions/approval/sandbox/trust.
- Full regression (Phase 7/8/8C/9A/9B/9C/9D.1 + single-instance + state broker/monitor): all pass.

**Known limitations (unchanged from plan).** `WorkflowPlan` is memory-only (no `config/workflows.json`); a Firefly restart does not restore workflows. `CHANGED_FILES` and `IMPLEMENTATION_SUMMARY` are not implemented; Claude Review is not executed. Artifact orphans are not auto-collected. No workflow telemetry file is written — the transient runner's telemetry goes to the executor, which records nothing this phase, so workflow calls are never mislabeled as ordinary Quick Ask.

**Phase 9D.3 suggestions.** (1) Workflow UI: a compact progress card consuming only `WorkflowEvent` (hide-not-cancel suspend policy), wired through `OverlayCoordinator`. (2) Confirm Step 2 → Codex native handoff (9C transport) for IMPLEMENT, keeping `USER_CONFIRMED`/`ARTIFACT_VALIDATED` evidence. (3) Claude Review on the PLAN + CHANGED_FILES artifacts. (4) Optional `purpose` telemetry field (`short_talk` / `workflow_plan`) if workflow metrics are ever recorded. (5) Workflow persistence + orphan GC when workflows become genuinely runnable.

---

## 20. Phase 9D.3 Outcome — Workflow UX + Plan Step Integration

Status: **DONE**. The previously headless WorkflowCoordinator / ArtifactStore / PlanStepExecutor are now wired into the product UI. The user can execute **Claude Plan (Step 1)** through the real app and watch workflow state on a new WorkflowCard. Codex Implement and Claude Review are never executed; Step 2 deliberately stays at `AWAITING_CONFIRMATION`. Appended 2026-08-15.

### 20.1 Workflow entry

The Codex coding recommendation card (9B/9C) is restyled for 9D.3:

| Action | Behavior |
|---|---|
| **Send to Codex** (primary) | unchanged 9C user-confirmed native handoff (`ProcessLauncher.launch_agent(codex, ws, initial_prompt=...)`) — never creates a workflow |
| **Plan with Claude** (secondary) | creates + confirms Step 1 of `PLAN_IMPLEMENT_REVIEW` and starts the managed Claude Plan |
| **Open only** (light) | unchanged 9B native open — no task, no handoff, no workflow |

"Ask Claude first" is **removed** (no four-action card; users can still ask Claude via Short Talk). `Send to Codex` remains the direct path so coding tasks are never forced through the workflow.

### 20.2 Recommendation → Workflow entry

`RecommendationCard` gains `plan_with_claude_requested(prompt, workspace)` (a double-click-guarded signal; the card never clears itself for it — the app decides). `app.py._on_recommendation_plan_with_claude` is the single application-layer gate:

1. **Workspace drift** (`workspace != current()`) → pending recommendation cancelled, short "Workspace changed. Please resubmit." notice, **no workflow created**.
2. **Active workflow already exists** (non-terminal) → no second workflow; the WorkflowCard re-shows with "A workflow is already in progress." (the card *is* the View-workflow entry).
3. Otherwise → `TaskRequest(text=prompt, workspace=locked_workspace)` built from the recommendation's **locked** prompt + workspace (never a re-read of the input/dock/current workspace) → `create_plan_implement_review` → **`confirm_step` then `execute` as two separate calls** → card shown immediately → `PlanStepExecutor.execute`.

### 20.3 WorkflowCard (`ui/workflow_card.py`)

A new Light Glass `PopoverBase` (width 340 ≈ spec target 330–370 px), low density: three step rows (agent · title · short status · tiny state dot) plus a light status line. It is a **view**: it consumes only `WorkflowEvent` notifications and immutable `WorkflowPlan` snapshots fetched from the coordinator (`coordinator.get_plan`). It never imports provider stream-json, `AgentEvent`, `QuickAskRunner`, `ProcessLauncher`, or `json`.

- Status copy is UI-owned (`Preparing…` / `Planning…` / `Plan ready` / `Waiting for confirmation` / `Pending` / `Failed` / `Cancelled` / `Skipped` / `Working…`). `READY` at the current step renders "Preparing…"; `STEP_STARTED` → `Planning…`.
- Actions are only signals: **Open plan** (the app resolves an `ArtifactStore`-validated path) and **Cancel workflow** (the app routes to executor-or-coordinator). No plan editor, no Markdown viewer.
- **Hide ≠ cancel**: the close button / `dismiss()` / `suspend_workflow()` only hide the card — never cancel the workflow, executor, or artifacts. A hidden workflow keeps receiving events and is re-shown unchanged.
- No UUIDs, step ids, artifact ids, full paths, or internal tokens are rendered; the full plan is never shown (only "Plan ready" + Open plan).
- **Recovery**: if an active non-terminal workflow's card is hidden and the user clicks Firefly (pet click) with no higher-priority overlay, `OverlayCoordinator.toggle_bubble` re-shows the WorkflowCard. No new toolbar/dashboard/sidebar.

### 20.4 confirm vs execute separation

The coordinator's semantics are untouched (protected scope). The application layer keeps them separate: the Plan click calls `confirm_step` (AWAITING_CONFIRMATION → READY, never starts an agent), then `PlanStepExecutor.execute` (consumes READY → mark_step_started → transient Claude Plan). `confirm_step` still never launches an agent; the executor owns execution (9D.2 unchanged).

### 20.5 WorkflowEvent-driven UI

The app forwards every coordinator `WorkflowEvent` to `WorkflowCard.on_workflow_event`, which refreshes from the new plan snapshot. Only the eight workflow event kinds are consumed: `STEP_READY / STEP_STARTED / ARTIFACT_ATTACHED / STEP_SUCCEEDED / STEP_FAILED / STEP_CANCELLED / STEP_CONFIRMATION_REQUIRED / WORKFLOW_SUCCEEDED / WORKFLOW_FAILED / WORKFLOW_CANCELLED`. The card ignores events for workflows it does not track. The app also tracks `_active_workflow_id` (set on `WORKFLOW_CREATED`, cleared on terminal) for the one-active-workflow guard.

### 20.6 Plan artifact display / open

After Step 1 succeeds the card enables **Open plan**. The click emits `open_plan_requested(workflow_id)`; `app.py._on_workflow_open_plan` finds the `PLAN` artifact on Step 1, calls `ArtifactStore.resolve_path(ref)` (which re-validates containment in the artifact root) and only then `QDesktopServices.openUrl(QUrl.fromLocalFile(path))`. The UI never supplies an arbitrary path; a failed resolve or a refused OS open shows a short notice, never a crash.

### 20.7 Step 1 / Step 2 final states

- **Step 1 (Claude Plan)** ends `SUCCEEDED` with a real `plan.md` artifact written under the artifact root.
- **Step 2 (Codex Implement)** ends `AWAITING_CONFIRMATION`, `current_step_index == 1`, workflow `WAITING_FOR_USER`. It is never confirmed, never `mark_step_started`, never executed. The card shows "Waiting for confirmation" with **no** confirmation button (artifact transport is not implemented). Step 3 shows "Pending".
- Since the workflow deliberately stops at Step 2's confirmation, `SUCCEEDED` is normally unreachable — correct.

### 20.8 Overlay priority

The WorkflowCard sits at the interactive tier (same as ShortAsk/Recommendation). `OverlayCoordinator` gained `show_workflow()` / `suspend_workflow()` / `_position_workflow()`. Showing the card hides Greeting and closes idle ShortAsk/Recommendation (never cancels a running Short Ask). Business popovers and PermissionCard `suspend_workflow()` — hide only, never cancel. PermissionCard clearing does **not** auto-repop the card; the user restores it with a Firefly click.

### 20.9 Cancel behavior

- **Plan running**: `app._on_workflow_cancel` calls `PlanStepExecutor.stop()`; the real transport `CANCELLED` event completes `coordinator.cancel_workflow` (the executor owns the wait). No unrelated processes are touched.
- **Waiting at Step 2**: the app calls `coordinator.cancel_workflow` directly.
- Card button hidden on terminal states.

### 20.10 Isolation (unchanged from 9D.2)

The workflow Plan still runs `persistent=False` + `isolated=True` (`--safe-mode`) on a transient runner with its own in-memory SessionManager. `config/sessions.json` and `runtime/sources/claude.json` are untouched (verified online). The WorkflowCard never touches `ShortAskPanel`, never shows the Short Talk session badge, and the workflow never appears in Short Talk history. Firefly's character keeps the true external lifecycle — no workflow-specific animation was fabricated.

### 20.11 Files / tests / online

- Added: `ui/workflow_card.py`, `tools/test_phase9d3_workflow_ux.py` (**56 tests** + desktop smoke), `tools/smoke_phase9d3_workflow_ux.py`.
- Modified: `app.py` (workflow components + Plan-with-Claude / open / cancel handlers, active-workflow tracking, `artifact_root` test hook), `ui/recommendation_card.py` (Plan with Claude / Open only layout, `plan_with_claude_requested`, removed `ask_claude_requested`), `ui/overlay_coordinator.py` (workflow_card param + show/suspend/position + pet-click recovery), `tools/test_phase9b_recommendation_ux.py` (41 tests — new codex layout), `tools/test_phase9c_native_handoff.py` (50 tests — light Open only), `tools/smoke_phase9c_native_handoff.py` (new layout + Plan-with-Claude smoke).
- Not touched (protected): `state_broker.py`, `core/state_monitor.py`, `core/agent_events.py`, `core/handoff.py`, `core/agent_router.py`, `core/workflow_models.py`, `core/workflow_coordinator.py`, `core/session_manager.py`, `core/artifact_store.py`, `ui/workflow_executor.py`, Hooks, `~/.claude`, `~/.codex`, launch scripts, `requirements.txt`, permissions/approval/sandbox/trust.
- Full regression (22 suites: Phase 7/8/8C/9A/9B/9C/9D.1/9D.2/9D.3 + single-instance + state broker/monitor): **all pass**.
- **Online smoke**: Claude online calls **1**, Codex online calls **0**. Drove the real app layer (codex card → Plan with Claude → WorkflowCard → real Claude Plan → Step 1 SUCCEEDED → Step 2 AWAITING_CONFIRMATION). All 22 checks OK: current `build_plan_prompt()` used, plan.md in-root and non-empty, sessions.json + claude.json byte-identical, temp workspace unchanged, WorkflowCard shows Plan ready / Waiting for confirmation / Pending. (The model's plan body was low-quality — it asked for more context — which is the documented 9D.2 limitation; per spec §33 no re-run for wording.)

### 20.12 Known limitations (unchanged from plan)

`WorkflowPlan` stays memory-only (no `config/workflows.json`); a Firefly restart drops active workflows (artifacts may remain). `CHANGED_FILES` / `IMPLEMENTATION_SUMMARY` and Claude Review are not implemented. Step 2 has no confirmation action yet (artifact transport pending). No content-version detection for hand-edited `plan.md`. Artifact orphans are not auto-collected. No workflow telemetry file.

### 20.13 Phase 9D.4 suggestions

(1) Confirm Step 2 → Codex native handoff (9C transport) for IMPLEMENT with `USER_CONFIRMED`/`ARTIFACT_VALIDATED` evidence and `CHANGED_FILES`/`IMPLEMENTATION_SUMMARY` artifacts. (2) Claude Review on the PLAN + CHANGED_FILES artifacts. (3) Workflow persistence (`config/workflows.json`, paused-resume) once workflows are genuinely runnable. (4) Optional `purpose` telemetry field. (5) Orphan GC for `runtime/artifacts`.

---

## 21. Phase 9D.4 Outcome — Codex Implement Handoff + Completion Evidence

Status: **DONE**. Step 2 (Codex Implement) of `PLAN_IMPLEMENT_REVIEW` is now runnable: the user confirms **Implement with Codex**, Firefly hands the original task + PLAN artifact to a native Codex through the 9C single-argv transport, and — only after a second explicit **Implementation complete** — Firefly scans the workspace against the pre-handoff baseline and produces the `CHANGED_FILES` + `IMPLEMENTATION_SUMMARY` artifacts before accepting `USER_CONFIRMED` evidence. Claude Review is never executed. Appended 2026-08-15.

### 21.1 Implement executor (`ui/workflow_implement_executor.py`)

A new `ImplementStepExecutor` (a `QObject`), deliberately separate from `PlanStepExecutor` so neither becomes an `if/elif` monolith. It owns only `agent=codex + intent=IMPLEMENT + handoff=OPEN_NATIVE`; REVIEW is rejected. `execute(plan, step_id)` sequence:

1. Validate workflow non-terminal, current step, `READY`, supported shape, and a workspace.
2. `coordinator.mark_step_started` → Step 2 `RUNNING`.
3. `workspace_snapshot.capture(plan.workspace)` → **baseline** (must exist before any handoff; a scan failure here fails the step — nothing has been handed off yet).
4. Read the PLAN artifact (`ArtifactStore.read_text`, cross-step lookup — PLAN lives on Step 1).
5. `build_implement_prompt(original_request, plan_text)`.
6. `ProcessLauncher.launch_agent("codex", workspace, initial_prompt=prompt)` — the 9C transport.
7. Spawn **success** leaves Step 2 `RUNNING` (never success); spawn failure → `mark_step_failed("launch_failed")` → workflow `FAILED`, no artifacts, no retry.

The baseline snapshot lives in the executor's memory, not the card, so hiding the card never loses it.

### 21.2 Step 2 confirmation

- Step 2 starts `AWAITING_CONFIRMATION` (after Plan success), workflow `WAITING_FOR_USER`.
- The card's **Implement with Codex** button emits `implement_with_codex_requested`. `app._on_workflow_implement` keeps **confirm and execute separate**: `coordinator.confirm_step` (→ `READY`, never launches) then `implement_executor.execute`. A double click is a no-op (button disabled after the first click + executor busy gate).
- `confirm_step()` never starts Codex; the executor consumes `READY` independently (9D.1 separation preserved).

### 21.3 Codex input — artifact-first

Only two sources reach Codex: `original_request.text` (verbatim) and the PLAN artifact text. No Claude transcript, no Short Talk history, no environment dump, no session id, no API key. The PLAN lives under `runtime/artifacts/...`; the application reads it and delivers the full text as **one argv item**, so Codex's sandbox is never asked to read a Firefly-relative path.

### 21.4 Prompt (`core/workflow_prompt.build_implement_prompt`)

`IMPLEMENT_STEP_PROMPT`: "You are the implementation step of a user-confirmed plan → implement → review workflow. Work only in the current workspace. Implement the user's task using the supplied plan. Use your normal native permission and sandbox policy. Do not bypass approvals. At the end, leave the workspace in a reviewable state." + `User task:` + `Implementation plan:`. It never adds `--dangerously-skip-permissions`, auto-approve, skip-sandbox, never-ask, or force-success. Empty task/plan → `ValueError`.

### 21.5 Transport (unchanged 9C boundary)

The prompt is delivered exactly as in 9C: `ProcessLauncher.launch_agent` → `_handoff_command` → `_resolve_native` (real native exe / `node + codex.js`) → `QProcess.startDetached(program, argv, workspace)`. No PowerShell, no cmd.exe, no wt.exe, no clipboard, no SendKeys, no `shell=True`. Quotes/newlines/`;`/`&`/`|`/`$()`/backticks/Unicode/emoji/Windows paths in the PLAN arrive as literal prompt data. A prompt that would start with `-` or equal a subcommand is refused by `validate_handoff_prompt` (the wrapper always starts with "You are…", so it passes).

### 21.6 WorkspaceSnapshot (`core/workspace_snapshot.py`)

Qt-free, stdlib-only, **git-independent** (the project is not a git repo, so git is never a hard dependency). Per file: relative POSIX path, size, `mtime_ns`, and a `sha256` for files ≤ 5 MB (`HASH_SIZE_LIMIT`); larger files fall back to `size + mtime_ns` (`strategy="size_mtime"`, no unbounded hashing). `capture(root) → Snapshot`; `diff(baseline, current) → ChangeSet(added, modified, deleted)`. Small-file hashing detects a same-size / same-mtime rewrite; large-file changes are caught by size or mtime.

Safety: excluded dirs include `.git`, `.venv`, `venv`, `node_modules`, `__pycache__`, `build`, `dist`, `runtime`, cache dirs. Symlink/junction **directories are never followed**; every file's resolved path is re-checked to stay inside the workspace root — anything that escapes is skipped. The whole root missing/unreadable raises `WorkspaceSnapshotError`; a single unreadable file is recorded in `Snapshot.skipped` and the rest still scans. No watchdog, no full workspace copy.

### 21.7 CHANGED_FILES artifact

`changed_files_text(changes)` → deterministic markdown (`# Changed Files` + `## Added / Modified / Deleted`, `- (none)` for empty sections). Only workspace-relative paths; never absolute paths, file contents, or secrets.

### 21.8 Completion — user-confirmed

**Implementation complete** (or the empty-diff **Mark complete anyway** override) → `app._on_workflow_implement_complete` → `implement_executor.confirm_completion`:

1. Re-scan the workspace, `diff` against the retained baseline.
2. Empty diff without an explicit override → **no success**: the card shows "No workspace changes were detected." with **Keep waiting** (primary) and **Mark complete anyway** (explicit secondary). Nothing auto-completes on "maybe it only changed memory".
3. Otherwise write `CHANGED_FILES` then `IMPLEMENTATION_SUMMARY` (producer = Step 2); **attach only after both files exist**; then `mark_step_succeeded(StepCompletionEvidence(USER_CONFIRMED))`.

`IMPLEMENTATION_SUMMARY` is an honest deterministic record — Task / Plan (path + sha256 prefix) / "User confirmed the Codex implementation step completed." / changed files / `Evidence source: USER_CONFIRMED + WORKSPACE_SNAPSHOT`. It never fabricates a Codex transcript and never claims "Codex reported success".

Transaction: if the second write fails, the first artifact is rolled back via the new `ArtifactStore.remove_artifact` (file + index record), so a partial write is never attached and never looks succeeded.

### 21.9 Evidence policy

Step 2 success requires all of: (1) explicit user confirmation, (2) both expected artifacts attached, (3) real workspace change evidence. `mark_step_succeeded` is never called by a launch success, a HandoffState, or an external lifecycle event. The coordinator already rejects `MANAGED_AGENT_RESULT` for Codex (9D.1); 9D.4 adds the executor that structurally never routes lifecycle state to `mark_step_succeeded` (test: emitting a lifecycle `SUCCESS` for Codex while Step 2 runs leaves it `RUNNING`).

### 21.10 Final states

Step 2 → `SUCCEEDED`; Step 3 → `AWAITING_CONFIRMATION`; workflow → `WAITING_FOR_USER`. Step 3 has **no** Review button (action absent until 9D.5); the card shows "Waiting for confirmation".

### 21.11 WorkflowCard (`ui/workflow_card.py`)

New Step-2 actions: **Implement with Codex** (Step 2 `AWAITING_CONFIRMATION`), **Implementation complete** (Step 2 `RUNNING`), **Keep waiting** / **Mark complete anyway** (no-changes state). Status copy: Step 2 `RUNNING` → "Working in Codex", Step 2 `SUCCEEDED` → "Implementation confirmed" (never "Codex succeeded"). The card is still a view: all actions are signals the app routes; it never starts a process. Hide-not-cancel is unchanged; the detached Codex keeps running and the completion action remains available when the card is restored. No `View Codex` action was added (Firefly cannot reliably focus an existing detached native terminal, so it does not fake a second session).

### 21.12 Cancel semantics

`app._on_workflow_cancel`: Plan running → `plan_executor.stop()` (unchanged). After a Codex handoff → `implement_executor.reset()` (drops the baseline) + `coordinator.cancel_workflow` + the card shows "Workflow cancelled. Codex may still be running separately." Firefly does not own a reliable handle to the detached native process, so cancel never claims Codex was killed and never `taskkill`s codex.exe.

### 21.13 Files / tests / online

- Added: `core/workspace_snapshot.py`, `ui/workflow_implement_executor.py`, `tools/test_phase9d4_workspace_snapshot.py` (**28 tests**), `tools/test_phase9d4_implement_workflow.py` (**50 tests** + desktop/mock smoke: Plan with Claude → Implement with Codex → mutate workspace → Implementation complete → Plan ready / Implementation confirmed / Waiting for confirmation, with fake launcher, Codex real 0, Claude real 0).
- Modified: `core/workflow_prompt.py` (`build_implement_prompt` + `IMPLEMENT_STEP_PROMPT`), `core/artifact_store.py` (`remove_artifact`, factored `_write_metadata`), `ui/workflow_card.py` (Step-2 actions + status copy + no-change/override/cancel notices), `app.py` (`ImplementStepExecutor` wiring, `_on_workflow_implement`, `_on_workflow_implement_complete`, honest cancel), `docs/PHASE9_AGENT_ROUTER_ARCHITECTURE.md` (this append only).
- Not touched (protected): `state_broker.py`, `core/state_monitor.py`, `core/agent_events.py` semantics, `core/handoff.py` semantics, `core/agent_router.py`, `core/workflow_models.py` basic state semantics, `core/workflow_coordinator.py` basic transition semantics, `ui/workflow_executor.py` (PlanStepExecutor), `ui/recommendation_card.py`, `ui/process_launcher.py`, Hooks, `~/.claude`, `~/.codex`, permissions/approval/sandbox/trust, launch scripts, `requirements.txt`, animations.
- Full regression (**24 suites**: Phase 7/8/8C/9A/9B/9C/9D.1/9D.2/9D.3/9D.4-snapshot/9D.4-implement + single-instance + state broker/monitor): **all pass**.
- **Codex online calls: 0. Claude online calls: 0.** No token burned: the 9C argv transport is already online-verified; all 9D.4 acceptance runs use a fake native launcher + a temporary workspace + manual filesystem mutations.

### 21.14 Known limitations

`WorkflowPlan` stays memory-only (no `config/workflows.json`); a Firefly restart while Codex is `RUNNING` drops the workflow state and baseline (the detached Codex may continue). No workflow persistence, no changed-files delta beyond the flat list, no Review, no `View Codex` (cannot focus an existing detached terminal), and no online Codex smoke this phase. A very large PLAN could exceed the OS argv length limit (documented 9C command-line visibility/limit) — unchanged from 9C.

### 21.15 Phase 9D.5 suggestions

(1) Claude Review on the PLAN + CHANGED_FILES artifacts, read-only (`--permission-mode plan` + `--safe-mode`), producing a `REVIEW` artifact — Step 3's action. (2) Optional online Codex smoke reusing the 9C transport. (3) Workflow persistence (`config/workflows.json`, paused-resume) once workflows are genuinely runnable. (4) Orphan GC for `runtime/artifacts`. (5) Optional richer change evidence (per-file diff) while keeping the CHANGED_FILES contract relative-path-only.
---

## 22. Phase 9D.5 Outcome — Claude Review + Workflow Completion

Status: **DONE**. Step 3 (Claude Review) of `PLAN_IMPLEMENT_REVIEW` is now runnable: after Codex Implement is confirmed, the user clicks **Review with Claude**, Firefly runs a fresh transient read-only Claude session over the PLAN + CHANGED_FILES artifacts and the current workspace, writes the `REVIEW` artifact, and the workflow reaches `SUCCEEDED` — for the first time. Appended 2026-08-15.

### 22.1 Review executor (`ui/workflow_review_executor.py`)

A new `ReviewStepExecutor` (a `QObject`), deliberately separate from `PlanStepExecutor` and `ImplementStepExecutor` so none becomes an `if/elif` monolith. It owns only `agent=claude + intent=REVIEW + handoff=SHORT_TALK`; PLAN and IMPLEMENT steps are rejected. `execute(plan, step_id)` sequence:

1. Validate workflow non-terminal, current step, `READY`, supported shape, and a workspace.
2. `coordinator.mark_step_started` → Step 3 `RUNNING`.
3. Artifact-first context: verify the PLAN artifact exists with `kind==PLAN` and producer = the PLAN step, and the CHANGED_FILES artifact with `kind==CHANGED_FILES` and producer = the IMPLEMENT step. Missing / wrong-producer artifacts fail the step (`plan_artifact_missing` / `changed_files_artifact_missing`) — an arbitrary path is never trusted.
4. `ArtifactStore.read_text` for both; the IMPLEMENTATION_SUMMARY is read only if present with the right producer (auxiliary, never fails the review).
5. `build_review_prompt(original_request, plan_text, changed_text, summary_text=...)`.
6. `QuickAskRunner.ask(agent="claude", …, persistent=False, isolated=True)` — the same transient managed path as the Plan step.
7. Only a usable FINAL → `ArtifactStore.write_text(REVIEW)` → `attach_artifact` → `mark_step_succeeded(MANAGED_AGENT_RESULT)` → Step 3 `SUCCEEDED` → Workflow `SUCCEEDED`. An empty result / `AgentEvent.ERROR` / artifact-write failure fails the step; no auto retry, no fallback agent, no remediation loop.

### 22.2 Step 3 confirmation

- Step 3 starts `AWAITING_CONFIRMATION` after Step 2 succeeds, workflow `WAITING_FOR_USER`.
- The card's **Review with Claude** button emits `review_with_claude_requested`. `app._on_workflow_review` keeps **confirm and execute separate**: `coordinator.confirm_step` (→ `READY`, never launches) then `review_executor.execute`. A double click is a no-op (button disabled + executor busy gate).
- `confirm_step()` never starts Claude (9D.1 separation preserved).

### 22.3 Review context — artifact-first, read-only workspace

The review prompt carries only: `original_request.text` (verbatim), the PLAN artifact text, the CHANGED_FILES artifact text, and an optional IMPLEMENTATION_SUMMARY. No Claude Plan transcript, no Codex terminal transcript, no Short Talk history, no AgentEvent history, no environment dump, no session id, no API key. The instruction tells Claude to **review the current workspace in read-only mode** — it compares the task / plan / changed-files evidence against the actual current contents of the relevant files. Deleted paths from the change evidence may no longer exist; the review is based on the plan + remaining workspace.

### 22.4 Prompt (`core/workflow_prompt.build_review_prompt`)

`REVIEW_STEP_PROMPT`: "You are the review step of a user-confirmed plan → implement → review workflow. Review the current workspace in read-only mode. … Do not modify project files. Stay read-only." + a note about deleted paths + "Produce a concise Markdown review. Include: `# Verdict` `PASS / NEEDS_CHANGES / UNCERTAIN`, `# Summary`, `# Findings`, `# Validation`, `# Recommended Next Actions`." + `User task:` / `Implementation plan:` / `Changed files:` / `Implementation summary:`. It never asks for auto-approval, modifications, patches, permission/sandbox bypass, or a fixed PASS verdict. Empty task / plan / changed-files → `ValueError`.

### 22.5 Read-only contract (unchanged 9D.2 boundary)

The transient runner reuses the verified `--permission-mode plan` + `--safe-mode` claude args: per-invocation read-only permission mode + hook isolation. No `bypassPermissions`, no `acceptEdits`, no global settings/hooks/trust/sandbox changes. A Review step never writes `runtime/sources/claude.json` (lifecycle isolation).

### 22.6 Transient session isolation

Mirrors the Plan step exactly: `QuickAskRunner(session_manager=None)` → a private in-memory `SessionManager` with no store; `persistent=False` (no `--resume`, `--no-session-persistence`); `isolated=True` (`--safe-mode`). The ordinary Short Talk `SessionManager` and `config/sessions.json` are byte-unchanged after a review. Each managed workflow step relies on artifacts for context, never on conversation continuity.

### 22.7 REVIEW artifact

Written by `ArtifactStore.write_text` as `runtime/artifacts/<workflow_id>/review.md` (atomic, full text — never the UI-truncated copy), producer = Step 3, and attached only after the file exists. Without a REVIEW artifact the coordinator refuses Step 3 `SUCCEEDED` (expected-artifact gate). The verdict in the review text is **never parsed** and never drives a workflow transition; a `NEEDS_CHANGES` review still means the Review step (and therefore the workflow) succeeded.

### 22.8 Final states / Workflow SUCCEEDED semantics

Step 3 → `SUCCEEDED`; all three steps `SUCCEEDED` → workflow `SUCCEEDED`. **`WorkflowState.SUCCEEDED` only means the Plan, Implement (under the current evidence policy), and Review steps completed.** It never means "code approved", "tests all pass", or "no bugs". Review findings are the user's to act on in a future phase — 9D.5 does not auto-create a Codex fix step, does not re-enter Implement, and does not build a remediation loop.

### 22.9 WorkflowCard (`ui/workflow_card.py`)

New Step-3 action **Review with Claude** (visible only while Step 3 `AWAITING_CONFIRMATION`), new **Open review** action, and completion copy: Step 3 `READY` → "Preparing…", `RUNNING` → "Reviewing…", `SUCCEEDED` → "Review ready"; workflow `SUCCEEDED` → "Workflow complete". The card never shows "Code approved" / "Implementation passed" / "Everything looks good". After completion the card stays visible with **Open review / Open plan / Close**; closing it never deletes artifacts. The card is still a view: all actions are signals the app resolves.

### 22.10 Open review

`app._on_workflow_open_review` mirrors `_on_workflow_open_plan`: the REVIEW artifact ref is resolved via `ArtifactStore.resolve_path` (re-verified inside the artifact root) before `QDesktopServices.openUrl`. Arbitrary paths are never opened; a failed open shows a short notice, never a crash. No in-app markdown/chat viewer was added — Firefly only says "Review ready" and hands the full review to the OS.

### 22.11 Cancel semantics

`app._on_workflow_cancel`: Review running → `review_executor.stop()` and wait for the real `CANCELLED` transport event to complete the coordinator cancel (never a fake instant stop). Review waiting (Step 3 not started) → coordinator cancel directly. Plan/Codex cancel paths are unchanged.

### 22.12 One active workflow + completed cards

A `SUCCEEDED` workflow is terminal; `_active_workflow_id` is cleared on `WORKFLOW_SUCCEEDED`, so a new workflow may be created. A completed card may be closed/replaced by a new workflow; there is still no workflow history UI.

### 22.13 Files / tests / online

- Added: `ui/workflow_review_executor.py`, `tools/test_phase9d5_review_workflow.py` (**69 tests** + desktop/mock smoke), `tools/smoke_phase9d5_review_workflow.py` (online).
- Modified: `core/workflow_prompt.py` (`build_review_prompt` + `REVIEW_STEP_PROMPT`), `ui/workflow_card.py` (Step-3 action + Open review + completion copy), `app.py` (`ReviewStepExecutor` wiring, `_on_workflow_review`, `_on_workflow_open_review`, running-review cancel, shutdown stop), `tools/test_phase9d4_implement_workflow.py` (the 9D.4 `test_card_step3_has_no_review_action` was superseded by `test_card_step3_review_action` because 9D.5 adds the Step-3 action), `docs/PHASE9_AGENT_ROUTER_ARCHITECTURE.md` (this append only).
- Not touched (protected): `state_broker.py`, `core/state_monitor.py`, `core/agent_events.py` semantics, `core/handoff.py` semantics, `core/agent_router.py`, `core/workflow_models.py` basic state semantics, `core/workflow_coordinator.py` basic transition semantics, `ui/workflow_executor.py` (PlanStepExecutor), `ui/workflow_implement_executor.py` (ImplementStepExecutor), `ui/recommendation_card.py`, `ui/process_launcher.py`, Hooks, `~/.claude`, `~/.codex`, permissions/approval/sandbox/trust, launch scripts, `requirements.txt`, animations.
- Full regression (**25 suites**: Phase 7/8/8C/9A/9B/9C/9D.1/9D.2-artifact/9D.2-plan/9D.3/9D.4-snapshot/9D.4-implement/9D.5-review + single-instance + state broker/monitor): **all pass**.
- Online smoke (**1 Claude review call, 0 Codex**): temp workspace simulating a completed Step 2; real `build_review_prompt`; all 22 checks passed — Step 3 `AWAITING→READY→RUNNING→SUCCEEDED`, workflow `SUCCEEDED`, `review.md` correct, sessions.json + claude.json byte-unchanged, workspace unchanged (only Claude's own `.claude/` metadata), transient SessionManager empty, card shows "Review ready" / "Workflow complete".

### 22.14 Known limitations

Workflow state stays memory-only (no `config/workflows.json`); a Firefly restart drops it even though `plan.md` / `changed_files.md` / `review.md` remain under `runtime/artifacts`. Artifact orphans are not auto-GC'd. The review verdict is not structured (no reliable structured contract yet — the full text is the artifact). No in-app review viewer. No online Codex smoke.

### 22.15 Phase 9D.6 suggestions

(1) User-driven follow-up from a `NEEDS_CHANGES` review (explicit "create fix step" / re-open Codex on the review) while keeping confirm-as-the-only-execution-gate. (2) Workflow persistence (`config/workflows.json`, paused-resume) now that a workflow can genuinely reach `SUCCEEDED`. (3) Structured review verdict extraction as a separate, designed contract. (4) Orphan GC for `runtime/artifacts`. (5) Optional online Codex smoke reusing the 9C transport.

### 22.16 Phase 9D.6 Outcome

Status: **NO-GO** (controlled real E2E, 2026-08-15). The real chain
Claude Plan -> Codex Implement -> user confirm -> Claude Review did not complete.
Real blocker: the native Codex interactive CLI requires a real TTY; the 9C
direct-native argv transport delivered the prompt and spawned the Codex process
(transport PASS, spawn PASS), but the detached process printed
`Error: stdin is not a terminal` and made zero workspace changes in 26+ minutes
(no codex.json hook events, `greeting.py` untouched). Step 2 never succeeded,
Step 3 never ran, and the workflow never reached SUCCEEDED. Secondary finding:
the Step 1 PLAN artifact text was a generic "I don't see a specific task or
request yet" response, not a plan for the task. No production source changed
(82 files verified unchanged); pre- and post-smoke regressions pass.
See `docs/PHASE9D6_E2E_REPORT.md`. Workflow Persistence must not proceed until
the Codex interactive-TTY blocker is resolved.
