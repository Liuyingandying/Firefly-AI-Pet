# Firefly Learning Bridge v0.1.1

**版本状态：`FIREFLY_LEARNING_BRIDGE_V011_FROZEN`**
**发布门禁：`CHOICE_JUDGING_CORRECTNESS_PASS` + `LEARNING_CONTEXT_ROLLOVER_PASS` + Fresh Course Final Gate（11/13 PASS，剩余项为用户 TUI 真实作答）**

本文档是 v0.1.1 的**唯一总览入口**。历史阶段报告保留于本目录，仅作过程记录。

---

## 1. Architecture

```
Firefly（入口/身份/资源/Context/Binding）
   │  launch_learning_mode(course_id, action)
   ▼
Standalone Z Code TUI（课程专属可交互会话）
   │  firefly-learning Skill（薄编排协议）
   ▼
teach-mcp（唯一学习事实权威）
```

- **Firefly**：learner_id / course_id / managed resources / 学习模式入口 / Learning Context / zcode_session_id binding。
- **Standalone Z Code TUI**：真正的交互课堂；firefly-learning Skill Host。
- **teach-mcp**：session、mastery、misconception、quiz history、curriculum、progress、pending question 的**唯一权威**。Firefly 与 Z Code 会话均不保存第二份学习状态。

## 2. Ownership（冻结）

| 层 | 拥有 | 禁止 |
|---|---|---|
| Firefly | learner_id、course/resource id、manifest、binding（opaque refs）、学习入口 UI | mastery/progress/misconception/quiz 的任何权威副本 |
| Z Code | 会话承载 + skill 编排 | 生成任何 ID、保存学习事实 |
| teach-mcp | 全部学习事实 | — |

## 3. Requirements

- Firefly_AI_Pet（PySide6）、node ≥ 18、Standalone Z Code TUI runtime（v3.14.3，commit `29628c9acdb81b703bbd4080c207a0e7ce5e276e`；本机验证路径示例 `E:\AI_Workspace\ZCode-Standalone\zcode`，仅文档示例）。
- Z Code 通过 `FIREFLY_ZCODE_RUNTIME`（指向 standalone `bin/zcode.mjs` 或 Windows 启动脚本）或 PATH 上的 standalone 命令定位；`FIREFLY_ZCODE_CLI` 可覆盖桌面内置 CLI；`FIREFLY_CONTEXT_LIMIT` 可覆盖 context 上限（默认 200,000，当前验证 runtime/model 的配置依据）。
- teach-mcp 已注册于 Z Code MCP 配置。

## 4. NEW Flow

PDF → `ResourceManager.import_pdf`（SHA256 去重、托管副本、manifest）→ 新 course_id → 「开始学习」→ headless bootstrap（`-p --json --cwd <workspace>`）→ skill：chapter discovery → incremental authoring → `finalize_authoring` → **Human Review Gate（waiting_review 停等）** → 用户批准（review_approval.json）→ 晋级 `knowledge/` → `start_curriculum` → `start_learning` → 出题 → binding.zcode_session_id = init run 的 `--json` sessionId → 自动打开课程专属 TUI。

## 5. Resume Flow

「继续学习」→ **Session Reuse Gate**：

- `binding.zcode_session_id` 存在且 `zcode_session_exists()`（只读查 Z Code 自身 session db）通过 → **直接 `--resume <id>`**（零新 init，task.json/init 文件零变动）。
- session 丢失/不可恢复 → **REBUILD**：重新 bootstrap init（新 sessionId）→ skill 调 `resume_learning` 从 teach-mcp 恢复真实进度 → binding 更新。teach-mcp 进度零漂移（实测）。
- Context Budget 前置检查（见 §7）：ROLLOVER_REQUIRED 时不 reuse，走 rollover init。

## 6. Choice Judging

- choice 题 schema 携带 canonical **`correct_option`**（显式字段 / answer 单字母 / answer 字母前缀，deterministic 提取）。
- 学生答案确定性规范化（`b/B/b./B./选B/答案是B/我选B` → 字母；自然语言长句不误抓）。
- 判题 = `normalized_student == correct_option`，**零 LLM**；文本回退剥离字母前缀比较正文。
- `record_result` 仅透传 evaluator 的 correct，无二次判定。
- 错误判分可修复：`result_repairs` 审计表 + 撤销 + 修复后重放（`repair_reason=choice_judge_bug`）。

## 7. Context Budget

- 官方数据源：Z Code `model_usage` 表最近一次请求的 `input_tokens`（≈当前会话上下文占用）。
- 阈值：`<70% NORMAL` / `70–85% WARNING` / `>85% ROLLOVER_REQUIRED`（默认 limit 200K——当前验证 runtime/model 的配置依据，可经 `FIREFLY_CONTEXT_LIMIT` 调整）。
- ROLLOVER 触发时：不 `--resume` 旧会话，改走 rollover init（最小引用 `rollover_snapshot.json`：old session、old usage、learner/course、teach_mcp_session_id、pending_question_id、teach_refs——**不含 mastery/progress 文本、不复制聊天历史**）→ 新 sessionId 写 binding → 自动打开新 TUI → `resume_learning` 恢复位置（沿用 pending question，不重复计分）。用户文案："学习会话已整理，继续当前课程。"

## 8. Human Review

`bridge/review_approval.json`：`{"schema_version":1,"task_id":…,"course_id":…,"approved":true,…}`。skill 的 review 流程**先读批准文件**：approved=true 直接执行晋级（复制 draft→validate→start_curriculum→binding 写 curriculum_id→转 resume）；文件缺失则展示 summary 并等待明确批准。未批准绝不晋级。

## 9. Runtime Installation（Standalone Z Code TUI）

1. 构建/获取 standalone Z Code（本验证基于 official v3.14.3，commit `29628c9acdb81b703bbd4080c207a0e7ce5e276e`）。
2. 布局要求：`<runtime>/bin/zcode.mjs`（或 Windows `zcode.cmd`）与 `<runtime>/agent/zcode.cjs` 并存；内置 `node.exe` 可选（无则用系统 node）。
3. 设置 `FIREFLY_ZCODE_RUNTIME` 指向 `bin/zcode.mjs`（或确保启动脚本在 PATH）。
4. 确保 `~/.zcode/cli/config.json` 注册 teach-mcp；`FIREFLY_ZCODE_CLI` 可覆盖桌面内置 CLI（默认自动探测桌面安装）。
5. Firefly 侧无硬编码路径；runtime 缺失时报 `ZCODE_TUI_RUNTIME_MISSING`。

## 10. Testing Evidence

- Firefly learning 全套回归：**153 passed / 4 failed（全部为历史基线，与本桥无关）**。
- Return Channel + delivery + reuse：return_channel 15 / delivery 10（含 reuse gate 3、rollover 文案 1）/ dialog runtime 3 / entry wiring 5。
- teach-mcp：**12 passed**（全量）；THZ-Q01/Q02 重放锁死：raw=b→true、raw=C→true。
- 真实 E2E：RC 课程全链（含用户 TUI 作答→record→mastery 推进→同 session 恢复）；THz 课程 NEW→Review→晋级→curriculum→start_learning→出题→TUI 交付（作答待用户）。
- 状态修复：2 条 choice_judge_bug 错误记录撤销+重放（results/mastery 已一致，审计表留痕）。

## 11. Post-release Hotfix（v0.1.1 增补，已验证）

### Context Budget / Session Rollover
- 官方数据源：Z Code `model_usage` 表最近一次请求的 `input_tokens`（≈当前会话上下文占用）。
- 当前验证基准 200K（`FIREFLY_CONTEXT_LIMIT` 可配置）；70%/85% 双门限。
- 触发 ROLLOVER：不 `--resume` 旧会话，改走 rollover init（`rollover_snapshot.json` 最小引用：old session、usage、learner/course、teach_mcp_session_id、pending_question_id、teach_refs——零 mastery/progress 文本、零历史复制）→ 新 sessionId 写 binding → 自动打开新 TUI → `resume_learning` 恢复位置（pending question 沿用，不重复计分）。用户文案："学习会话已整理，继续当前课程。"
- 实录：old `sess_78fac740`（usage 220,870 ≈ 111%）→ new `sess_74576537`（137,821 → NORMAL）。

### Question Presentation
- `generate_question` 返回 **student-facing payload**（question_id/mode/stem/options 编号 dict/difficulty/session_id），**零 answer/explanation 泄漏**（server truth 由 evaluator 内部自取）。
- skill 渲染契约：收到 payload 必须立即渲染题干+全部选项+作答提示；禁止只报 question_id。
- **QUESTION_PRESENTED gate**：未渲染完整题面前禁止 evaluate/record；payload 不完整报 `QUESTION_PRESENTATION_INCOMPLETE`。
- **needs_review 禁入正式教学**：全 needs_review 包拒绝出题并禁止模型兜底。
- 盲答污染修复：`question_not_presented` 撤销入 `result_repairs` 审计（RC-E01 ×2）。
- interactive/embedded 按 `delivery_mode` 互斥分流：interactive 零 result.json 写入（文件系统实测验证）。

## 12. Technical Debt（只记录，不修）

1. 学习 TUI 加载 11/13 MCP；ZCode 无 session 级 MCP profile——MCP schema 使新 session 起步约 136–138K tokens（baseline 即 ~69% of 200K）。
2. standalone Z Code 为本机构建的官方 v3.14.3 runtime；官方发布 Windows CLI/TUI 后可替换。
3. `zcode_session_exists` 读取 ZCode SQLite 内部 session 表；官方 query API 出现后再迁移。
4. 历史知识包 16/86 题中 66 题标 `needs_review`（answer 无字母前缀形态，判分走文本回退）——待人工补 correct_option。
5. Z Code TUI 内部 tool trace 未做 learner-friendly 隐藏。
6. 教学节奏可优化为 micro lesson。

## 13. Version Status

**`FIREFLY_LEARNING_BRIDGE_V011_FROZEN`**

阶段报告索引：`FIREFLY_LEARNING_BRIDGE_V01_REPORT.md`（Bridge 本体）→ `FIREFLY_LEARNING_ENTRY_WIRING_REPORT.md` → `FIREFLY_LEARNING_DIALOG_RUNTIME_REPORT.md` → `ZCODE_CLI_PROVIDER_AUDIT.md` → `FIREFLY_ZCODE_TASK_INJECTION_REPORT.md` → `FIREFLY_LEARNING_RETURN_CHANNEL_V01_REPORT.md`（embedded，experimental）→ `FIREFLY_RETURN_RUNTIME_DELIVERY_REPORT.md` → `ZCODE_INTERACTIVE_SESSION_AUDIT.md` → `FIREFLY_INTERACTIVE_ZCODE_LEARNING_REPORT.md` → `FIREFLY_FRESH_COURSE_FINAL_GATE.md` → `CHOICE_JUDGING_P0_FIX.md` → `LEARNING_CONTEXT_ROLLOVER_V011.md` → 本文档。
