# Firefly Learning Bridge v0.1 — 交付报告

日期：2026-09-26 ~ 2026-09-27
分支：plugins-integration（未提交，遵循任务纪律）
配套文档：`docs/firefly_learning_bridge_v01_audit.md`（Phase 1 审计）、`docs/learning/MEMORY_OWNERSHIP.md`（Phase 2 冻结契约）

---

## 架构总览（已验收的分层）

```
Firefly
├─ learner identity            （learning/identity.py + settings learning.learner_id）
├─ managed learning resources  （learning/resource_manager.py + manifest.json，托管副本）
├─ course selection            （LearningBridgeDialog 托盘入口，launch_learning_mode）
└─ learning preferences        （settings：last_course_id 等；契约允许偏好，禁学习状态）

       ↓ Learning Context（workspace/.firefly/learning_context.json + env FIREFLY_LEARNING_CONTEXT，每次 launch 原子重写）

Z Code + firefly-learning Skill
└─ orchestration only          （SKILL.md 薄协议：发现 context → 读 binding → 按 action 分派 → 回写 opaque id）

       ↓ MCP                    （start_learning / resume_learning / start_curriculum / generate_question / evaluate_answer / record_result …）

teach-mcp
├─ session                     （learning_sessions，session_id 服务端生成）
├─ mastery                     （knowledge_mastery）
├─ misconception               （misconception_records）
├─ curriculum                  （curriculum_id + knowledge/ 正式包）
├─ quiz history                （results）
└─ learning progress           （session.phase/progress/next_step）
       ↘ 返回边：opaque IDs（pipeline_id / curriculum_id / session_id）原样回写
         course_binding.json —— Firefly 仅用于编排状态推导，不是学习状态
```

---

## 0. 交付物清单

| 类别 | 文件 |
|---|---|
| 协议文档 | docs/learning/MEMORY_OWNERSHIP.md（冻结）、docs/firefly_learning_bridge_v01_audit.md |
| Schema | docs/learning/resource_manifest.schema.json、docs/learning/learning_context.schema.json |
| Firefly 模块 | learning/ 包：`_storage.py`、`resource_manager.py`、`learning_context.py`、`course_binding.py`、`bridge_state.py`、`identity.py`、`launcher.py`、`skill_installer.py`、`bridge_dialog.py`、`skill_source/SKILL.md` |
| 既有文件增量 | core/settings_manager.py（learning.learner_id 持久化）、ui/system_tray.py（「学习模式」菜单项）、app.py（open_learning_bridge 单例入口） |
| Z Code Skill | `~/.zcode/skills/firefly-learning/`（由 learning.skill_installer 从仓库源安装） |
| 测试 | tests/test_learning_bridge.py（25 项，覆盖 Phase 13 的 17 项要求 + Phase 16 场景）、tests/test_learning_bridge_memory_guard.py（Phase 12 守护，8 项）——**33 项全绿** |
| PoC 脚本 | .tmp_learning_bridge_poc/（gen_pdf.py、poc_step1~5） |

禁止事项自查：未重写 teach-mcp 任何代码（零改动）；未在 Firefly 维护 mastery/misconception/progress；未在 Z Code Memory 存学习进度；模型/客户端零 ID 生成；未引入新 Agent 框架（纯文件协议 + teach-mcp 工具编排）。

---

## 1. 学习资源如何导入？

`ResourceManager.import_pdf(pdf_path, learner_id)`（learning/resource_manager.py）：
校验（存在/PDF 后缀/非空/≤200MB）→ 流式 SHA256 → **全库哈希去重**（命中即返回既有 course，`deduplicated=True`）→ 生成 `course_id` → 复制进 `<数据根>/learning/courses/<course_id>/source/`（文件名清洗 + 冲突加后缀）→ 生成 manifest → 原子写 `manifest.json`（tmp + os.replace）→ 建目录 `bridge/`、`workspace/.firefly/`。
数据根复用既有 `core/user_paths.py` 的 `UserDataPaths.learning`（`%LOCALAPPDATA%/FireflyAI/learning/courses/`），未新建第二数据根。UI 侧经 `LearningBridgeDialog._on_import`（QFileDialog 选 PDF）调用。

**PoC 实证**：导入 `RC电路充放电讲义.pdf` 得 `crs-d91e981f5a92`；随后**删除原始 PDF**，托管副本仍完整可读可处理（后续全部流程均只依赖托管副本）。

## 2. Resource Manifest 最终 schema 是什么？

`docs/learning/resource_manifest.schema.json`（v1，`additionalProperties: false`，Python 侧 `validate_manifest` 双重强制）：

```json
{
  "schema_version": 1,
  "course_id": "crs-<12hex>",
  "title": "...", "learner_id": "ff-<8hex>",
  "created_at": "...Z", "updated_at": "...Z",
  "resources": [{
    "resource_id": "res-<sha256前12位>",
    "type": "pdf",
    "relative_path": "source/<name>.pdf",
    "original_name": "...", "sha256": "<64hex>", "imported_at": "...Z"
  }]
}
```

约束落实：ID 由工具生成（course_id 随机 12hex、resource_id=sha256 前 12 位确定性）；模型不生成 ID；相对路径 + resolve 时强制留在 course 目录内（越界即 `RESOURCE_PATH_ESCAPES_COURSE`）；sha256 去重；manifest 无任何学习状态字段（出现即 `MANIFEST_FORBIDDEN_FIELD`）。

## 3. Firefly 如何启动 Z Code？

`launch_learning_mode(course_id, action)`（learning/launcher.py）→ headless 模式 spawn：

```
node.exe C:\Users\FAJ\AppData\Local\Programs\ZCode\resources\glm\zcode.cjs
  --prompt "<点名 firefly-learning skill + context 路径 + action>"
  --cwd "<course>/workspace"
```

CLI 参数来自实测 `node zcode.cjs --help`（zcode 0.16.9：`-p/--prompt`、`--cwd`、`--attach`、`-c/--continue`、`--resume`），非猜测。解析顺序：env `FIREFLY_ZCODE_CLI`/`FIREFLY_NODE` → `%LOCALAPPDATA%` 默认安装路径探测；未找到返回结构化错误（`ZCODE_CLI_NOT_FOUND`/`NODE_NOT_FOUND`），课程资源零损伤（场景 A 实测）。Windows 下 `DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP`，输出落 `bridge/launch.log`。交互式模式（UI 实际按钮）优先 `wt.exe -d <workspace> node zcode.cjs` 开 TUI，无 wt 时 `CREATE_NEW_CONSOLE` 兜底。与既有 `ui/agent_launcher.launch_zcode`（仅拉起桌面 GUI、无 workspace 语义）并存，桥接专用路径走 CLI。

## 4. Z Code 如何发现 Learning Context？

三重冗余、不依赖剪贴板：
1. 环境变量 `FIREFLY_LEARNING_CONTEXT`（QProcess/subprocess env 注入）；
2. `<cwd>/.firefly/learning_context.json`（workspace 内固定路径，skill 的 cwd 兜底读法）；
3. prompt 文本内直接给出 context 绝对路径。

Context 由 Firefly **每次 launch 原子重写**（build → validate → tmp+replace），schema 见 `docs/learning/learning_context.schema.json`：`schema_version/source/learner_id/course_id/manifest_path/action(new|resume|review)/created_at` + 可选 opaque `curriculum_id/pipeline_id`（仅当 binding 已有时原样回显）。`additionalProperties: false`，出现 mastery/progress 类字段即 `CONTEXT_FORBIDDEN_FIELD`。

## 5. Learning Skill 具体负责什么？

`~/.zcode/skills/firefly-learning/SKILL.md`（仓库源 `learning/skill_source/`，installer 安装）。**薄协议层**，只做编排：发现并校验 context → 读 binding → 按 action 分派（new=teach-mcp 处理资料后停在 WAITING_REVIEW；review=无状态定位 draft（pipeline package_paths + 全库扫描 validate_package + manifest 标题匹配）→ 展示摘要 → 用户明确批准后才复制 draft→knowledge → start_curriculum → 转 resume；resume=learning_status/resume_learning 取真实状态继续教学）→ 收尾只把 teach-mcp 返回过的 opaque id 写进 binding。
PDF 解析、curriculum 算法、出题判卷、可视化渲染**全部仍由 teach-mcp 完成**；skill 自身零算法、零状态存储。

## 6. teach-mcp 负责什么？

唯一权威来源：learner profile、learning_sessions、mastery、misconceptions、quiz results、curriculum、next_topic、teaching progress（`state/session.db` 五张表 + `knowledge/` 正式包 + `draft_packages/` 草稿 + `state/book_pipeline/`）。skill 教学中出题必须 `generate_question`、判卷必须 `evaluate_answer`+`record_result` 服务端成对执行。本阶段对 teach-mcp **零代码修改**。

## 7. Firefly Memory 负责什么？

按冻结契约 `docs/learning/MEMORY_OWNERSHIP.md`：
允许 = learner_id（`learning.learner_id`，一次生成终身不变）、长期学习偏好、最近 course_id（`learning.last_course_id`，既有键）。
禁止 = mastery / misconception_records / quiz_history / curriculum progress / session progress / 当前题目 / 章节完成度。
memory_records.json（主 Memory）不承担任何学习职责。实测 PoC 后 pet_preferences.json 仅含 `learning.learner_id` 与 `learning.last_course_id` 两个 learning 键。

## 8. mastery/progress 唯一来源在哪里？

**teach-mcp**（state/session.db + 包数据）。Firefly 是 **read-through display**：v0.1 UI 只显示编排状态（binding/标记推导），不显示、不缓存任何学习数值；一切学习数值展示只能来自 teach-mcp 当次查询。
守护测试（test_learning_bridge_memory_guard.py）：AST 扫描 learning/ 全部模块禁止写入四类权威字段（dict 键/关键字参数/下标存储）；三类协议文件注入这些字段必须被校验器拒绝；settings 只允许两个 learning 键。实测：course 目录全文扫描四类字段零命中。

## 9. learner/course/session 三个 ID 由谁生成？

| ID | 生成方 | 实证值（PoC） |
|---|---|---|
| learner_id | **Firefly**（identity.ensure_learner_id，一次生成持久化） | `ff-02adc88d` |
| course_id | **Firefly Resource Manager** | `crs-d91e981f5a92` |
| session_id | **teach-mcp**（服务端 `{learner}-{时间戳}-{uuid4}`） | `ff-02adc88d-20260927-010222-11f5` |

pipeline_id（`bp-6f543ba228`）/curriculum_id（`cu-8c8adee42e`）同为 teach-mcp 生成，binding 只原样保存。
强制手段：identity 对已存非法 ID 抛 `LEARNER_ID_CORRUPT`（拒绝静默重置）；launcher 校验 manifest.learner_id 与当前 learner 一致（`LEARNER_MISMATCH`，跨学习者拒绝）；test_14 以 AST 证明 learning/ 代码零 session_id 引用；SKILL.md 硬规则第 1 条禁止模型生成任何 ID。

## 10. 删除 Z Code 对话后能否继续学习？

**能，已真实验证。** 恢复链 = Firefly 重生成 context + binding（opaque ids）+ teach-mcp 数据库，三者之外无隐藏状态。PoC 中 Z Code 进程退出（headless 每次运行即退出，等价于会话终结）后再次 launch：Z Code 产生**全新会话**（新 sess_*），skill 经 `resume_learning(learner_id)` 恢复**同一个** teach-mcp 会话（`ff-02adc88d-20260927-010222-11f5`，phase concept→quiz、progress 0.2 原样），此前答题结果（`results: RC-E01 correct=1`、`mastery 1.0`）完整保留，教学从 quiz 阶段继续出下一题。测试 test_15 另以单元级模拟 workspace 全删后重新 launch 亦通过。

## 11. Firefly 显示的学习进度是否与 teach-mcp 一致？

一致——通过**不显示第二份值**的结构性保证：v0.1 Firefly UI（LearningBridgeDialog）只显示编排状态文案（新资料/处理中/待审核/就绪/可继续），这些值由 binding + 标记推导，不是学习事实；任何 mastery/progress 数值在 Firefly 侧无存储、无缓存（第 8 条守护测试 + 第 15 项验收：Firefly 侧扫描零命中，teach-mcp 侧同刻查询 mastery=1.0/progress=0.2 唯一存在）。不存在可能发生漂移的第二权威值。

## 12. 是否完成真实“开始学习→关闭→继续学习”PoC？

**是（2026-09-27 00:45–01:15，全程真实链路，未 mock）**：

| 步骤 | 结果 |
|---|---|
| 导入 PDF（真实数据根+真实 settings） | course `crs-d91e981f5a92`；删除原文件后仍可用 |
| launch new（产品 API 真实 spawn Z Code headless） | skill 读取 context；teach-mcp pipeline 处理；产出 draft 包（6 概念/3 错误模型/5 题/1 可视化，validate 通过）；binding=`{pipeline_id}`；**停在 WAITING_REVIEW**（Firefly 侧 derive=WAITING_REVIEW） |
| review gate | 第一轮 review 运行未能定位 draft 时**如实报错拒绝晋级**（门未失守）；协议补充 draft 无状态发现规则后，重跑 review：展示摘要→显式批准→复制 `draft_packages/control_system/rc`→`knowledge/control_system/rc`（validate 通过；因 curriculum 需 ≥2 包按 teach-mcp 约束同时晋级了既存 `first_order_response`）→ `start_curriculum` 得 `cu-8c8adee42e` → binding=`{pipeline_id, curriculum_id}` |
| start_learning + 第一题 | teach-mcp 生成 session_id；出 RC 充电概念题 |
| 答题 | 经 launch resume 提交答案 B → `evaluate_answer` + `record_result` → results 落库（correct）、mastery 更新、session 推进 quiz/0.2 |
| **关闭 → 继续学习** | Z Code 已退出；重新 launch resume → 全新 Z Code 会话 → `resume_learning` 恢复同一 learner/session → 答题结果/掌握度原样 → 继续出下一题 |

### PoC 揭示并已处置的事实

1. **teach-mcp `start_book_pipeline` 对该 PDF 章节发现接受 0 章**（QPdfWriter 文本层兼容性问题；accepted=0/ambiguous=0 → pipeline FAILED）。skill 协议允许的降级路径（teach-mcp 直接 authoring：start_authoring→submit→finalize）产出同等 draft，接口未阻塞，teach-mcp 零改动。新课程建议使用文本层更标准的 PDF 来源。
2. **skill 协议缺口修复**：action=review 原依赖 binding.pipeline_id 定位 draft；降级路径下 pipeline FAILED 时找不到 draft。已改为无状态发现（pipeline package_paths ∪ 全库扫描 validate_package + manifest 标题匹配），并已实测闭环。
3. **后台 spawn 竞态（环境项，非协议缺陷）**：PoC 脚本 spawn 后立即退出时，detach 子进程偶发被测试 harness 的进程树清理连带终止；生产 Firefly 为常驻进程不受影响（PoC 中改用驻留父进程后 100% 复现成功）。

### Phase 16 失败场景覆盖

| 场景 | 覆盖方式 | 结果 |
|---|---|---|
| A Z Code 未安装 | 单元（env/LOCALAPPDATA 重定向 + which 置空） | `ZCODE_CLI_NOT_FOUND`/`NODE_NOT_FOUND` 结构化错误；manifest 复核完好 |
| B teach-mcp 不可用 | skill 硬规则 + 文本断言 | “学习服务不可用”即停，禁止伪造/替代存储 |
| C manifest 损坏 | 单元 | `FILE_MALFORMED`/`MANIFEST_INVALID` 结构化报错 |
| D 重复导入 | 单元 + PoC | sha256 去重返回既有 course，无重复副本 |
| E draft 未审核 | 单元 + 真实 PoC | WAITING_REVIEW 锁定 action=review；第一轮 review 主动拒绝无凭据晋级 |
| F session 不存在 | 单元 + PoC | 无 open session 且有 curriculum → skill 走 start_learning（ID 服务端生成）；无 curriculum → 提示先 new/review |

### 遗留与边界（v0.1 明确不做）

- 多设备/云同步、OCR、手机端、UI 重做（任务禁止项）；
- 交互式 TUI 路径（wt.exe）未做真实点击验收（headless 路径全链已真实验证；两者共用同一 context 发现协议）；
- `ui/v2` 既有学习模式（Firefly 自有链）未与本桥合并——两者并存，互不改动；
- 既存测试基线失败（与本桥无关，stash 对照确认）：`test_system_tray.py::test_g_shell_shutdown_removes_tray`（app.py WIP `zcode_poller` 缺失）、`test_learning_entry_ui.py` 3 项（未跟踪 WIP 测试 vs 已改 console.py）。

**结论：Learning Bridge v0.1 六协议（Resource Manifest / Learning Context / Memory Ownership / Course Binding / 身份 / Handoff）全部落地，33 项测试全绿，真实“开始→关闭→继续”PoC 通过。按任务要求在此停止，不扩展。**
