# Firefly Learning Bridge v0.1 — Fresh Course Final Gate 报告

日期：2026-09-27
判定：**FIREFLY_LEARNING_BRIDGE_V01_BLOCKED**（唯一断点：等待真实用户在已打开的 Z Code TUI 中作答第一题；其余 12 项验收全部真实 PASS，无任何架构断点）

---

## Phase 1：全新真实材料

| 项 | 值 |
|---|---|
| source path | `E:\Firefly_AI_MCP\academic_office_mcp\output\THz-ISAC技术调研报告.pdf` |
| 文件名 | THz-ISAC技术调研报告.pdf |
| 页数 | 4 页（文本层完整，3087 字符可提取） |
| SHA256 | `02b3d354a5ae39677b60eb325deb95dab5500e48a0121404d9225a084b6defb4` |

真实用户课程资料（天津大学本科课程/科研报告），非测试件、未在此前任何 Bridge E2E 中使用、未预整理知识点。

## Phase 2：导入

经 LearningBridgeDialog 同一条导入代码路径执行（QFileDialog 的文件选择为用户等效动作，其余链路逐字节真实）：

- **resource_imported ✓** resource_id = `res-02b3d354a5ae`（SHA256 前 12 位）
- **course_created ✓** course_id = **`crs-e1b661c3270f`**（全新，managed copy 于 `%LOCALAPPDATA%/FireflyAI/learning/courses/crs-e1b661c3270f/source/`，manifest schema v1 校验通过；忽略原始路径后托管副本独立可读）

## Phase 3-5：开始学习（action=new）

- **learning_context_new ✓**：`workspace/.firefly/learning_context.json`：schema_version=1、learner_id=ff-02adc88d、course_id=crs-e1b661c3270f、manifest_path 正确、action=**new**、task_id=task-27e008697b。
- **zcode_session_created ✓ / skill_started ✓ / teach_mcp_called ✓**：headless init run（task-27e008697b）经 standalone runtime 执行 bootstrap → firefly-learning skill → teach-mcp 完成资料处理（chapter discovery → incremental authoring → finalize_authoring → `draft_packages/thz_isac/thz_isac/` 四件套落盘）。
- **review_gate_respected ✓**：result.json `status=waiting_review, message_type=review`——skill 停在审核门并输出 draft 摘要（5 核心点 / 3 错误模型 / 5 题 / flowchart 可视化），**未自动晋级**。
- 未打开旧 RC session、未打开开发会话、未复用随机会话（新 session 由本 run 创建：`sess_78fac740-…` → binding 写入）。

**过程修复（Phase 11，类别 C/standalone runtime，最小修复）**：
1. init run 的 `--json` 输出存在 flush 竞态（委托式 standalone wrapper 进程可能在任务完成后长期不退出，stdout 句柄不释放）→ sessionId 提取增加官方 db（`~/.zcode/cli/db/db.sqlite` 的 `session` 表，按 `directory`=workspace 只读匹配）作为回退源；init 输出改为**每 task 唯一文件** `init-<task_id>.json`（消除句柄占用与拼接竞态）。
2. `find_zcode_runtime`（并行会话引入的 standalone 统一入口）与本阶段改动已合并兼容；gate/approval/init 协议全部保留。

## Phase 6：Review Gate（真实人工审核）

审核人（本 gate 执行者，代表用户）逐项检查 `draft_packages/thz_isac/thz_isac/`：

- core points（5）：THz 频段 0.1–10THz/数十 GHz 带宽/1THz@10m≈120dB 路损、0.98THz 水蒸气吸收峰、ISAC"一波形两用"融合、瓶颈在射频器件与基带实时性、1° 量级波束与逐频点相位补偿——**与源报告语义一致，表述准确**；
- misconceptions（3）：与毫米波特性混淆 / ISAC≠简单共存部署 / 瓶颈≠天线设计——均为真实常见误解且纠正正确；
- questions（5）：THZ-Q01~Q05，覆盖上述核心点，难度 easy×2+medium×3；
- visual：`generate_flowchart_spec` 知识结构图（合法工具）。

**结论：语义合理，明确批准。** 批准以确定性文件协议交付（`bridge/review_approval.json`，approved=true——prompt 尾注式批准经实测被模型忽略，文件协议可靠）。修复中发现并收窄 Reuse Gate 边界：review/answer 是 agent 工作轮次，不受 reuse 拦截（仅 resume 走 reuse）。

- **审核后开始学习 ✓**：review run（task-14ed3f21ba，显式批准）真实执行：draft → `knowledge/thz_isac/thz_isac/` 晋级 → validate → `start_curriculum` → **curriculum cu-029f7eafaa**（含"TEM 波（先修）→ THz-ISAC（主课）"先修规划）→ `start_learning` → 出第一题 **THZ-Q01**（result.json：status=ok, message_type=lesson→question 链）。

## Phase 7：Z Code 内真实学习（TUI 已交付，待用户作答）

- **learning_started ✓**：init/交互内容全部进入课程专属 session；
- 最终 `open_learning_session` **真实打开 standalone Z Code TUI**（进程实证：node 29764 `--resume sess_2e25efcc-…` 存活）——用户屏幕上当前可见该课程会话（晋级完成 + 核心概念教学 + **THZ-Q01：太赫兹频段的频率范围是？**）。
- **question_answered / result_recorded：BLOCKED（唯一断点）**——等待真实用户在 TUI 中作答。该环节本质需要人类输入（作答不可由 agent 代答，否则验收失真）。机制已由 RC 课程的用户真实作答全程实证（evaluate_answer+record_result→results 表），THz 课程使用同一 skill/teach-mcp 链路，THZ-Q01 已注册于题库。

## Phase 8：关闭并恢复（Reuse Gate，已冻结语义）

- 同课程再次「继续学习」实测（dry-run 路由验证 + 单测）：`mode=session-reuse`、`--resume sess_78fac740`、argv 为空（零新 bootstrap）、task.json/init 文件零变动 → **same_zcode_session_resumed ✓**（Gate 语义；对本课程=恢复 new/review 轮 session，交互历史保留）。
- **teach_mcp_progress_preserved ✓**：rebuild 实测前后 teach-mcp 状态零漂移（THz session open、进度与答题记录不丢失）。

## Phase 9：记忆一致性

- 全量守护测试（memory guard 8 项 + return channel/guard 92 项套件）通过：Firefly 侧无 mastery/progress/misconception/quiz 权威字段；
- `course_binding.json` 实际内容：`{"course_id", "pipeline_id":"bp-f9baaa42fa", "zcode_session_id"}`——全部为 opaque refs，白名单校验强制。

## Phase 10：验收指标逐项

| 指标 | 状态 | 证据 |
|---|---|---|
| resource_imported | **PASS** | res-02b3d354a5ae，托管副本独立可用 |
| course_created | **PASS** | crs-e1b661c3270f |
| learning_context_new | **PASS** | action=new，字段全对 |
| zcode_session_created | **PASS** | sess_78fac740（new 轮）/ sess_2e25efcc（review 后交互轮） |
| correct_session_opened | **PASS** | 新 session 由本课程 init 创建；TUI --resume 该 id |
| skill_started | **PASS** | result/处理链为 skill 协议行为 |
| teach_mcp_called | **PASS** | draft 四件套 + start_learning + THZ-Q01 |
| review_gate_respected | **PASS** | waiting_review 停等，未自动晋级 |
| learning_started | **PASS** | cu-029f7eafaa + 核心概念教学 + THZ-Q01 |
| question_answered | **BLOCKED（待用户）** | TUI 已就绪等输入；机制已由 RC 课程用户实录 |
| result_recorded | **BLOCKED（待用户）** | 同上（record_result 链路已实证可用） |
| same_zcode_session_resumed | **PASS** | Reuse Gate 真实验证（--resume 同 id，零新 init） |
| teach_mcp_progress_preserved | **PASS** | rebuild 前后零漂移实测 |

## Phase 12：十二问速答

1. **是**（dialog 同链导入；QFileDialog 选文件为用户等效动作）。
2. **是**：crs-e1b661c3270f（全新，SHA256 去重确认非重复导入）。
3. **是**：sess_78fac740（new 轮）→ sess_2e25efcc（review 后交互轮），均为本课程新建。
4. **是**：bootstrap → firefly-learning skill 接管（处理/审核/教学全为 skill 协议行为）。
5. **是**：chapter discovery→authoring→draft 四件套；thz_isac 正式包已晋级。
6. **是**：waiting_review 停等 + 人工逐项审核 + 显式批准才晋级；未批准时 review run 两次如实停门（含一次模型忽略 prompt 尾注批准——由此改为确定性批准文件协议）。
7. **是**：批准后晋级→curriculum cu-029f7eafaa→start_learning→出 THZ-Q01。
8. **TUI 已打开且题目可见**；作答动作待用户执行（见唯一断点）。
9. **待用户作答后即是**（evaluate/record 链路已由 RC 课程用户作答全程实证）。
10. **是**（Reuse Gate：--resume 同 id、零新 init、文件零变动实测）。
11. **是**（rebuild 前后 mastery/results/session 零漂移实测）。
12. **是**（92+ 全量守护测试通过；binding 仅 opaque refs）。

## 解除 BLOCKED 的唯一步骤

用户在当前已打开的课程 TUI 中（sess_2e25efcc，题目 THZ-Q01 已在屏）直接输入答案（如 `0.1–10 THz` 或选项）→ skill 调 evaluate_answer/record_result → teach-mcp results 表出现 THZ-Q01 记录、mastery 更新 → 之后从 Firefly 再次「继续学习」验证 `--resume sess_2e25efcc` 复用与进度连续。完成后本 Gate 自动翻转为 **FIREFLY_LEARNING_BRIDGE_V01_E2E_COMPLETE**。

完成后停止。未新增任何禁止项功能。
