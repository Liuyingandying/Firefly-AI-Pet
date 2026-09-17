# Firefly_AI_Pet — v1.0 发布文件归档清单（Release File Manifest v1）

> 审计日期：2026-09-17
> 审计角色：Release Repository Auditor
> 审计方式：**只读**。未修改代码；**未执行任何 git 状态变更操作**（无 add / commit / stage / restore）；仅使用 `git --no-optional-locks status`、`git ls-files`、`git check-ignore` 等零写入查询与静态阅读。
> 基线：HEAD = `50e73a9`；实测 **63 个已跟踪文件修改 + 763 个未跟踪文件**（`git status` 显示的 ~380 为目录折叠计数）。
> 前提：User Data Migration 已完成——`core/user_paths.py`（未跟踪，待入库）确立规范数据根 `%LOCALAPPDATA%/FireflyAI`，仓库内 `runtime/` 降级为 legacy 迁移源。
> 关联文档：`docs/Release_Scope_v1.md`（Scope Freeze 审计，2026-09-17）

---

## 0. 工作区实测全景

| 区域 | 已跟踪·修改 | 未跟踪文件（展开） | 说明 |
|---|---:|---:|---|
| `tests/` | 17 | 142（含 1 个二进制工件） | 回归面主体 |
| `.pytest_camera_plugins*/` | — | 270 | **pytest 运行残留**（两个目录 × 135） |
| `core/` | 25 | 82 | 其中 `core/learning/` 52 个、`core/scratchpad/` 4 个 |
| `ui/` | 15 | 27 | 其中 `ui/v2/` 控制台 13 个 |
| `MiMo_Desktop_AI_Project_Portfolio/` + zip | — | 18 + 1 | **个人申请材料**，与项目无关 |
| `tools/` | 2 | 11 | supervisor/硬件桥为发布必需 |
| `docs/` | 26（已跟踪） | 10 | 含本清单与 Release_Scope_v1 |
| `_pagelens_ui_verify/` | — | 9 | UI 验证截图（debris） |
| `scripts/` | — | 8 | 数据运维/诊断脚本 |
| `voice_client/` | — | 6 | **整包未跟踪** |
| `memory/` | 7 | 6 | M2/M3B 模块未跟踪 |
| `extensions/` | 0 | 4 | **浏览器扩展 0 跟踪** |
| `config/` | — | 3 | 其中 1 个含设备序列号 ⚠️ |
| `assets/` | — | 3（README） | 字体按许可策略不入库 |
| 根目录 | 3（app.py / state_broker.py / start_pet.ps1） | ~163 | ~155 份阶段/审计报告 .md、~8 张预览 PNG、1 个 zip |
| 其他 | — | 1 | `pytest.ini`（**未跟踪**） |

密钥面核查（`git check-ignore` 实测）：`.env`（line 85）✅、`core/provider_state.json*`（line 143）✅、`config/ui_settings.json` / `sessions.json` / `pet_preferences.json` ✅、`runtime/`（line 32）✅ 均已被 ignore。**例外：`config/hardware_devices.json` 未跟踪且未被任何 ignore 规则覆盖** ⚠️（见 R3）。

---

## A. Include List（Release 必须包含）

判定标准：v1.0 Bundled 能力的源码、测试、默认配置模板、浏览器扩展、启动脚本与治理文档。**全部当前处于"未提交"状态（modified 或 untracked），入库前一个都不存在。**

### A1. 应用源码（含 63 个 modified）

| 路径 | 文件数 | 当前状态 | 备注 |
|---|---:|---|---|
| `app.py`、`state_broker.py`、`start_pet.ps1` | 3 | modified | 组合根 / 状态源 / 启动入口 |
| `core/`（除下述未跟踪专项外）+ `core/learning/` | 82 + 25 modified | **82 全部 untracked** | 重点：`core/learning/` **52 个文件 0 跟踪**（R1）；`plugin_loader.py`、`plugin_api.py`、`quick_tools.py`、`providers/catalog.py`、`providers/status.py`、`scratchpad/`（4）、`capabilities/video_gate.py`、视频链 5 件、`screen_vision/screen/camera.py`、`user_paths.py`（数据迁移规范，必须入库） |
| `ui/` | 27 + 15 modified | **27 全部 untracked** | 重点：`ui/v2/` 控制台 13 个（主 UI 面，R2）；`quick_tools_popover.py`、`scratchpad_window.py`、`scratchpad_drop.py`、`memory_manager.py`、`global_hotkey.py`、`tju_llm_health.py`、PDF 栈 5 件 |
| `memory/` | 6 + 7 modified | untracked：`m2.py`、`m3b.py`、`access_mode.py`、`recall_gate.py`、`history_restore.py`、`suggestion_store.py` | M2A/M3B 决策与门控层 |
| `providers/` | 3 modified | — | deepseek / tju_qwen / zhipu_glm |
| `voice_client/` | 6 | **全部 untracked** | 语音客户端整包（R12） |
| `character/` | 6 | 已跟踪 | 无动作 |

### A2. 测试

| 路径 | 数量 | 当前状态 | 备注 |
|---|---:|---|---|
| `tests/test_*.py`（未跟踪） | 141 | untracked | learning 全系、camera/video、voice、tray、multi-session 等 |
| `tests/`（已跟踪） | 17 | modified | 含 `conftest.py` |
| `pytest.ini` | 1 | untracked | 回归命令依赖；注意其 `norecursedirs` 覆盖缺口（R4） |

### A3. 配置与资源

| 路径 | 状态 | 备注 |
|---|---|---|
| `config/companion.json` | untracked | 已核内容：纯默认策略（memory.write_policy / suggestion 开关与类别白名单），**无个人路径、无密钥**，可作为发布默认模板 ✅ |
| `config/voice_config.yaml` | untracked | 本地回环 server 配置，无密钥 ✅ |
| `config/hardware_devices.example.json` | **尚不存在** | 需新建脱敏模板后随 `config/hardware_devices.json`（真值）一起处理（见 B2/R3） |
| `.env.example` | 已跟踪 | 需修正漂移：删 DASHSCOPE 死配置、补 `TJUTOKEN`（不改代码，发布前文档级修正） |
| `assets/fonts/README.md`、`assets/ui/**` | untracked | 3 个 README；字体二进制按许可策略**不入库**（系统字体兜底） |
| `assets/animations/`、`firefly.ico`、`assets/paper_reader/pdfjs/` | 已跟踪 | 无动作 |

### A4. 扩展与运维入口

| 路径 | 状态 | 备注 |
|---|---|---|
| `extensions/pagelens_bridge/`（manifest.json / background.js / content.js / README.md） | **4 个全部 untracked，目录 0 跟踪** | PageLens 网页能力的随包组件（R6） |
| `tools/firefly_runtime_supervisor.py`、`tools/hardware_ports.py`、`tools/esp32_serial_bridge_poc.py`、`tools/firefly_led_bridge.py` | untracked | 启动链必需（start_pet.ps1 → supervisor）+ 硬件桥 |
| `tools/stop_pet.py`、`tools/simulate_event.py` | modified | 停止脚本需入库 |

### A5. 文档（治理文档必须，阶段报告建议归档）

| 路径 | 状态 | 备注 |
|---|---|---|
| `docs/`（10 个 untracked：Release_Scope_v1、Voice×2、Memory×2、Recent_Sessions×2、 screenshots×3） | untracked | 直接 include |
| 根目录治理文档：`V1_FROZEN.md`、`Firefly_Release_Preflight_Audit.md`、`Firefly_Release_Freeze_Plan.md`、`Quick_Tools_Capability_Gate_Audit.md`、`Video_Analysis_Capability_Gate_Report.md`、`Video_Analysis_Gating_Audit.md` | untracked | 发布治理证据，建议提升为 include |
| 根目录其余 ~150 份 Phase/Audit/Report .md | untracked | **文档存档**：建议单独一个 archive commit 入库（工程历史证据），但不进安装包 |

---

## B. Exclude List（禁止进入发布）

### B1. User local data（用户本地数据）

| 路径 | 处置 |
|---|---|
| `runtime/`（含 conversation/、scratchpad/assets/、memory_records、provider 状态、诊断日志） | 已 gitignore（line 32）✅；User Data Migration 后仅为 legacy 源，**发布物中必须整体排除**；打包器需显式排除以防残留 |
| `%LOCALAPPDATA%/FireflyAI/**` | 迁移后的规范数据根，**天然在仓库外**；任何"复制到仓库内打包"的流程都不得把它带进来 |
| `logs/`（现于 `%LOCALAPPDATA%/FireflyAI/logs`，由 user_paths 定义） | 仓库外；同上 |
| `tests/_unused/learning.sqlite3` | 未跟踪的二进制测试工件；**不得 add**；建议加入 ignore 或删除（删除动作本审计不执行） |
| `docs/screenshots/*.png`（3 张） | 文档引用的截图，可随 docs 入库但**不进安装包** |

### B2. Secrets（凭据与机器标识）

| 路径 | 处置 |
|---|---|
| `.env` | 已 gitignore ✅；打包显式排除 |
| `core/provider_state.json*` | 已 gitignore（line 143）✅；注意它**在磁盘上存在**——打包前显式排除；后续代码层迁移至 runtime/ 属 P1 |
| `config/hardware_devices.json` | ⚠️ **未跟踪且未被 ignore**：含设备 VID/PID、序列号与维修史备注（真值仅存本机，不入库）。**任何 `git add config/` 类批量操作都会把它带进历史**。处置顺序：先补 ignore 规则 → 新建脱敏 `hardware_devices.example.json` → 真值文件留在本地 |
| `config/ui_settings.json`、`config/sessions.json`、`config/pet_preferences.json` | 已 gitignore ✅（注意 ui_settings 现存 pytest 污染值，属本机状态） |
| API Key / token（`TJULLM_API_KEY`、`ZHIPU_API_KEY`、`DEEPSEEK_API_KEY`、`TJUTOKEN`、`ANTHROPIC_*`） | 仅存在于 `.env` / 进程环境，不在任何已跟踪文件中（catalog 仅存变量名）✅；`.env.example` 只含变量名模板 |

### B3. Development only（仅开发工作区）

| 路径 | 数量 | 处置 |
|---|---:|---|
| `.pytest_camera_plugins/`、`.pytest_camera_plugins_final2/` | 270 | pytest 残留 debris；**不入库**；建议 ignore 规则 `.pytest_camera_*/`；并已被 pytest 收集风险波及（R4） |
| `_pagelens_ui_verify/`（9 张验证 PNG） | 9 | UI 验证 debris；不入库，建议 ignore |
| 根目录预览 PNG（`.console_*.png`、`.context_*.png`、`.sidebar_*.png`、`Firefly_UI_Consolidation_P0*.png` 等） | ~8 | 报告预览 debris；不入库 |
| `MiMo_Desktop_AI_Project_Portfolio/` + `MiMo_Desktop_AI_Project_Portfolio.zip` | 19 | **个人申请材料**，与项目无关；不入库；建议移出仓库目录 |
| `tools/camera_capture_*_probe.py`、`tools/experiment_many_tools.py` | 3 | 诊断/实验脚本；保留在仓库 dev 层可选，不进安装包 |
| `tools/test_led_runtime_chain.py`、`test_led_stability.py`、`test_led_tool_hysteresis.py`、`test_led_visual_showcase.py` | 4 | 硬件工具测试（依赖真实设备）；建议随 tools 入库但标注 hardware-required |
| `scripts/`（memory_m1/m11/m15 运维脚本、qcamera 诊断） | 8 | **数据运维脚本**：memory_m11_phase2_reconcile 等会写真实记忆数据；仅开发/运维使用，不进安装包 |

---

## External Capability 标注（随包能力 vs 外部能力）

| 能力 | 标注 | 依据 |
|---|---|---|
| Companion / Memory / Memory Manager / 会话历史 / Scratchpad / PageLens+扩展 / Screen Vision / Learning / Providers / 宠物壳 / voice_client（客户端代码） | **bundled** | 源码全部在本仓（A1–A4 入库后即完整） |
| Quick Tools 壳 + 门机制 | **bundled**（壳）；4 个插件体 **optional external** | 插件本体在 `E:\Firefly_AI_Private_Plugins\`，不在本仓发行边界 |
| Camera Vision / Video Analysis（B 站链）/ TJU Info Retrieval / learning-focus | **optional external** | 依赖仓库外插件与服务（BiliInsight、TJU_Info_Retrieval 工程） |
| Voice Pipeline（TTS/RVC 服务） | **optional external** | 服务在 `E:\Firefly_PageLens\voice\voice_module`，默认 `voice.enabled=false` |
| Hardware / ESP32（Prism + LED 桥） | **optional external** | 桥代码 bundled（tools/），设备与 `hardware_devices.json` 真值为本机私有 |
| DASHSCOPE（`dashscope-qwen`）provider | **unsupported** | catalog 已 deprecated、无代码读取；从 `.env.example` 移除 |
| RecentSessions MOCK_SESSIONS 回退 | **unsupported** | 假数据路径，v1.0 必须移除（Scope 审计 P0-6） |

---

## C. 风险列表

| # | 等级 | 风险 | 处置建议 |
|---|---|---|---|
| R1 | 🔴 高 | `core/learning/` 52 个实现文件 **0 跟踪**——v1.0 学习能力无法从 HEAD 重建，且 schema v4 存储层在内 | 最优先单独 commit 入库；入库前跑 learning 离线测试子集 |
| R2 | 🔴 高 | `ui/v2/` 控制台 13 个文件 0 跟踪——当前主 UI 面不在版本库 | 与 character_conversation_runner（modified）同批入库并交叉审 |
| R3 | 🔴 高 | `config/hardware_devices.json` 未跟踪且**未被 ignore**——批量 `git add config/` 或 `git add .` 会把设备序列号/维修史写进发布历史 | **在任何批量 add 之前**：补 `.gitignore` 条目 + 新建脱敏 example；真值永不入库 |
| R4 | 🟠 中高 | 270 个 pytest 残留文件未 ignore；且 `pytest.ini` 的 `norecursedirs` **替换了默认值**，未覆盖 `.pytest_camera_*`（默认 `.*` 规则已失效）——从根目录跑 pytest 会收集残留目录里的 test 文件（回归基线"4 个 --ignore 收集文件"的根源） | 补 ignore 规则；norecursedirs 增加 `.pytest_camera*`、`_pagelens_ui_verify`、`.*`（配置修正建议，本审计不执行） |
| R5 | 🟠 中 | `MiMo_Desktop_AI_Project_Portfolio`（18 文件 + zip）个人材料混在仓库根目录 | 移出仓库；短期先加 ignore 防误提交 |
| R6 | 🟠 中 | `extensions/` 浏览器扩展 0 跟踪——PageLens 网页理解随包即缺 | 随 PageLens 批次入库 |
| R7 | 🟠 中 | `voice_client/` 6 文件、`pytest.ini`、`config/companion.json`、`voice_config.yaml` 等发布必需件未跟踪 | 分批入库（见 D） |
| R8 | 🟠 中 | 63 个 modified 跨 9 个子系统（companion/memory/screen_vision/ui/tests/providers/tools）未提交——单点大 commit 不可审、不可回滚 | 按 D 的子系统顺序分批 stage，每批跑对应回归 |
| R9 | 🟡 低中 | `scripts/memory_m11_phase2_reconcile.py` 等运维脚本可写真实记忆数据 | 明确标注 ops-only；不进安装包；执行需人工确认 |
| R10 | 🟡 低 | `tests/_unused/learning.sqlite3` 二进制工件与 141 个测试文件混在同一未跟踪面 | add 时按 `tests/test_*.py` + `conftest.py` 白名单式操作，`_unused/` 加入 ignore |
| R11 | 🟡 低 | 根目录 ~150 份报告 .md 与 8 张预览 PNG 混杂 | 报告归档单独 commit；PNG 加 ignore |
| R12 | 🟡 低 | `.env.example` 漂移（含 DASHSCOPE 死配置、缺 `TJUTOKEN`） | 发布前修正模板（文档级变更） |

---

## D. 建议 git staging 顺序（**仅建议，本审计未执行任何 git 操作**）

原则：先堵泄漏面（ignore），再按子系统分批、每批可独立审查与回归；批内 `git add` 使用显式路径白名单，**全程禁用 `git add .` / `git add -A`**。

```
Step 0  堵泄漏面（先于一切 add）
        .gitignore 增补：.pytest_camera_*/ 、_pagelens_ui_verify/ 、
        MiMo_Desktop_AI_Project_Portfolio* 、config/hardware_devices.json 、
        tests/_unused/ 、根目录 *_preview.png / *_Qt_Render.png
        新建：config/hardware_devices.example.json（脱敏）
        验证：git status 中上述路径全部消失

Step 1  工程基线
        pytest.ini + .gitignore（Step 0 产物）
        回归：pytest --collect-only 干净（无 camera 残留目录被收集）

Step 2  core 基础设施
        core/providers/{__init__,catalog,status}.py、core/runtime_bus.py、
        core/runtime_state_aggregator.py、core/zcode_state_poller.py、
        core/pet_state_resolver.py、core/pet_activity_controller.py、
        core/user_paths.py、core/visual_state_filter.py、
        core/extension_api.py、core/plugin_api.py

Step 3  记忆层
        memory/{m2,m3b,access_mode,recall_gate,history_restore,suggestion_store}.py
        + memory/ 7 个 modified（service/repository/records/mem0_adapter/...）
        回归：tests/test_memory_*.py + test_write_guards

Step 4  Companion 核心
        core/companion_*、conversation_*（modified）+ app.py + state_broker.py
        + ui/character_conversation_runner.py（modified）
        回归：test_companion_*、test_conversation_*、test_character_*

Step 5  Screen Vision
        core/screen_vision/ 10 modified + core/screen_vision/screen/camera.py
        + ui/image_vision_worker.py、ui/image_ocr_worker.py（如未跟踪见清单）
        回归：test_screen_vision_*、test_capture_semantics

Step 6  插件系统 / Quick Tools
        core/plugin_loader.py、core/quick_tools.py、ui/quick_tools_popover.py
        + core/settings_manager.py（modified）
        回归：test_plugin_loader、test_quick_tools_*、test_camera_vision_plugin_integration

Step 7  PDF / PageLens 栈
        core/{page_context,paper_context,pdf_ambient_context,pdf_text_hit_test,
        pdf_viewport_context,pdf_visual_region}.py + document_router/pagelens_bridge（modified）
        + ui/ 5 个 pdf_* + paperlens_reader + pagelens_panel/explain_box（modified）
        + extensions/pagelens_bridge/ 4 件
        回归：test_pagelens_*、test_paperlens_*、test_document_*

Step 8  Scratchpad
        core/scratchpad/ 4 件 + ui/scratchpad_window.py、ui/scratchpad_drop.py

Step 9  v2 控制台与 UI 收尾
        ui/v2/ 13 件 + ui/ 其余 untracked（ambient_status、chat_markup、global_hotkey、
        memory_manager、tju_llm_health、runtime_state_debug）+ ui/ 15 modified
        回归：test_ui_v2_*、test_learning_entry_ui（预期暴露已知 8 项 UI 失败）

Step 10 视频链
        core/{bili_video_reader,bili_insight_client,video_reader,video_study,
        video_time_parser,video_frame_vision,video_vision}.py、
        core/capabilities/ 2 件 + core/video_pipeline.py（modified）
        + ui/v2/video_card.py（已随 Step 9）
        回归：test_bili_video_reader（真实服务缺失应 skip）

Step 11 Learning
        core/learning/ 52 件（整目录）
        回归：tests/test_learning_*.py（离线子集）

Step 12 Voice
        voice_client/ 6 件 + config/voice_config.yaml
        回归：test_voice_*

Step 13 硬件
        tools/{firefly_runtime_supervisor,hardware_ports,esp32_serial_bridge_poc,
        firefly_led_bridge}.py + tools/stop_pet.py、simulate_event.py（modified）
        + tools/test_led_*（标注 hardware-required）+ start_pet.ps1（modified）

Step 14 测试
        tests/ 141 个 test_*.py + conftest.py 等 17 modified
        （白名单：tests/test_*.py、tests/conftest.py；不 add tests/_unused/）

Step 15 文档
        docs/ 10 件（含 Release_Scope_v1.md 与本清单）
        + 根目录治理文档 7 件（V1_FROZEN、Preflight、Freeze Plan、Gate audits×2、Video×2）
        + assets 3 个 README
        + （可选、独立 commit）根目录 ~150 份阶段报告存档

Step 16 收口
        git status 应仅剩：ignore 后的 debris（0 条）、B1/B2 本地件、
        未决定的可选件。全量回归 → 处置 13 项基线失败 → 打 tag。
```

每步 commit 建议形如 `feat(learning): import core/learning subsystem (52 files)`，并在 commit body 记录该步回归结果。**Step 0 未完成前不得执行任何后续步骤。**

---

## 附录：本审计的数字可复现性

- `git --no-optional-locks status --porcelain` → 63 个 ` M` + 折叠 `??` 条目
- `git ls-files --others --exclude-standard | wc -l` → 763
- `git ls-files core/learning | wc -l` → 0（对照 `ls-files --others` → 52）
- `git check-ignore -v .env config/hardware_devices.json ...` → 前者命中 line 85，**hardware_devices.json 未命中**

（清单结束。本审计唯一写入物为本文件；未执行任何 git 状态变更操作。）
