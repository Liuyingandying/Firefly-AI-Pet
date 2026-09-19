# Phase 8 Preflight + UI Architecture

> 日期：2026-08-15  
> 视觉 Single Source of Truth：`design_refs/firefly_ui_target.png`（已按原始 1672 × 941 图像实际查看）  
> 本文范围：现状审计、视觉分析、Phase 8 UI 架构；不包含 UI 实现。

## 1. Executive Summary

Firefly 下一阶段不应继续被定义为“显示 AI 状态的桌宠”，而应升级为 **Character-driven Multi-Agent Desktop Gateway**：角色是用户与 Claude、Codex、ChatGPT 之间的统一入口，负责 Talk、Launch、Monitor、Coordinate；工作区、会话、权限和设置只在需要时以角色周边的轻量 Overlay 出现。

本轮结论如下：

1. 当前工程的宠物动画、单实例控制、进程安全启动、工作区持久化、Quick Ask 协议解析和多 Agent State Broker 都有可保留价值。
2. 当前 `CompanionPanel` 是一个 500 × 690、深色、功能堆叠、长期占据视觉注意力的传统工具面板，和最终参考图的“极简 + 轻科幻 + Light Glass + 角色中心化”语言根本冲突。它应被拆除和替换，不能只换成白色皮肤。
3. `state_broker.py` 的纯 Python 解析、优先级、TTL、容错和测试已经稳定，建议冻结为兼容性核心，不做无意义重写。独立 Agent 状态展示应由新的只读状态监视层读取 `runtime/sources/*.json`，而不是扭曲 Broker 的“单一 resolved winner”职责。
4. UI 层当前直接轮询 Broker、驱动动画并写 `runtime/state.json`，职责过多。目标架构应把状态读取和 resolved-state 落盘移至 `core/state_monitor.py`，UI 只消费 Signal。
5. Quick Ask 的主要延迟来自“每轮新建 CLI 进程 + PowerShell 包装进程 + CLI/session 初始化 + 网络与模型响应”，不是 Qt 主线程同步阻塞。Phase 8A 不应顺手修性能，Phase 8C 应以可测量的首字延迟、续聊延迟和可靠性重新比较 CLI、Codex app-server、Claude CLI/Agent SDK 与长连接方案。
6. 当前 Codex 全局 Hook 确实存在并按 Agent 写入 `runtime/sources/codex.json`；当前 Claude 全局 `settings.json` 实际没有 Hooks，与仓库 README 所称“已配置全局 Claude Hooks”不一致。这是现状漂移，不属于本轮或 Phase 8A 的修改范围。

目标交互关系：

```text
User
  ↓
Firefly
  ├─ Claude
  ├─ Codex
  └─ ChatGPT
  ↓
Workspace / Task
  ↓
Agent execution
  ↓
State Broker
  ↓
Firefly feedback
```

未来工作流应允许：`Claude 分析 → Codex 执行 → Claude Review → Firefly 汇总`。Phase 8 只建立接口和边界，不实现 Agent Router。

## 2. Visual Reference Analysis

### 2.1 A. 从图片直接观察到的事实

以下内容只描述图像中可见的事实，不把实现猜测当成事实。

#### 整体构图与留白

- 画布为 1672 × 941，背景接近白色，带非常轻的冷灰蓝雾化渐变。
- 绝大多数画面没有控件、文字或装饰；左侧约三分之二基本保持空白。
- 主要交互集群位于画面右侧偏下区域，不采用居中主窗口，也没有覆盖全屏的内容框架。
- 视觉对象不是一个连成整体的大面板，而是数个相互留有呼吸距离的独立“浮岛”：角色、Speech Bubble、Agent Dock、Vertical Toolbar，以及左上角品牌标记。

#### Firefly 的位置与中心性

- Firefly 角色位于右侧偏下，约在画面宽度的 80% 附近、高度的 55% 附近。
- 角色站在一个浅色椭圆光台上，脚下有青白色辉光和柔和投影。
- 角色本体比任何单个控件更具细节、色彩和对比度，是右侧交互集群的视觉焦点。
- 多个微弱光点分布在角色周围，强化“流萤/能量”感，但数量很少。

#### 流萤与 UI 元素的位置关系

- Speech Bubble 在角色左上方，气泡尾部朝向角色。
- Agent Dock 在角色正下方，宽度大于角色，但高度很低。
- Vertical Toolbar 在角色右侧，与角色大致垂直对齐。
- 三者都围绕角色布置，却没有形成封闭窗口；角色是这些 UI 的空间锚点。

#### Agent Dock

- Dock 是一条横向、圆角很大的胶囊形浮层。
- 内部分为 Claude、Codex、ChatGPT 三段，段与段之间有很淡的竖向分隔线。
- 每段包含品牌图标、名称和一个小型青绿色状态点。
- Claude 图标为橙色，Codex 图标为灰色线性几何符号，ChatGPT 图标为绿色。
- 名称使用深色文字；状态点位于名称下方附近，不以大面积颜色抢占注意力。
- Dock 没有标题栏、菜单栏、滚动条或多余说明。

#### Vertical Toolbar

- Toolbar 是一条窄而高的竖向胶囊浮层，位于角色右边。
- 可见三个入口：顶部青色星芒、中央六边形线框、底部齿轮。
- 顶部入口处于选中态：图标更亮，并位于一个更明显的浅色圆形/椭圆光区内。
- 中央和底部入口之间存在很淡的横向分隔线；未选中图标使用低饱和蓝灰色。
- Toolbar 没有可见文本标签，依赖图标、位置和 hover/tooltip 补充语义。

#### Speech Bubble

- Speech Bubble 是横向圆角气泡，带右下方向的小尾巴。
- 内容为两行：第一行 `I'm Firefly!`，第二行 `How can I help you today?`。
- `Firefly!` 使用明亮青色，其余文字为蓝灰色。
- 气泡左侧有小型青色星芒，右上角还有很小的青色装饰点。
- 气泡背景接近白色、略半透明，边缘和阴影都非常轻。

#### 左上 FIRE FLY 品牌区

- 左上角有一个青色星芒图标和大字距的 `F I R E F L Y` 字标。
- 字标颜色非常淡，接近冷灰蓝；品牌存在感低于角色和交互控件。
- 品牌下方有一条短、极细、极淡的竖线。
- 品牌区没有被包进卡片，也不承担导航栏功能。

#### Light Glass、半透明、圆角、边框、阴影与辉光

- 所有浮层都使用接近白色的轻质感，而不是深色玻璃或高饱和霓虹。
- 浮层边缘有细而低对比的冷白/浅蓝边界；没有粗边框。
- 圆角非常大：Dock 和 Toolbar 接近胶囊，Bubble 也使用大圆角。
- 阴影范围较宽但浓度低，呈冷灰蓝，主要用于把白色浮层从浅背景中轻轻托起。
- 青色辉光集中在星芒、角色脚台和少量状态点附近；辉光是点状强调，不是全局发光。
- 图中没有厚重高光、强烈镜面反射或明显“磨砂噪点”。“Glass”主要由浅透明层、软阴影、细边界和冷色环境光共同表达。

#### 字体层级、色彩、密度与视觉层级

- 字体整体是现代无衬线；Dock 名称和 Bubble 正文的字号接近，品牌字标通过超大字距形成独立层级。
- 主要文字使用深蓝灰而非纯黑；次级图标和文字使用更浅的蓝灰。
- 主色为白、冷灰蓝、青色、薄荷绿；橙色只作为 Claude 品牌识别色出现。
- 控件密度很低：同一时刻可见的动作入口极少，且每个浮层只承担一个清晰主题。
- 视觉层级从高到低大致为：角色 → 角色邻近的 Bubble / Dock / Toolbar → 左上品牌 → 背景光雾与微光点。

### 2.2 B. 设计推断

以下是根据视觉事实得到的产品和实现推断，不是图片直接证明的行为。

- 默认状态应由多个透明、无边框、always-on-top 的小型 Overlay 组成，而不是一张透明大窗口里绘制全部内容。这样才能保留桌面可用性、点击穿透策略和真正的“大面积留白”。
- Firefly 应成为所有浮层的 anchor。拖动角色时，Dock、Toolbar 和已打开的 Bubble/Popover 应按布局规则跟随或重排，而不是保存彼此无关的绝对屏幕坐标。
- Dock 的 `selected`、`active` 和 Agent lifecycle state 是三个不同维度：选中项可有浅色底/边框，active 表示当前交互对象，状态点表达 idle/thinking/working/waiting/success/error/sleeping。
- Light Glass 不能只靠 `rgba(255,255,255,...)`。在 Windows 上应组合透明窗口、DWM/平台模糊能力（可用时）、低对比边界、软阴影和 fallback 背板，并测试浅色和深色桌面背景。
- 参考图的色值不能从压缩截图精确反推。建议把近似 token 集中在 `ui/theme.py`，通过视觉对照迭代，而不是把颜色散落在各 QWidget 的 QSS 中。
- 建议初始 token 方向：主文字冷深蓝灰、次级文字低饱和蓝灰、accent 青色、success 薄荷绿、error 柔和珊瑚红；玻璃面板使用较高白色透光率、1 px 冷白边界、大圆角和宽而淡的阴影。具体数值需在真实桌面上验收。
- 左上品牌区更像品牌氛围元素而非持续可点击导航；若产品并不需要全屏品牌层，可在桌面 Overlay 版中降级为首次启动/空闲时短暂出现，避免用一个全屏透明窗口承载它。

### 2.3 为什么它不像传统 Windows 软件窗口

它没有标题栏、窗口边界、菜单、Ribbon、标签页、状态栏、固定内容区和矩形应用背景；没有一个持续占据屏幕的“大容器”。信息被拆成角色周围的低密度浮岛，靠空间关系而不是窗口 chrome 组织。角色本身同时承担品牌、入口和状态反馈，次级界面按需出现，桌面背景与留白成为构图的一部分。

构成“极简 + 轻科幻 + Light Glass + 角色中心化”的关键特征是：

- **极简**：入口少、文案短、低密度、弱分隔、大量留白。
- **轻科幻**：青色星芒、点状能量光、冷色环境雾、几何线性图标，但不使用重霓虹或复杂 HUD。
- **Light Glass**：浅色半透明浮层、细边界、宽软阴影、大圆角、低对比层次。
- **角色中心化**：所有高价值入口围绕 Firefly 排列，气泡明确指向角色，角色的细节和对比度最高。

## 3. Current Architecture Audit

### 3.1 实际阅读范围

本次审计逐行阅读了实际代码和数据，而非只依赖 README：

- `app.py`
- `state_broker.py`
- `ui/companion_panel.py`
- `ui/process_launcher.py`
- `ui/quick_chat_protocol.py`
- `ui/workspace_store.py`
- `ui/__init__.py`
- `tools/simulate_event.py`
- `tools/stop_pet.py`
- `tools/run_cli.ps1`
- `tools/test_state_broker.py`
- `tools/test_phase7a_core.py`
- `tools/test_phase7b_core.py`
- `runtime/state.json` 与 `runtime/sources/{manual,codex,claude}.json`
- `start_pet.ps1`、`stop_pet.ps1`
- `verify_phase7a.ps1`、`verify_phase7b.ps1`
- `README.md`、`README_PHASE7A.md`、`README_PHASE7B.md`
- `config/ui_settings.json`、`requirements.txt`
- 只读核对了当前用户级 Claude/Codex Hook 配置；未修改任何配置。

### 3.2 当前已实现的功能

| 能力 | 实际实现 |
|---|---|
| 桌宠窗口 | PySide6 无边框、透明、always-on-top、`Qt.Tool` 窗口 |
| 动画 | `QMovie` 按 resolved state 映射 7 种状态到现有 GIF；sleeping 冻结首帧 |
| 拖动/点击 | Qt drag threshold 区分拖动与短点击；短点击切换 Companion Panel |
| 状态汇总 | 每 150 ms 调用 `state_broker.resolve()`；按优先级和时间戳选单一 winner |
| resolved state | `Pet._write_resolved()` 原子写入 `runtime/state.json` |
| Agent 源状态 | `tools/simulate_event.py` 原子写入各自的 `runtime/sources/<agent>.json` |
| 多 Agent 冲突处理 | Claude、Codex、manual 分源；损坏/半写文件被忽略；transient/manual/stale TTL |
| 单实例 | Windows named mutex；另有 `QLocalServer` 控制通道 |
| 启停 | `start_pet.ps1` 用 `pythonw.exe` detached 启动；`stop_pet.ps1` 经本地 socket 精确退出 |
| Workspace | 保存当前路径和最多 5 个 recent workspace；原子写 `config/ui_settings.json` |
| Agent Launch | Codex/Claude 在选定 workspace 启动；优先 Windows Terminal；ChatGPT 走系统 URL handler |
| Quick Ask | Codex/Claude 只读提问；异步 `QProcess`；停止、清空、effort 选择、耗时状态 |
| Session | 按 `agent + normalized workspace` 在内存保存 Codex thread ID / Claude session ID |
| 流式/事件 | Claude 支持 text delta；Codex 解析 JSONL 状态和最终 agent message |
| 安全基线 | Prompt 作为 argv 传递，不拼接 PowerShell 源码；Codex read-only，Claude plan mode |

### 3.3 Phase 7A / 7B / 7B.1 实际遗留状态

- **Phase 7A 可验证落地**：Companion Panel、workspace/current-recents、完整 CLI 启动入口、ChatGPT URL、Quick Ask 基础能力、拖动/点击区分均在代码中。
- **Phase 7B 可验证落地**：原生 session/thread ID 续聊、Codex JSONL、Claude stream-json、effort、elapsed/status、New chat 和协议测试都在代码中。`README_PHASE7A.md` 中“还没有原生持久 session、手工拼接历史”的说明已被 7B 实现取代，属于旧文档。
- **Phase 7B.1 无仓库内正式标记或独立说明**：不能仅凭名称宣称其完整交付。当前可观察到的后续状态是 Codex 用户级 lifecycle Hook 已安装并按 `--agent codex` 写独立源文件；Claude 当前全局设置却没有 Hook。应把“7B.1”视作需要由项目负责人确认的标签，而不是从代码反推一个不存在的版本边界。
- 所有现有纯 Python 测试本轮实际执行通过：State Broker 12 项、Phase 7A store、Phase 7B protocol/session。第一次直接运行 7A test 时因未按验证脚本设置导入路径而失败，改用模块方式后通过；这不是产品逻辑失败，但说明测试入口本身不够自包含。

### 3.4 当前模块耦合

`app.Pet` 同时承担窗口、拖动、动画、状态轮询、resolved-state 持久化、Panel 生命周期、进程入口菜单、单实例连接和退出清理。`CompanionPanel` 同时承担布局、主题、workspace CRUD、Agent launch、Quick Ask 表单、session UI、stream rendering、错误弹窗和相对定位。`QuickAskRunner` 则把进程生命周期、PowerShell 桥、Agent 协议选择、session registry、JSONL 解析调度和面向 UI 的中文状态文本集中在同一文件中。

因此当前最严重的 UI/backend 耦合点是：

1. `Pet._poll_state()` 直接调用 Broker、更新 Panel、换动画并写 resolved 文件。
2. `CompanionPanel` 直接实例化 `WorkspaceStore` 和 `QuickAskRunner`，也直接调用静态 `ProcessLauncher`。
3. backend 通过 UI 文案 Signal 暴露状态，而不是发出结构化 lifecycle event；后续 Dock、Bubble、Permission Card 会被迫重复解释文本。

## 4. Phase 7 Technical Debt

### 4.1 最大技术债

1. **单体深色 Companion Panel**：503 行 QWidget 把启动器、工作区、聊天、状态和设置集中到一个固定大面板，既违背参考图，也使任何局部 UI 改动牵动 backend。
2. **状态边界不清**：Broker 只负责 resolved winner 本来合理，但 UI 既读取又落盘；Dock 所需的每 Agent 独立状态没有正式只读服务；ChatGPT 也没有可观察 lifecycle source。
3. **每轮 CLI 冷启动的 Quick Ask 架构**：session ID 复用减少上下文重传，却没有复用进程、连接或服务端通道；感知延迟仍高。

### 4.2 其他债务与漂移

- README 主标题仍写 “Core v1 + Phase 7A”，但代码已含 Phase 7B。
- `README_PHASE7A.md` 的会话实现说明已经过时。
- 当前 Claude 全局设置与 README 的 Hook 声明不一致；旧备份中的 Hook 还曾默认写 manual source，不应当被当作当前正确配置恢复。
- 当前 Codex Hook 包含 start/prompt/pre-tool/post-tool/permission/stop/session-end，但没有与 README 中 Claude 映射相同的 error 事件桥；UI 不应假定所有 Agent 都能发出全部状态。
- `SessionRegistry` 仅驻留于桌宠进程，重启即丢失；没有 session 元数据、标题、最近使用时间或可恢复性模型。
- Agent 状态和 UI 交互状态尚未分层，`active`/`selected` 很容易与 `working` 混用。
- `QSS`、尺寸、文字和布局硬编码在 `CompanionPanel`，没有 theme token、DPI/多屏视觉规范和统一动效规则。
- 当前 `reset_position()` 只依据 primary screen；Panel 使用 `screenAt()`，多屏策略不一致。
- `stop()` 在 Windows 使用 `taskkill /T /F` 终止 Quick Ask 进程树，虽然有明确 PID，但仍属于强制终止语义，未来长驻连接需更温和的 cancel contract。

### 4.3 为什么当前大型深色 Companion Panel 应淘汰

- 它以 500 × 690 的持续矩形形成传统主窗口感，角色从产品中心退化为“打开工具面板的按钮”。
- 近乎不透明的深蓝背景、密集表单、多个 ComboBox/Button/TextEdit 与目标的浅色低密度浮岛完全相反。
- Workspace、Agent launch、Quick Ask、session 和状态都同时可见，破坏按需展开和大面积留白。
- 把它改成白色只会得到“大白色窗口”，不会自动获得空间锚定、轻量层级、局部浮层和角色中心性。
- 它的代码结构也无法自然支持多个独立 Overlay 的 outside-click、Esc、anchor reposition、z-order 和互斥显示规则。

### 4.4 Quick Ask 为什么慢

#### CLI process startup

每次 `ask()` 都新建 `QProcess` 并启动本地 CLI。即使已有 session ID，也会重新加载 CLI runtime、配置、认证状态、可能的插件/MCP 环境和命令解析。Phase 7B 复用的是远端/CLI 会话标识，不是本地进程。

#### PowerShell wrapper

Windows 路径为 `QProcess → powershell.exe -NoLogo -NoProfile ... -File tools/run_cli.ps1 → codex/claude`。`-NoProfile` 已减少 profile 成本，参数 splatting 也保障了安全，但仍多一次 PowerShell 进程创建、脚本加载和子进程转发。它是可测量的固定开销，不应凭感觉认定为唯一主因。

#### Network transport

CLI 启动后仍需建立或恢复到 Agent 服务的通信。DNS/代理/TLS/本地代理转发、连接重试、服务端排队和首包时间都会影响延迟。由于每轮 CLI 进程重启，底层连接是否能真正复用不可由当前 UI 保证。

#### Session initialization

`SessionRegistry` 只保存 ID。`codex exec resume <thread_id>` 和 `claude -p --resume <session_id>` 仍需要 CLI 初始化并向服务端恢复会话、校验 workspace/context。它避免手工重发整段历史，但不等于零成本续聊；桌宠重启还会丢失全部 ID。

#### Model reasoning

默认 `low` effort 只降低推理预算，不会消除请求排队、上下文准备、模型生成和工具读取。Codex 即使是 read-only，也可能检查 workspace；任务复杂度、上下文大小和输出长度仍是主要变量。

#### UI blocking / 感知阻塞

当前进程 I/O 使用异步 `QProcess` Signal，主线程并未同步等待 CLI；拖动理论上仍可响应。因此“慢”不能笼统归咎于 UI blocking。感知上仍有三个问题：一次只允许一个请求、请求期间多个控件被禁用、Codex 通常到 `item.completed/agent_message` 才显示正文，前面主要只有状态文本。大量增量文本直接追加到 `QTextBrowser` 也可能造成局部重绘抖动，但不是首字延迟的首要来源。

## 5. Components To Keep

### 5.1 优先冻结和保留

1. **`state_broker.py`**：保持纯 Python、无 Qt、单一 resolved winner、优先级/TTL/容错逻辑和现有测试。Phase 8A 原则上零改动。
2. **分源状态写入契约**：`tools/simulate_event.py` 的原子写、每 Agent 只写自己源文件、silent hook 不阻断 Agent 的设计应冻结。
3. **现有动画资产与状态映射**：继续使用 resolved state 驱动现有 GIF；本阶段不改素材。

### 5.2 保留但通过接口包裹

- `ProcessLauncher` 的 PATH 发现、argv 安全传递、Windows Terminal fallback 和 ChatGPT URL 能力。
- `tools/run_cli.ps1` 的参数安全边界；Phase 8A 不优化它。
- `quick_chat_protocol.py` 的纯函数参数构造、事件解析和 workspace-key 逻辑，供 Phase 8C 评估时作为兼容基线。
- `WorkspaceStore` 的原子持久化和 recent workspace 规则；由新的 `WorkspaceManager` 包装，而不是让 UI 直接持有 store。
- named mutex、`QLocalServer` 精确退出、start/stop 脚本。
- 已有 broker/protocol/store 回归测试。

### 5.3 Hooks 的冻结边界

- UI 永远不读取、写入或修补 `C:\Users\<用户名>\.claude\`、`C:\Users\<用户名>\.codex\`。
- 冻结 Hook 的产品契约：短时、silent、失败不阻断 Agent、绝对路径调用、只写对应 Agent source、原子写入、绝不改变权限/approval 决策。
- 当前 Codex Hook 的 agent-specific 写入和 lifecycle-to-state 映射保持不动。
- 当前 Claude Hook 实际不存在，不能把“没有配置”描述为已冻结的成功实现。恢复/重建 Claude Hook 必须是另一个明确授权的维护任务，且需要核对当前 Claude 官方事件支持；不属于 Phase 8A。

## 6. Components To Replace

- 用独立 Overlay 替换 `ui/companion_panel.py`；旧类在迁移完成后退役，不进行白色换肤。
- 把 `app.Pet` 拆为 composition root + `PetOverlay` + core services；拖动与动画留在 Pet，状态 I/O 移出 UI。
- 用结构化 Agent event 替代 `QuickAskRunner.status(str)` 这类 backend 直接生成展示文案的方式。
- 用 `WorkspaceManager` 替代 UI 直接实例化 `WorkspaceStore`。
- 用 `SessionManager` 替代 Runner 内部私有的 session registry 所有权。
- 用 `OverlayCoordinator` 统一管理 anchor、互斥、Esc、outside-click、screen clamping 和 z-order。
- 用集中 `theme.py` 替代散落 QSS、尺寸和色值。

## 7. Target UI Architecture

### 7.1 产品层次

```text
Presentation / Overlay UI
  PetOverlay · AgentDock · SpeechBubble · VerticalToolbar
  WorkspacePopover · SessionPopover · PermissionCard · SettingsPopover
                         │ signals / slots only
                         ▼
Application Core
  OverlayCoordinator · StateMonitor · AgentManager
  WorkspaceManager · SessionManager · Notification/KeepAwake services
                         │ adapters
                         ▼
Existing Integration Layer
  state_broker.py · ProcessLauncher · quick_chat_protocol.py
  WorkspaceStore · runtime/sources/*.json · platform launch APIs
                         │ external lifecycle events
                         ▼
Claude / Codex Hooks and future ChatGPT/provider adapters
```

### 7.2 推荐目录

```text
app.py                         # composition root only
state_broker.py                # frozen compatibility resolver
core/
    __init__.py
    state_monitor.py           # read-only source snapshots + resolved publisher
    agent_manager.py           # Agent registry, launch/lifecycle facade
    workspace_manager.py       # current/recent workspace application service
    session_manager.py         # session ownership and metadata
    models.py                  # typed state/event DTOs, no Qt widgets
ui/
    pet_overlay.py
    agent_dock.py
    speech_bubble.py
    vertical_toolbar.py
    overlay_coordinator.py
    popover_base.py
    workspace_popover.py
    session_popover.py
    permission_card.py
    settings_popover.py
    theme.py
```

这不是要求一次性创建全部文件。Phase 8A 只创建 UI Shell 必需部分和最小 workspace popover；Session、Permission 和完整 Settings 内容在 8B 落地。

### 7.3 关键架构原则

- 角色窗口与每个浮层都应是小型真实窗口/Overlay；禁止用一个全屏透明 QWidget 假装留白，因为它会吞噬桌面点击、破坏任务切换与多屏行为。
- `app.py` 只负责 QApplication、单实例、service/widget 组装和 shutdown 顺序。
- Core 发结构化状态；UI 决定图标、文案、颜色和动效。
- `state_broker.py` 继续只解析“当前总体展示状态”；Dock 的独立状态来自 `StateMonitor` 对 source snapshots 的只读解析。
- UI 不直接修改 Hooks，不直接写 `runtime/sources/*.json`，不直接写 resolved `runtime/state.json`。
- resolved `state.json` 若继续作为兼容输出，由 core 中的 `ResolvedStatePublisher` 原子写入；Pet 只接收 `resolved_state_changed`。
- Agent Router 未来以接口注入 `AgentManager`，不进入 QWidget，也不修改 Hook contract。

## 8. Component Responsibilities

| 模块 | 单一职责 | 明确不负责 |
|---|---|---|
| `PetOverlay` | 显示现有动画、拖动、短点击、角色 hit area | 读写 JSON、启动 Agent、管理 workspace |
| `AgentDock` | 显示三个 Agent 的 icon/name/status dot/hover/selected/active；发出选择与激活 Signal | 读取 source 文件、执行 CLI |
| `SpeechBubble` | 轻量问候、短反馈、Talk 入口；控制文本截断与自动消失 | 持有长聊天历史 |
| `VerticalToolbar` | Companion、Workspace/Sessions、Settings 三个入口及选中态 | 直接创建/销毁业务 service |
| `PopoverBase` | 统一 glass frame、anchor、close、focus、Esc/outside-click 行为 | 具体 workspace/session 业务 |
| `WorkspacePopover` | 最小 current/recent/browse UI；8B 扩展 workspace 信息 | 直接写设置文件 |
| `SessionPopover` | 8B 展示按 Agent/workspace 分组的 session dashboard | 直接调用 CLI resume |
| `PermissionCard` | 8B 展示“Codex 需要你的批准”与 `[查看]`，指向原 Agent approval surface | 自行批准、修改 permission/trust |
| `SettingsPopover` | UI 外观、位置、通知等产品设置 | 修改 Claude/Codex 全局配置或 Hooks |
| `OverlayCoordinator` | 所有浮层的锚定、互斥、z-order、多屏 clamp、Esc、outside-click | Agent lifecycle |
| `StateMonitor` | 定时读取各 source、调用 Broker、去重、发 per-agent/resolved Signal；兼容性落盘 | 决定 UI 样式 |
| `AgentManager` | Agent registry、capabilities、selected/active、launch facade；未来接 provider/router | 绘制 Dock、写 Hooks |
| `WorkspaceManager` | 包装 Store，验证/切换 current workspace，发变更 Signal | 展示 QFileDialog 细节 |
| `SessionManager` | 按 Agent/workspace 管理 session ID 和元数据；提供 clear/resume contract | 渲染聊天或 Popover |
| `state_broker.py` | 从现有源解析单一 resolved state | QWidget、独立 Agent UI 状态列表、Hook 修改 |

### 8.1 谁负责什么

- **谁读取 Agent 状态**：`StateMonitor`，不是 `AgentDock`。
- **谁负责 Pet animation**：`PetOverlay`，输入仅为 normalized resolved state。
- **谁负责 Agent lifecycle**：`AgentManager`；现阶段内部复用 `ProcessLauncher`，Phase 8C 再替换 Quick Chat transport。
- **谁负责 workspace**：`WorkspaceManager`；底层可继续用 `WorkspaceStore`。
- **谁负责 session**：`SessionManager`；Quick Ask provider 只能通过它查询/更新 session。
- **谁写 resolved state.json**：core 的 resolved publisher（可作为 `StateMonitor` 内部组件）；UI 零写入。
- **谁修改 Hooks**：没有任何 UI/core 模块有此权限。Hooks 是外部集成配置，仅在独立、明确授权的维护任务中处理。

## 9. State / Signal Flow

### 9.1 状态反馈流

```text
Claude/Codex lifecycle Hook
  → tools/simulate_event.py
  → runtime/sources/<agent>.json (atomic, agent-owned)
  → StateMonitor poll
      ├─ source snapshot normalization
      │    → agent_state_changed(agent_id, AgentState)
      │         → AgentDock.update_agent_state(...)
      └─ state_broker.resolve()
           → resolved_state_changed(ResolvedState)
                ├─ PetOverlay.apply_animation_state(...)
                ├─ SpeechBubble.maybe_show_feedback(...)
                └─ ResolvedStatePublisher → runtime/state.json (compatibility)
```

### 9.2 用户交互流

```text
PetOverlay.clicked
  → OverlayCoordinator.toggle_speech_bubble()

AgentDock.agent_selected(agent_id)
  → AgentManager.select_agent(agent_id)
  → active_agent_changed(agent_id)
  → AgentDock.set_active(...)

AgentDock.agent_activated(agent_id)
  → AgentManager.launch(agent_id, WorkspaceManager.current)
  → lifecycle_event(launching/launched/failed)
  → Dock + SpeechBubble feedback

VerticalToolbar.workspace_requested
  → OverlayCoordinator.show_workspace_popover(anchor=toolbar)
WorkspacePopover.workspace_chosen(path)
  → WorkspaceManager.set_current(path)
  → workspace_changed(path)
  → AgentManager + SessionManager receive new context
```

### 9.3 推荐 Signal 契约

- `StateMonitor.agent_state_changed(str agent_id, AgentState state)`
- `StateMonitor.resolved_state_changed(ResolvedState state)`
- `PetOverlay.clicked()`、`PetOverlay.drag_started()`、`PetOverlay.moved(QPoint)`、`PetOverlay.drag_finished()`
- `AgentDock.agent_selected(str)`、`AgentDock.agent_activated(str)`
- `VerticalToolbar.action_requested(str action_id)`
- `WorkspaceManager.workspace_changed(PathLike)`、`workspace_error(str code, str detail)`
- `SessionManager.session_changed(SessionRef)`、`session_cleared(SessionKey)`
- `AgentManager.lifecycle_changed(AgentLifecycleEvent)`、`permission_waiting(PermissionRef)`
- `OverlayCoordinator.transient_closed(str overlay_id)`

Signal payload 应为结构化对象/枚举；展示中文文案由 UI formatter 负责。这样未来 Router、通知和日志可以消费同一事件而无需解析字符串。

### 9.4 Overlay 关闭与定位规则

- Esc 关闭当前最上层 transient overlay；再次 Esc 再关闭下一层，不退出宠物。
- 点击所有 Firefly overlay 之外的空白区域关闭 transient Bubble/Popover；Dock 与 Toolbar 默认保留。
- 同一时刻最多一个业务 Popover（Workspace/Session/Settings）；Permission Card 可作为更高优先级叠层，但不能替用户批准。
- 拖动 Pet 时先隐藏 transient Popover 或让其跟随；Dock/Toolbar 按稳定 anchor 更新。
- 所有 anchor 位置经当前 screen available geometry clamp；靠近屏幕边缘时自动翻转方向。
- 不创建覆盖整个桌面的透明捕获窗口。Outside-click 应使用应用级事件过滤器和已知 overlay geometry，避免阻挡其他应用。

## 10. Phase 8A Plan — UI Shell

### 10.1 范围

- Pet Overlay（保留动画与拖动）
- Agent Dock（Claude / Codex / ChatGPT 独立状态展示）
- Speech Bubble
- Vertical Toolbar（Companion、Workspace / Sessions、Settings）
- Popover infrastructure
- Light Glass Theme
- 最小 Workspace chooser popover

路线中原本把完整 Workspace Popover 放在 8B，但 Phase 8A 验收标准要求“Workspace 使用 Popover”。解决方式是：8A 提供只含 current/recent/browse 的最小 Popover，8B 再补充 dashboard、session 关联、通知和更丰富的 workspace 信息。

### 10.2 Phase 8A 未来需要新增的文件

- `core/__init__.py`
- `core/models.py`
- `core/state_monitor.py`
- `core/agent_manager.py`（先做 registry/state/launch facade，不重写 Quick Ask）
- `core/workspace_manager.py`（薄包装现有 Store）
- `ui/theme.py`
- `ui/pet_overlay.py`
- `ui/agent_dock.py`
- `ui/speech_bubble.py`
- `ui/vertical_toolbar.py`
- `ui/popover_base.py`
- `ui/workspace_popover.py`（最小版）
- `ui/overlay_coordinator.py`
- 对应的无 GUI core 测试和最小 Qt interaction/geometry 测试（测试文件名在实现前统一确定）

### 10.3 Phase 8A 未来需要修改的文件

- `app.py`：收缩为 composition root，组装 service 与 Overlay，保留单实例/退出语义。
- `ui/__init__.py`：仅在需要公开稳定 UI 类型时更新。

`ui/companion_panel.py` 在 8A 迁移期间可暂时保留在磁盘作为 fallback，但默认路径不得继续展示；待验收后再由单独清理任务决定是否删除。Phase 8A 不应为迁移而改写 backend。

### 10.4 Phase 8A 明确禁止修改

- `state_broker.py` 的解析、优先级、TTL 和 schema。
- `tools/simulate_event.py` 及 Hook 写入契约。
- `C:\Users\<用户名>\.claude\`、`C:\Users\<用户名>\.codex\` 下任何 Hooks、permissions、approval、sandbox、trust 配置。
- `assets/animations/` 中任何动画素材。
- Quick Ask transport 与性能路径：`ui/process_launcher.py`、`ui/quick_chat_protocol.py`、`tools/run_cli.ps1`。
- `start_pet.ps1`、`stop_pet.ps1`、`requirements.txt`，除非实现中出现独立、被证实且另行授权的必要性；不能顺手修改。
- 不直接编辑 `runtime/state.json` 或 `runtime/sources/*.json`；运行时只能沿既有 owner contract 更新。
- 不实现 Agent Router、不做 Claude → Codex workflow、不新增自动 approval。

### 10.5 Phase 8A 验收映射

1. 默认只见 Pet + Dock + Toolbar，无传统主窗口。
2. 角色拥有最高对比度和细节，Overlay 均以角色为 anchor。
3. 拖动测试保持现有 click/drag threshold；多屏与边缘翻转新增验收。
4. Dock 与角色保持稳定间距，并随拖动重排。
5. 三 Agent 各有独立 presentation state；ChatGPT 在没有 telemetry 时不得伪造 working，只显示真实可知的 idle/selected/launch result。
6. Pet 动画只消费 `state_broker.resolve()` 产生的 resolved state。
7. 点击 Firefly 切换轻量 Speech Bubble，不打开大 Panel。
8. Workspace current/recent/browse 使用最小 Popover。
9. Esc 和 outside-click 关闭 transient UI。
10. 以参考图进行同屏截图对照，检查布局、留白、玻璃、阴影和密度。
11. 明确拒绝大白色窗口或全屏透明捕获层。
12. 不向参考图的大面积空白添加 dashboard、日志、装饰面板或永久文案。

## 11. Phase 8B Plan — Product Experience

- 扩展 Workspace Popover：workspace 元数据、最近任务入口、无效路径恢复，但保持轻量。
- 实现 Session Dashboard：按 workspace + Agent 显示 session、最近活动、continue/new/forget；底层归 `SessionManager`。
- 实现 Permission Card：文案如“Codex 需要你的批准”与 `[查看]`；按钮只能聚焦/打开原 Agent approval surface，不能代替用户批准。
- 通知策略：success/error/permission waiting 使用 Bubble、toast 或 taskbar attention 的分级规则；防止连续 Hook 事件造成提示风暴。
- Agent working 时防系统睡眠：由 core `KeepAwakeService` 根据所有 Agent aggregate busy 状态持有/释放系统执行状态；异常退出时确保释放。该逻辑不能放在 Widget show/hide 中。
- 完整 Settings Popover：仅产品自身设置，如 overlay scale、跟随、通知、开机启动候选项；不编辑 Agent permissions/hooks。
- 完善可访问性、键盘导航、DPI、多屏、浅/深桌面背景下的对比度和低性能 fallback。

## 12. Phase 8C Plan — Quick Chat Backend

Phase 8C 不应先押注技术名词，而应先建立可复现基准：CLI 冷启动、session resume、首个状态事件、首个文本 token、总耗时、取消耗时、重连、进程残留和错误恢复，Claude/Codex 分开记录。

需要重新评估：

- **Codex CLI**：保留当前安全 argv/read-only 基线，测量 per-turn startup 占比和 JSONL 真正的文本增量能力。
- **Codex app-server**：验证当前安装版本是否有稳定、受支持、可取消、可恢复 session 的长驻接口；若可用，用 adapter 隔离其协议。
- **Claude CLI**：测量 `-p --resume` 的启动与首 token；保留 stream-json parser 作为 fallback。
- **Claude Agent SDK**：核对认证、权限模型、session continuation、streaming、工具事件和本地分发条件；不能假设与现有 CLI 登录态完全等价。
- **session continuation**：把 session owner 移到 `SessionManager`，定义进程重启后的恢复/过期/无效 ID 行为。
- **streaming**：统一为结构化 `text_delta/status/tool/permission/final/error` event，UI 不感知提供方 JSON。
- **latency**：优先减少进程与连接冷启动；PowerShell wrapper 是否替换必须由 profile 数据决定，不能牺牲 argv 安全。

建议 provider contract：

```text
AgentProvider.start_session(context) -> SessionRef
AgentProvider.send(session, prompt, policy) -> stream[AgentEvent]
AgentProvider.cancel(turn_id)
AgentProvider.resume(session)
AgentProvider.close(session)
AgentProvider.capabilities() -> CapabilitySet
```

## 13. Phase 9 Direction — Agent Router

Phase 9 在 core 层增加 Router，不让 Router 直接操作 QWidget、Hooks 或 JSON 文件。

建议抽象：

- `TaskIntent`：用户目标、workspace、风险、是否允许写入、期望产物。
- `AgentCapability`：analysis、coding、review、chat、tooling、streaming、approval support。
- `AgentRecommendation`：推荐 Agent、原因、风险和需要用户确认的权限。
- `WorkflowPlan`：有序 `WorkflowStep`，每步指定 provider、输入、产物、approval gate 和 reviewer。
- `WorkflowContext`：跨 Agent 传递经过约束的摘要/产物引用，不隐式复制秘密或全部 transcript。
- `WorkflowEvent`：由 Firefly 聚合展示，不让多个 provider 同时直接争夺 UI。

目标流程：

```text
User task
  → Router recommends Claude analysis
  → Claude produces scoped plan
  → approval gate (if needed)
  → Codex executes in workspace
  → Claude reviews diff/result
  → Firefly summarizes outcome and next action
```

Router 只能推荐和编排；权限边界仍由各 Agent 原生机制与用户决定。

## 14. Risks

| 风险 | 影响 | 缓解 |
|---|---|---|
| 浅色 Glass 在白色桌面上消失 | 边界/可点击性不足 | 细边框 + 软阴影 + 动态 fallback 背板；多背景截图验收 |
| 用全屏透明窗口实现 outside-click | 阻挡桌面与其他应用 | 多小窗口 + app event filter + geometry hit test |
| 多个 always-on-top 窗口 z-order 漂移 | Popover 被遮挡/任务切换异常 | `OverlayCoordinator` 单点管理 owner、raise 和 focus |
| 状态源只覆盖 Claude/Codex | ChatGPT 状态可能虚假 | capabilities 标识 telemetry 支持；未知时显示真实 idle/unavailable，不猜测 |
| Broker 单 winner 不够 Dock 使用 | 三 Agent 无法独立显示 | 保留 Broker给动画；StateMonitor 另发 per-source snapshot |
| Claude Hook 当前缺失 | Claude live state 不更新 | 文档化漂移；另开授权任务修复，不让 UI 自改配置 |
| Hook event 风暴 | Bubble/动画频繁闪烁 | core 去重、节流、transient TTL；Dock 与 resolved 动画分开 |
| 动画拖动时多窗口重排抖动 | 视觉不稳定 | drag 中批量/帧节流位置更新，结束后最终 clamp |
| 高 DPI/多屏比例不同 | Anchor 偏移或越界 | 使用 logical coordinates、当前 screen geometry 和 DPI 测试矩阵 |
| Quick Ask backend 与 UI 再次耦合 | 8C 更换 transport 成本高 | Phase 8A 先建立 AgentEvent/provider 边界，不改 transport |
| Permission Card 被误解为审批入口 | 安全风险 | 只提供“查看”，明确跳转原 approval surface，不生成 approval 操作 |
| README 与实现继续漂移 | 错误验收结论 | 阶段结束基于代码和可执行测试更新事实表 |

## 15. Explicit Non-Goals

本轮以及 Phase 8A 不做：

- 不实现任何正式 UI 代码。
- 不把深色 Companion Panel 改成白色 Companion Panel。
- 不填充参考图中的留白，不新增永久 dashboard/log/chat 大面板。
- 不重写 `state_broker.py`。
- 不修改 Claude/Codex Hooks、permissions、approval、sandbox 或 trust。
- 不修改动画素材或重新生成角色。
- 不修 Quick Ask 延迟，不选择 app-server/SDK 胜者。
- 不实现持久 session dashboard 的完整功能。
- 不实现 Agent Router、多 Agent 自动交接或 Claude Review workflow。
- 不让 Firefly 自动批准权限请求。
- 不把 ChatGPT “已打开网页”误报成可监控的完整 Agent lifecycle。

## 16. Recommended Implementation Order

1. 固化视觉 token、Overlay 窗口规则、anchor geometry 和验收截图基线。
2. 新建 typed core models 与 `StateMonitor`，保持 `state_broker.py` 不动；先用测试证明 resolved 行为没有变化。
3. 从 `app.Pet` 提取 `PetOverlay`，保持 GIF、drag threshold、pause/sleeping 行为完全回归。
4. 实现 `OverlayCoordinator` 与 `PopoverBase`，先解决多屏、边缘翻转、Esc、outside-click 和 z-order。
5. 实现静态 Agent Dock 和 Vertical Toolbar，再接入 per-agent source state；将 selected/active/lifecycle 分开。
6. 实现 Speech Bubble 的短反馈与点击切换，禁止在其中堆叠完整聊天历史。
7. 用 `WorkspaceManager` 包装现有 Store，实现最小 Workspace Popover，以满足 8A 验收标准。
8. 收缩 `app.py` 为 composition root；默认入口切换到 Overlay Shell，旧 Panel 仅临时 fallback。
9. 进行 Phase 8A 视觉、拖动、多屏、状态、Esc/outside-click 和不阻挡桌面的验收；特别检查“没有大白窗”和“留白未被填充”。
10. 进入 Phase 8B：session、permission、notifications、keep-awake 与完整 popover 内容。
11. 进入 Phase 8C：用基准数据选择 Quick Chat transport，并通过 provider adapter 迁移。
12. 最后进入 Phase 9 Router；Router 只依赖 core contract，不反向侵入 Overlay 与 Hooks。

---

最终架构判断：`ui/pet_overlay.py`、`agent_dock.py`、`speech_bubble.py`、`workspace_popover.py`、`session_popover.py`、`permission_card.py`、`settings_popover.py`、`theme.py` 的拆分方向正确，但必须补充 `vertical_toolbar.py`、`popover_base.py`、`overlay_coordinator.py` 和 core `state_monitor.py/models.py`，否则状态 I/O、浮层协调和数据契约仍会回流到 Widget。`state_broker.py` 应保留并冻结，作为动画 resolved-state 的稳定事实源。
