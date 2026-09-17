# Firefly_AI_Pet — v1.0 Release Scope Freeze 审计

> 审计日期：2026-09-17
> 审计角色：Release Architect
> 审计方式：**纯静态只读**（未修改任何代码与配置；未运行任何会写 runtime / memory / conversation 数据的测试或脚本；仅执行文件枚举、`git status` / `git log` 只读查询与静态阅读）
> 审计基线：HEAD = `50e73a9`（feat(companion): add system tray resident mode），工作区含 **63 个已跟踪文件修改 + 384 个未跟踪路径，0 个已暂存**
> 输出物：本文档（本次审计唯一写入）
> 交叉证据：`docs/PHASE_D_RELEASE_REVIEW.md`（2026-08-20）、`Firefly_Release_Preflight_Audit.md`（结论 **RELEASE PREFLIGHT: BLOCKED**）、`Firefly_Release_Freeze_Plan.md`、`V1_FROZEN.md`（2026-08-30 实体验收冻结基线）

---

## 0. 审计合规声明

- 全程只读。未修改代码、未修改配置、未运行任何测试。
- 审计过程中未读取 `.env` 的值（仅核对 `.env.example` 的变量名与 `.gitignore` 覆盖关系）；`config/` 下个人配置仅核对结构，未摘录个人数据。
- 本文所有结论均给出文件级证据，路径相对 `E:\Firefly_AI_Pet\`。

---

## 1. 执行摘要

**Scope Freeze 裁决：当前状态为 BLOCKED（工程基线未固化），但能力面本身可立即冻结为三层发布范围。**

核心结论：

1. **产品能力面已经足够宽且分层清晰**。16 项能力中，10 项可归入 Bundled（进安装包），6 项归入 Optional External（依赖仓库外环境），其余明确 Not Included。没有发现"必须先补齐才能描述范围"的能力空洞。
2. **决定性阻断不在能力，而在可复现性**：63 modified + 384 untracked 意味着当前任何 tag / 安装包都无法从 Git HEAD 重建。`core/plugin_loader.py`、`core/quick_tools.py`、`core/learning/`（48 个实现文件）、`voice_client/`、`tools/` 桥脚本、`tests/` 大部分等关键能力文件均未被 Git 跟踪。
3. **发布工程缺口**：无打包管线（`start_pet.ps1` 依赖本机 `.venv`）；`requirements.txt` 缺少被直接 `import` 的 `requests`、`Pillow`（以及硬件链 `pyserial`、视频链 ffmpeg/faster-whisper 等外部可执行/可选依赖）。
4. **回归证据不足**：项目回归基线记录（2026-09-17）为 13 项已知失败（8 Learning UI + 2 app.py WIP + 3 真实视频）+ 4 个 `--ignore` 收集文件；`.pytest_cache/v/cache/lastfailed` 现存 43 条（stale，含历史记录），此后无干净全量跑测证据。
5. **隐私出网密集且缺披露面**：对话文本、屏幕截图、摄像头帧、文档摘录/页图、视频转录均会发往 TJU / 智谱 / DeepSeek 云端 API；Screen Vision 无用户可见总开关（仅窄触发词）。记忆索引（mem0 本地模式）与语音客户端（127.0.0.1）经核实**无云出口**。
6. **仓库外硬依赖 5 处路径落在 E 盘固定位置**（插件根、BiliInsight 服务、TJU 检索工程、PageLens 语音服务、测试硬编码路径），换机即整体降级。

---

## 2. 审计范围与目录映射事实

任务指定扫描 `core/ ui/ memory/ plugins/ learning/ voice/ assets/ config/`。实测目录存在性：

| 任务指定目录 | 实际状态 | 能力真实位置 |
|---|---|---|
| `core/` | 存在（80+ 模块） | — |
| `ui/` | 存在（50+ 模块 + `ui/v2/` 控制台） | — |
| `memory/` | 存在（16 模块 + `suggestion/`） | — |
| `plugins/` | **顶层不存在** | 插件壳代码在 `core/plugin_loader.py` / `core/quick_tools.py` / `ui/quick_tools_popover.py`；4 个插件**本体在仓库外** `E:\Firefly_AI_Private_Plugins\`（`DEFAULT_PLUGIN_ROOT` 硬编码，`core/plugin_loader.py:30`） |
| `learning/` | **顶层不存在** | `core/learning/`（完整子包：orchestrator/curriculum/teaching/decision/store 等） |
| `voice/` | **顶层不存在** | `voice_client/`（仓库内 HTTP 客户端）+ 仓库外 TTS/RVC 服务 `E:\Firefly_PageLens\voice\voice_module`；配置 `config/voice_config.yaml` |
| `assets/` | 存在 | `animations/`（7 个状态 gif）、`firefly.ico`、`paper_reader/pdfjs/`、`fonts/`（**仅 README，字体文件按许可策略不入库**）、`ui/`（README + background） |
| `config/` | 存在 | `companion.json`、`pet_preferences.json`、`ui_settings.json`、`sessions.json`（后三者本地个人配置，已 gitignore）、`hardware_devices.json`（含设备序列号）、`voice_config.yaml` |

---

## 3. 能力状态总表

状态取值：`ready` / `partial` / `experimental` / `external dependency`。

| # | 能力 | 状态 | 关键运行依赖 | 进入 v1.0 | 分类 |
|---|---|---|---|---|---|
| 1 | Companion 对话系统（含托盘驻留） | ready | 云 LLM Key（TJU/智谱/DeepSeek 任一） | ✅ | Bundled |
| 2 | Memory System | ready | 无外部强制依赖（本地 mem0/Qdrant/fastembed，懒加载可降级） | ✅ | Bundled |
| 3 | Memory Manager（UI） | ready | PySide6 | ✅ | Bundled |
| 4 | Conversation History（多会话） | ready | 无（纯 JSON） | ✅（附 1 项缺陷修复要求） | Bundled |
| 5 | Scratchpad | ready | 无 | ✅ | Bundled |
| 6 | Quick Tools（壳 + 门机制） | partial | 插件本体全部在仓库外 | ✅ 壳 / ⚠️ 插件体外置 | Bundled（壳）；插件体 Optional External |
| 7 | Screen Vision | ready（功能面） | 云视觉 API Key + Windows | ✅（附隐私披露要求） | Bundled |
| 8 | Camera Vision | partial | 仓库外插件 + 摄像头 + 云视觉 API | ⚠️ 条件性 | Optional External |
| 9 | Video Analysis | partial | 仓库外 BiliInsight 服务 / 插件体 + ffmpeg + ASR | ⚠️ 条件性 | Optional External |
| 10 | TJU Info Retrieval | external dependency | 仓库外插件 + 外部检索工程 + 校园登录 | ⚠️ 建议不承诺 | Optional External |
| 11 | Learning System | partial | 无外部强制依赖（SQLite 本地 + Provider 层） | ✅ 条件性（先固化 + 清测试债） | Bundled（附条件） |
| 12 | Voice Pipeline | external dependency | 仓库外 GPU TTS/RVC 服务 | ✅（默认关闭、静默降级） | Optional External |
| 13 | PageLens / PDF 栈 | ready | PyMuPDF/RapidOCR（已声明）+ 云 LLM/视觉 | ✅ | Bundled |
| 14 | Hardware / ESP32 | partial | ESP32 设备 + tools/ 桥 + pyserial | ⚠️ 条件性 | Optional External |
| 15 | 桌面宠物壳 + Agent 生命周期监控（v0.1 核心） | ready | 无 | ✅ | Bundled |
| 16 | Agent 工作流 Plan→Implement→Review + Short Talk | experimental | Claude/Codex CLI 本地登录 | ✅ 代码随包，**禁止宣称生产级** | Bundled（宣称受限） |

---

## 4. 逐能力详析

### 4.1 Companion 对话系统（含系统托盘驻留）

- **名称**：Companion 对话系统
- **状态**：ready
- **运行依赖**：云 LLM（`core/ai_router.py` 固定回退序 tju → zhipu → deepseek，全部 OpenAI-compatible 直连，env Key：`TJULLM_API_KEY` / `ZHIPU_API_KEY` / `DEEPSEEK_API_KEY`）；无 Key 时启动不受影响，聊天降级为错误文案。
- **是否适合进入 v1.0**：**是**。
- **理由**：组合根 `core/companion_runtime.py` 分层清晰；五源上下文装配（`core/companion_context_builder.py`：角色/Bond/记忆/历史/当轮 turn_context）；每阶段读取失败隔离降级，仅 Character/Provider 失败 fail-fast。托盘驻留模式已落地（commit `50e73a9`，`ui/system_tray.py`，退出为唯一真退出路径，`tests/test_system_tray.py` 覆盖）。测试面完整（test_companion_runtime / context_builder / config / conversation_runtime / character_conversation_runner 等）。
- **风险**：
  - 每轮对话文本出网至三家云 LLM；`auto_extract_enabled` 开启时**每轮追加一次记忆候选抽取 LLM 调用**。
  - 当前工作区 63 个未提交修改覆盖 companion 核心文件（companion_runtime / config / context_builder / conversation_runtime 等）——**冻结前必须先固化**。
  - `keyboard==0.13.5` 全局键盘钩子在打包 / 杀软场景易被标记。
  - `config/companion.json` 中 `auto_write_enabled=true` 命名有误导（实际仅做置信度提升与 pending 置顶，"promotion never writes"，不会自动直写记忆），发布文档应说明。

### 4.2 Memory System

- **名称**：Memory System（M2A/M2C 生命周期 + M3B 检索排序/访问隔离/Gate）
- **状态**：ready
- **运行依赖**：**无外部强制依赖**。JSON 权威存储 `runtime/companion/memory_records.json`（原子写、版本化）；语义索引为本地化 mem0 OSS（Qdrant 本地磁盘模式 `runtime/memory/qdrant/` + fastembed 本地嵌入 `BAAI/bge-small-zh-v1.5`，模型缓存 `runtime/memory/models/`）。mem0 的 LLM 推理被刻意禁用（infer=False，llm 指向 127.0.0.1:9）——**记忆索引零云出口**。mem0ai 在 requirements 中必装，但运行时懒加载可降级（`MemoryDependencyError` 时仓库记录仍权威，仅语义检索降级）。
- **是否适合进入 v1.0**：**是**。
- **理由**：M1→M3B.8 全系列报告与 30+ 专项测试；写入守卫（仅显式"记住…"句式可写，红线过滤器常开不可关闭——PEM/JWT/AKIA/ghp_/身份证/银行卡等硬阻断）；机器来源永远无法直写（AUTO_SOURCE_TRIGGERS）；M3B Gate 五规则 + 访问模式三级能力门（READ_ONLY/SAFE_WRITE/CONFIRMED_WRITE）；生产注入 100% 过 Gate（项目回归记录）。
- **风险**：
  - 用户记忆落在**项目目录 `runtime/`** 而非 `%APPDATA%`/`%LOCALAPPDATA%`——覆盖安装、便携分发、杀软隔离都会丢用户记忆，发布前需明确迁移策略或便携版声明。
  - fastembed 模型首次使用需联网下载（离线首启自动降级为无语义检索）。
  - `runtime/` 被 gitignore，但当前仓库内仍存在 `memory/test_memory.py` 等测试文件与生产模块同目录，需在打包时精确圈定。

### 4.3 Memory Manager（UI）

- **名称**：Memory Manager 2.0（`ui/memory_manager.py`）+ Memory Panel（`ui/memory_panel.py`）
- **状态**：ready
- **运行依赖**：仅 PySide6；写操作经 `_confirmed_handle` 派生 CONFIRMED_WRITE 视图，读永远只读。
- **是否适合进入 v1.0**：**是**。
- **理由**：当前/历史(superseded)/全部过滤、添加、编辑（旧记录 supersede 新记录，绝不原地覆写）、忘记（确认对话框）、导出全部落地（M3A 系列报告）；注意 `memory/memory_manager.py` 为已弃用的 v0.2 门面，真身在 `ui/memory_manager.py`（591 行），打包与文档需避免混淆。
- **风险**：低。删除走确认对话框，清空依赖 CONFIRMED_WRITE 门。

### 4.4 Conversation History（多会话）

- **名称**：Conversation History / 多会话管理
- **状态**：ready
- **运行依赖**：无外部依赖（stdlib JSON 单文件 `runtime/companion/conversation.json`，原子写含 WinError 5 重试；每会话 40 条有界窗口 + 可选摘要 + 独立标题）。
- **是否适合进入 v1.0**：**是**（附 1 项缺陷修复要求）。
- **理由**：create/switch/delete/rename/启动恢复全部落地并有测试（test_conversation_store / test_conversation_multi_session / recent_sessions 系列）。注意与另一域区分：`core/session_manager.py` + `config/sessions.json` 管理 Claude/Codex CLI 原生会话映射，二者不是同一系统。
- **风险**：`ui/v2/recent_sessions.py:28-34,73-74` 在 store 缺失/异常时**回退显示硬编码 MOCK_SESSIONS（假会话列表）**——v1.0 必须改为空态，否则用户会看到不存在的会话。另：本机 `config/ui_settings.json` 已被 pytest 污染（指向 `.pytest_tmp_*`），暴露**测试会写真实用户配置**的隔离缺陷（该文件被 gitignore，不入库，但需修复测试隔离）。

### 4.5 Scratchpad

- **名称**：Scratchpad（临时收件箱）
- **状态**：ready
- **运行依赖**：无。文件存储 `runtime/companion/scratchpad/items.json` + 图片副本 `assets/<uuid>.<ext>`；损坏文件隔离重命名而非崩溃；20MB 上限 + 真解码校验。
- **是否适合进入 v1.0**：**是**。
- **理由**：明确定位"不是记忆/不是经历/不入对话"；拖放策略严格（远程 URL 永不下载；本地图片复制入库，永不删用户原文件）；有完整报告与测试（test_scratchpad_store / drop / window）。懒初始化（app.py:614-621, 762-772）。
- **风险**：低。数据同样落项目目录 `runtime/`（同 4.2 迁移问题）。无配置键（路径硬编码），打包后路径行为需验证。

### 4.6 Quick Tools（壳 + 门机制 + 插件注册）

- **名称**：Quick Tools（插件注册表 + 门机制 + 管理弹窗）
- **状态**：partial（壳与门机制 ready；4 个插件本体全部在仓库外）
- **运行依赖**：`core/plugin_loader.py`：默认插件根硬编码 `E:\Firefly_AI_Private_Plugins`（`FIREFLY_PLUGIN_PATH` env 可追加）；`MANAGED_PLUGIN_IDS` 白名单四插件（firefly-video / firefly-camera-vision / learning-focus / tju-info-retrieval）；启停唯一真源 `is_plugin_enabled`（持久化于 `config/pet_preferences.json` 的 `plugins.<id>.enabled`），无缓存、无旁路（有专项审计文档）。发现在本仓（AST 只读探测不 import），**执行体不在本仓**。
- **是否适合进入 v1.0**：壳与门机制 **是**；插件体 **否（Optional External）**。
- **理由**：门机制有完整审计（`Quick_Tools_Capability_Gate_Audit.md`：firefly-video VERIFIED/FROZEN、camera PASS、learning-focus 语义清晰、tju 默认开启）；`core/quick_tools.py` 为纯内存注册表无持久化副作用。
- **风险**：
  - **换机即空壳**：四插件目录缺失时 Quick Tools 卡片全部不出现（fail-safe 但用户可感知）。
  - `tju-info-retrieval` 在 prefs 无键时**默认 enabled=True**。
  - manifest 的 `capabilities` 仅展示元数据、非宿主强制权限。
  - `plugin_loader.py`、`quick_tools.py`、`quick_tools_popover.py` 均为 **git untracked**。

### 4.7 Screen Vision

- **名称**：Screen Vision（显式触发的屏幕理解）
- **状态**：ready（功能面）；隐私面需披露
- **运行依赖**：云视觉链 TJU v3（`TJULLM_API_KEY`）→ 智谱 `glm-4.6v-flash`（`ZHIPU_API_KEY`）→ DeepSeek vision（`DEEPSEEK_API_KEY`）；推理链另用 `TJUTOKEN`；本地依赖 requests/Pillow/Win32（**仅 Windows**）。failover + 共享熔断器 + 错误脱敏（`screen_vision/safety.py`）。
- **是否适合进入 v1.0**：**是**（附强制隐私披露与总开关产品决策）。
- **理由**：契约明确"绝无后台捕获、绝不写盘"（`screen/capture.py`、`service.py`）；仅窄触发词（"看一下我的屏幕"等）或 `/look` 显式触发，Stage-1 policy"宁漏勿误"；实现完整（failover/熔断/错误分类/截图不落日志）。
- **风险**：
  - **无用户可见总开关**——唯一设置键 `screen_vision_fast_mode` 只切换快/韧性路由，两条路径都出网；建议 v1.0 前补总开关或至少在设置页与发布说明明示。
  - 截图以 base64 发往云端，可含任意屏幕内容；仅支持主屏截取，多屏场景 last-window 目标可能回退整屏。
  - 隐私与稳定性核心文件（config/service/trigger/capture）当前为**未提交修改状态**。

### 4.8 Camera Vision

- **名称**：Camera Vision（显式触发的单帧摄像头理解）
- **状态**：partial
- **运行依赖**：宿主侧捕获在仓（`core/screen_vision/screen/camera.py`，QCamera 单帧 + 8s 看门狗，无后台/无落盘）；**插件本体在仓库外** `E:\Firefly_AI_Private_Plugins\firefly_camera_vision\`；摄像头硬件 + Qt Multimedia；分析复用 4.7 云视觉链。
- **是否适合进入 v1.0**：**条件性**——随包仅能承诺"宿主侧"，卡片随插件目录缺失而消失。
- **理由**：门控链完整且 fail-closed（`app.py:265-267` 以 `is_plugin_enabled` 为唯一真源；独立开发入口 `lambda: False`；触发顺序保证"看看我的屏幕"永不误开摄像头，有测试锁定）；Vision-1A/1B/1C 三阶段报告齐备。
- **风险**：摄像头帧出网（同 4.7 隐私面）；`camera.py` git untracked；真实硬件回归依赖本机摄像头（历史线程编组问题已修复，但无干净机器证据）；插件目录不在发行边界。

### 4.9 Video Analysis

- **名称**：Video Analysis（B 站视频陪学 + 本地视频分析）
- **状态**：partial
- **运行依赖**：**B 站链**：外部服务 `E:\Firefly_BiliInsight_Service`（子进程 JSONL 调用，自带 .venv 与 faster-whisper 本地 ASR，transcribe 超时 900s）→ 云 LLM 摘要/出题/判卷。**本地视频链**：firefly-video 插件（仓库外）+ ffmpeg/ffprobe + faster-whisper/PySceneDetect/RapidOCR（**均未进 requirements**）。门：`core/capabilities/video_gate.py` 实时读 `is_plugin_enabled("firefly-video")`，fail-closed，任何 ffprobe/ffmpeg 之前拒绝。
- **是否适合进入 v1.0**：**条件性**——代码与门机制可随包，**能力承诺必须标注"需外部服务"**。
- **理由**：B 站集成是真实的（真实下载 + 本地 ASR + 云摘要 + 会话内追问/时间点追问/陪学闭环）；gate 审计 VERIFIED/FROZEN。
- **风险**：
  - 换机必坏（外部服务路径硬编码，可被 `FIREFLY_BILI_SERVICE_DIR` env 覆盖但默认值为本机）。
  - 长视频 transcribe 最长可阻塞 900s（用户可感知卡顿）。
  - 转录文本出网至云 LLM。
  - 项目回归基线中 **3 项真实视频测试失败**（`.pytest_cache/lastfailed` 含 `test_p10_real_video` / `test_real_frame_analysis_docker_video` / `test_real_timestamp_followup` 等 stale 记录），无 clean-run 证据。
  - 潜在旁路：`VideoQa.answer/summarize` 内部调 `process()` 不传 gate 参数——当前无生产调用方，属潜在风险，v1.0 后应封堵。

### 4.10 TJU Info Retrieval

- **名称**：TJU Info Retrieval（科研信息检索插件）
- **状态**：external dependency
- **运行依赖**：双层外置——插件本体 `E:\Firefly_AI_Private_Plugins\tju_info_retrieval\` + 外部检索工程 `E:\AI_Workspace\TJU_Info_Retrieval`（每请求新建 subprocess，shell=False，health 20s / search 150s 超时）；校园登录态（过期进入 AUTH_REQUIRED，提供受控浏览器手动重登 UX，不碰凭据）。
- **是否适合进入 v1.0**：**否（建议作为本机可选件，不进安装承诺）**。
- **理由**：功能完整（allowlist、保守触发——仅显式"用TJU信息检索查…"、三处 open_ui 入口共享同一 `plugin.open_ui()`、热开关、AUTH 恢复 UX，4 个专项测试）；工程缺席时**绝不阻断启动**（discover 对不存在目录静默跳过，聊天侧降级文案）。
- **风险**：双外部工程 + 测试硬编码本机路径（clean 机器上 4 个 TJU 测试会失败）；AUTH 依赖受控 Edge 浏览器；默认 enabled=True（见 4.6）。

### 4.11 Learning System

- **名称**：Learning System（学习智能体）
- **状态**：partial
- **运行依赖**：无外部强制依赖。确定性规则内核 + SQLite（`%LOCALAPPDATA%\FireflyAI\learning\learning_store.sqlite3`，SCHEMA_VERSION=4，19 张表，幂等迁移）；LLM 仅充当"教师"产出文案，被 `LearningResponseContract` 强约束（锚点校验），掌握度唯一裁决者是规则引擎（有测试锁定"无 LLM 判定"）；课件由 PDF→OCR→确定性 draft 适配器生成，须用户 confirm_draft 激活。
- **是否适合进入 v1.0**：**条件性是**——先完成两项前置（见风险）。
- **理由**：架构成熟（Phase 0→9C 共 30+ 阶段报告；`docs/PHASE8_UI_ARCHITECTURE.md`、`docs/PHASE9D6_E2E_REPORT.md`）；规则内核冻结语义清晰（难度封顶、跨天证据、时间永不降级）；v2 控制台集成与启动恢复已落地。
- **风险**：
  - `core/learning/` **48 个实现文件全部 git untracked**——clean checkout 无法重建该能力（决定性）。
  - **UI 测试债**：项目回归基线 13 项失败中 8 项为 Learning UI（Context Status Card 重构后 `test_learning_entry_ui` / `test_learning_runtime_integration` / `test_learning_runtime_v65` / `test_learning_restore_semantics` 等引用旧属性）。
  - 真实数据库中只有 draft/会话数据（courses=2, draft_chapters=82, concepts/assessments/reviews=0），**active curriculum 从未走通真实闭环**。
  - schema 自动升级无备份/回滚验证预案。

### 4.12 Voice Pipeline

- **名称**：Voice Pipeline（TTS 语音播报）
- **状态**：external dependency（客户端部分 ready）
- **运行依赖**：仓库内 `voice_client/`（HTTP 客户端 + 播放按钮 + 文本清洗，"永不抛异常"）→ **仓库外 GPU 服务** `E:\Firefly_PageLens\voice\voice_module`（Python 3.10 / torch 2.5.1 / edge-tts+RVC，独立 venv，主程序**不启动不监管**），`http://127.0.0.1:8300/voice/speak`。配置 `config/voice_config.yaml`：`voice.enabled` 总开关、`auto_play`（默认 false，仅显示播放按钮）、超长截断 1600 字符。
- **是否适合进入 v1.0**：**是（以默认关闭的 Optional External 形态）**——客户端代码随包，服务不随包，文案不得宣称"语音随包可用"。
- **理由**：客户端完整且有回归（test_voice_integration / expression / ux / v131 / v132）；失败仅日志、零影响文字聊天；v1.3.1 修复了停止失效与晚到结果翻转 UI。
- **风险**：外部服务生命周期完全独立；外部环境 numpy<2 与本仓 venv numpy 2.x 冲突不可合并；`voice_client/` 与 `voice_config.yaml` 当前 git untracked；模板注释引用本机绝对路径需清理；已知小缺陷：`connect_timeout_s` 配置存在但 urllib 实际只用单一 timeout。

### 4.13 PageLens / PDF 栈

- **名称**：PageLens（网页概念卡）+ PaperLens 2.2（PDF 阅读）+ 文档附件管线
- **状态**：ready
- **运行依赖**：PyMuPDF、rapidocr（均在 requirements）；websockets 桥（**仅绑定 127.0.0.1:17321**，严格 JSON 类型白名单）；浏览器扩展在仓 `extensions/pagelens_bridge/`；文档附件支持 PDF/DOCX/PPTX/TXT/MD/XLSX/CSV（python-docx/pptx/openpyxl 已声明）；懒 OCR 会话内存态、从不落盘；`document_router.py` 为无模型调用的 5 路规则路由。PPTX 视觉页转换依赖本机 PowerPoint COM（无则该路径降级）。全局热键 Ctrl+Alt+Shift+L。
- **是否适合进入 v1.0**：**是**。
- **理由**：PaperLens 2.2 实施/审计报告、OCR Overlay Feature Freeze 审计、E2E 桥测试齐备；隐私面相对克制（文档**仅相关摘录出机**，历史只留占位符）。
- **风险**：
  - 直接 `import` 的 `requests`（screen_vision 4 处）与 `Pillow`（document_vision.py）**不在 requirements.txt**，靠传递依赖存活——打包必炸点。
  - PDF 页图/摘录出网至云 LLM/视觉。
  - Bridge origin 认证弱（接受所有 WebSocket origin，仅回环缓解；Phase D issue 5 建议 handshake token）。
  - 桥按需起线程 + 热键依赖 `keyboard` 全局钩子。

### 4.14 Hardware / ESP32

- **名称**：Hardware / ESP32（Prism HUD 桥 + LED 桥 + 宠物状态输出）
- **状态**：partial
- **运行依赖**：ESP32 设备（Prism COM5 / CH343 1A86:55D3；LED COM10 / 303A:1001）；`tools/esp32_serial_bridge_poc.py` 与 `tools/firefly_led_bridge.py`（**两个按桥分离的互斥体**，非单一全局互斥体）；`tools/hardware_ports.py` 按 显式 port → USB serial → VID:PID 解析；`tools/firefly_runtime_supervisor.py` 独立 Job Object 监管，"一个失败绝不阻断另一个"。
- **是否适合进入 v1.0**：**条件性**——主程序侧完全解耦可随包；桥与设备为 Optional External。
- **理由**：**硬件不是启动必需**——app.py 仅原子写 `runtime/hardware_state.json` / `runtime/led_state.json`（OSError 静默），supervisor 设备缺席时 "Prism bridge skipped"；设备解析三级降序；测试覆盖桥协议/状态写/hysteresis。
- **风险**：
  - `config/hardware_devices.json` **含设备序列号与维修史，不得原样进 release**（需脱敏 example 模板，目前不存在）。
  - 3 个桥/端口脚本 + supervisor git untracked；`pyserial` 未进 requirements。
  - 互斥体测试结果依赖硬件在线状态（曾因停栈"环境性通过"）。

### 4.15 桌面宠物壳 + Agent 生命周期监控（补充：v0.1 核心）

- **名称**：Desktop Pet Shell + Claude/Codex 生命周期监控 + Agent 推荐/移交
- **状态**：ready
- **运行依赖**：无外部强制依赖（本地状态文件轮询；Short Talk 需本机 Claude/Codex CLI 登录，缺席时路由显示 unavailable）。
- **是否适合进入 v1.0**：**是**。
- **理由**：这是 2026-08-30 **实体验收通过的 V1_FROZEN 基线**（Bridge Lifecycle v1，VERIFIED/FROZEN，含备份恢复路径）；Phase D 评审对桌面壳/生命周期/Agent 推荐均 GO；`docs/releases/v0.1.0-firefly-demo.md` 有发布叙事。
- **风险**：`start_pet.ps1` → supervisor → `.venv\Scripts\pythonw.exe` 启动链要求发布自带 venv 或改打包 exe（当前无 PyInstaller 管线）。

### 4.16 Agent 工作流 + Short Talk（补充：受宣称限制）

- **名称**：Plan → Implement → Review 工作流（`core/workflow_coordinator.py` 等）+ Short Talk（Claude/Codex CLI 流式聊天）
- **状态**：experimental
- **运行依赖**：本机 Claude / Codex CLI 登录（`~/.claude`、`~/.codex`）；Plan/Review 直连 Anthropic 兼容端点（env：`ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` 等，密钥仅内存、不进 repr/artifacts）。
- **是否适合进入 v1.0**：**代码随包可以；但 v1.0 材料不得宣称生产级**。
- **理由**：Phase D 评审（2026-08-20）裁决：全链在线 E2E 最近一次为 **NO-GO**，H2/H4 热改后离线套件绿但**完整在线链未复认证**；工作流状态在内存（重启丢失进行中工作流）。
- **风险**：确认门与产物安全有测试，但"进程启动 ≠ 任务成功"的边界必须在发布文案中保留。

---

## 5. 分类结果

### 5.1 Bundled（可直接进入安装包）

| 能力 | 条件/备注 |
|---|---|
| Companion 对话系统（含托盘驻留） | 冻结前先固化 63 个未提交修改 |
| Memory System（含 M2A/M3B 全链） | 无 |
| Memory Manager（UI） | 无 |
| Conversation History（多会话） | 必须移除 RecentSessions MOCK_SESSIONS 回退 |
| Scratchpad | 无 |
| PageLens + PaperLens 2.2 + 文档附件 + 浏览器扩展 | requirements 补 requests/Pillow |
| Screen Vision（宿主代码） | 隐私披露强制；建议补总开关 |
| Quick Tools 壳 + 门机制 + 注册表 | 插件体不随包（见 5.2） |
| Learning System | 附条件：固化 `core/learning/` 入库 + 清 8 项 Learning UI 测试债 |
| Providers 层（ai_router / catalog / 3 家适配） | Key 控激活，无 Key 降级 |
| 桌面宠物壳 + 生命周期监控 + Agent 推荐（v0.1 核心） | 无 |
| Agent 工作流 + Short Talk（代码） | 宣称受限：不得称生产级（见 4.16） |
| assets（animations 7 gif / firefly.ico / paper_reader/pdfjs） | 字体文件按仓库策略不入包（系统字体兜底） |

### 5.2 Optional External（依赖外部环境，代码可随包但能力不承诺）

| 能力 | 缺失外部环境时的表现 |
|---|---|
| Camera Vision | 插件体 `E:\Firefly_AI_Private_Plugins\firefly_camera_vision\` 缺失 → Quick Tools 无卡片 |
| Video Analysis | BiliInsight 服务 / firefly-video 插件 / ffmpeg+ASR 缺失 → 门 fail-closed 拒绝并提示 |
| TJU Info Retrieval | 双外部工程缺失 → 插件静默不加载，聊天降级文案 |
| learning-focus 插件 | 插件体缺失 → 仅缺附加能力，核心 Learning System 不受影响 |
| Voice Pipeline | 外部 TTS 服务缺席 → `voice.enabled=false` / 连接失败静默，文字聊天零影响 |
| Hardware / ESP32（桥 + 设备） | 设备缺席 → supervisor 跳过桥，app 完全正常 |

### 5.3 Not Included（不进入安装包 / 发布物）

| 项 | 理由 |
|---|---|
| `.env`（仅 `.env.example` 可随包，且需修正漂移：删 DASHSCOPE 死配置、补 `TJUTOKEN`） | 凭据 |
| `runtime/` 全部用户数据（对话/记忆/向量库/建议/便签/bond/provider 状态） | 用户隐私数据 |
| `config/ui_settings.json`、`config/sessions.json`、`config/pet_preferences.json` | 本机个人配置（已 gitignore；注意当前 ui_settings 已被 pytest 污染） |
| `config/hardware_devices.json` | 含设备序列号/维修史；以脱敏 `hardware_devices.example.json` 替代（待创建） |
| `assets/fonts/` 字体二进制 | 许可策略（README 明示"本仓库不附带任何字体文件"，系统字体兜底） |
| `core/provider_state.json` | 运行时可变状态混入源码目录（Phase D issue 9，建议迁 `runtime/`） |
| RecentSessions MOCK_SESSIONS 假数据 | 若不修复回退路径，假会话不得出现在发布版 |
| DASHSCOPE（`dashscope-qwen`）provider 配置 | catalog 中已 deprecated 且无代码读取 |
| 一次性调试工件：`.pytest_tmp_*`（数十个）、`.pytest_camera_*`、`.console_*.png`、`.context_*.png`、`runtime/` 下诊断脚本与临时日志、`tools/` 冒烟/基准脚本 | 非产品面 |
| `tests/` | 测试不随用户包（可随源码发布另行决策） |

---

## 6. 跨能力横切风险

### 6.1 隐私出网矩阵（v1.0 发布说明必载）

| 数据 | 去向 | 触发方式 | 可关断性 |
|---|---|---|---|
| 对话文本（每轮） | TJU/智谱/DeepSeek 云 LLM | 每次聊天 | 摘除所有 Key 即不出网（功能降级） |
| 记忆候选抽取二次调用 | 同上 | `auto_extract_enabled`（当前开） | 配置可关 |
| 屏幕截图 | 云视觉链（TJU v3→GLM-4.6V→DeepSeek vision） | 仅显式触发词/`/look` | **无总开关**（仅窄触发软闸）⚠️ |
| 摄像头单帧 | 同上 | 仅显式"看看我" + 插件开 | Quick Tools 可关（fail-closed） |
| 文档摘录 / PDF 页图 / PPTX 页 | 云 LLM/视觉 | 附件/阅读动作 | 面板关闭即无 |
| 视频转录文本 | 云 LLM | 视频链接/本地视频 + 门开 | 门 fail-closed |
| 记忆语义索引 | **本地**（Qdrant 磁盘模式 + fastembed 本地嵌入，infer=False） | — | 零云出口 ✅ |
| 语音播报 | 127.0.0.1:8300 | 默认关 | `voice.enabled` ✅ |
| PageLens 桥 | 127.0.0.1:17321 | 面板/扩展 | 仅回环 ✅（origin 认证弱，已记录） |

红线过滤器（write_guards）只保护"写入记忆"，**不保护"发给 LLM"**——用户粘贴的密钥仍会作为对话上下文出网，发布说明应披露。

### 6.2 用户数据落盘地图

| 数据 | 位置 | 迁移风险 |
|---|---|---|
| 对话/记忆/建议/Bond/Scratchpad | 项目目录 `runtime/companion/` | 高：覆盖安装/杀软隔离即丢失 |
| 向量库/嵌入模型/history.db | `runtime/memory/` | 同上；模型缓存体积需计入安装器 |
| Learning 数据 | `%LOCALAPPDATA%\FireflyAI\learning\`（唯一在用户目录） | 低 |
| Provider 健康状态 | `core/provider_state.json`（源码目录） | 中：应迁 runtime/ |
| 个人配置 | `config/*.json`（gitignored） | 低（不随包） |

### 6.3 依赖声明缺口（requirements.txt 实测 13 项）

- **直接 import 但未声明**：`requests`（screen_vision 等 4 处）、`Pillow`（document_vision 等）——目前靠 mem0ai/fastembed 传递依赖存活，打包即断。
- **硬件链未声明**：`pyserial`（tools/hardware_ports.py 明确依赖）。
- **视频链外部依赖未声明/未文档化**：ffmpeg、ffprobe 可执行文件；faster-whisper / scenedetect（服务侧自带，宿主可选路径）。
- PySide6 / websockets 未锁版本下限。

### 6.4 仓库边界外硬依赖（换机降级表）

| 外部依赖 | 硬编码位置 | 缺失表现 |
|---|---|---|
| `E:\Firefly_AI_Private_Plugins`（4 插件） | `core/plugin_loader.py:30` | Quick Tools 卡片消失 |
| `E:\Firefly_BiliInsight_Service` | `core/bili_insight_client.py:28-61`（env 可覆盖） | B 站链不可用 |
| `E:\AI_Workspace\TJU_Info_Retrieval` | 测试与 adapter 默认值 | 检索不可用 |
| `E:\Firefly_PageLens\voice\voice_module` | voice_config 模板注释 | 语音不可用（默认已关） |
| TJU 校内 API 端点（ai.tju.edu.cn） | `core/providers/catalog.py` | 主力 LLM/视觉链不可用，回退商用 API |

### 6.5 测试与回归证据状态

- 规模：`tests/` 198 个测试文件 + `memory/suggestion/tests/`、`memory/test_memory.py`。
- 项目回归基线记录（2026-09-17）：**13 项已知失败**（8 Learning UI 旧属性引用 + 2 app.py WIP + 3 真实视频环境依赖）+ 4 个 `--ignore` 收集文件；另记录"桥互斥体因停栈暂通过，恢复硬件回 14"（环境敏感）。
- `.pytest_cache/v/cache/lastfailed` 实测现存 **43 条**（stale，含历史失败），**此后无干净全量回归证据**。
- Phase D 在线验收（2026-08-20）：TJU 正常路由与 DeepSeek 双故障回退真实通过；全工作流链 NO-GO 待复测。

### 6.6 工程与打包状态

- Git：63 modified + 384 untracked（0 staged）。关键能力文件未跟踪清单包括：`core/plugin_loader.py`、`core/quick_tools.py`、`core/learning/`（48 文件）、`core/screen_vision/screen/camera.py`、`voice_client/`、`tools/` 桥与 supervisor、`tests/` 大部分、`assets/fonts/`、`assets/ui/`。
- 打包：**无 PyInstaller/spec 或等价管线**；`start_pet.ps1` 依赖本机 `.venv`。
- 历史结论一致：`docs/PHASE_D_RELEASE_REVIEW.md`（"NO-GO for creating a release tag from the current working tree"）与 `Firefly_Release_Preflight_Audit.md`（**RELEASE PREFLIGHT: BLOCKED**）。

---

## 7. Scope Freeze 裁决与 v1.0 前置条件

**裁决：批准按第 5 节三层范围冻结"发布范围"（Scope Definition FREEZE：批准）；拒绝在当前工作区状态下生成发布物（Release Artifact FREEZE：BLOCKED）。**

进入 v1.0 的 P0 前置条件（完成前不得打 tag / 出包）：

1. **工作树固化**：审查并处置 63 modified + 384 untracked（提交 / 明确 ignore），确保 HEAD 可完整重建 Bundled 全部能力。
2. **依赖声明修复**：requirements.txt 补 `requests`、`Pillow`、`pyserial`；视频链外部依赖写入安装/README 文档；锁定 PySide6/websockets 下限。
3. **一次干净全量回归**：处置 13 项已知基线失败（8 Learning UI 属重构性修复；2 app.py WIP 需收敛；3 真实视频属环境依赖需明确 skip 策略）；留存 clean run 证据。
4. **打包管线决策**：PyInstaller 或"自带 venv 的启动器"二选一，并验证 `assets/`（animations/ico/pdfjs）随包完整。
5. **隐私披露与开关**：发布说明载明 6.1 出网矩阵；对 Screen Vision 总开关做出产品决策（补开关或明示设计）。
6. **RecentSessions MOCK 回退移除**（改为空态）。
7. **脱敏模板**：新增 `hardware_devices.example.json`；修正 `.env.example` 漂移（删 DASHSCOPE、补 `TJUTOKEN`）。
8. **`core/provider_state.json` 迁至 `runtime/`**（源码目录不得含运行时可变状态）。
9. **用户数据目录策略**：`runtime/` → `%LOCALAPPDATA%` 迁移，或发布说明明确"便携版、数据随目录"。
10. **对外依赖降级文案**：Optional External 各能力在 UI/README 中给出"未检测到外部组件"的诚实提示（多数已具备，需走查）。

P1（v1.0 后首个补丁窗口）：PageLens Bridge origin 认证；`VideoQa` gate 参数旁路封堵；ProviderHTTPError 上游响应体脱敏；工作流全链在线复认证。

---

## 8. 附录：证据文件索引

- 既往发布审计：`docs/PHASE_D_RELEASE_REVIEW.md`、`Firefly_Release_Preflight_Audit.md`、`Firefly_Release_Freeze_Plan.md`、`V1_FROZEN.md`、`docs/releases/v0.1.0-firefly-demo.md`
- 门控审计：`Quick_Tools_Capability_Gate_Audit.md`、`Video_Analysis_Capability_Gate_Report.md`、`Firefly_Companion_Memory_Boundary_Audit.md`
- 能力报告（抽样）：`Firefly_Memory_M2A_Policy_DryRun_Report.md`、`Firefly_Memory_M3B_*` 系列、`Firefly_Learning_Agent_Phase*`（30+）、`Firefly_Camera_Vision_*`、`docs/Voice_Agent_Integration.md`、`docs/Voice_v131_Bugfix.md`、`Firefly_Scratchpad_v1_Report.md`
- 关键实现：`app.py`、`core/companion_runtime.py`、`core/ai_router.py`、`core/providers/catalog.py`、`core/plugin_loader.py`、`core/screen_vision/*`、`core/video_pipeline.py`、`core/pagelens_bridge.py`、`memory/service.py`、`ui/system_tray.py`、`ui/v2/recent_sessions.py`、`voice_client/`、`tools/firefly_runtime_supervisor.py`
- 配置：`requirements.txt`、`.env.example`、`config/companion.json`、`config/hardware_devices.json`、`config/voice_config.yaml`、`.gitignore`

（审计结束。本文档为唯一写入物，其余全部操作只读。）
