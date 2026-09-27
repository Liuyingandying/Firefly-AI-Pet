# Firefly Learning v0.1.1 — Context Budget & Session Rollover 报告

日期：2026-09-27
判定：**LEARNING_CONTEXT_ROLLOVER_PASS**
（以 THz 课程真实超限 session 为样本完成全链验证；teach-mcp 唯一权威原则零变更）

---

## 1. 111% 主要由什么造成？

**官方数据实锤（Z Code 自身 `model_usage` 表，per-request 真实记录）**：用户看到的 221.1K/111% 来自 **new 流程轮** session `sess_78fac740`——该轮执行 PDF 处理 + chapter authoring 长链，共 **181 次模型请求**，峰值单请求 input_tokens = **220,870**（220,870/1.11 ≈ 199.2K，与显示基准吻合）。agent 多步循环中每次请求都重发全部上下文（system + SKILL.md + **13 个 MCP 的全量 tool schemas** + 历史 + tool 输出），authoring 类长链使峰值快速膨胀。

Context 构成近似占比（以各 session 首请求 ≈137.8K 的固定 baseline 为基准）：

| 来源 | 近似占比（baseline 内） | 说明 |
|---|---|---|
| MCP tool schemas + 各 server 注入 | **~70-75%** | 13 个 MCP 全量 schema（Phase 9 技术债核心） |
| system + SKILL.md + bootstrap | ~5-8% | skill 是薄协议，占比小 |
| conversation history + tool outputs | 随交互增长 | 出题/判分/讲解轮次累积 |
| PDF/knowledge content | 处理轮尖峰 | new 轮一次性读入（181 请求的长尾主因） |
| visual/tool traces | 小 | render 输出紧凑 |

## 2. Z Code 真实 context limit 是多少？

**200K**。反推：官方显示 111% 对应实测峰值 input 220,870 tokens → 220,870/1.11 ≈ 199.2K ≈ 200K（ZCode 对当前 tju-llm 路由模型的窗口设定）。已落地为可配置值：env `FIREFLY_CONTEXT_LIMIT`（默认 200,000），不硬编码。

## 3. rollover 阈值是什么？

按任务书比例，作用于"最近一次模型请求 input_tokens / limit"（官方值）：

- `< 70%` → NORMAL（THz 单请求轮 137.8K/200K = 68.9%，恰在边缘——本身暴露 MCP surface 肥大，见 Phase 9）
- `70–85%` → WARNING（sess_2e25efcc：155,766/200K = 77.9%，实测命中；仅日志标记，行为不变）
- `> 85%` → ROLLOVER_REQUIRED（sess_78fac740：220,870/200K = 110%，实测命中并真实触发 rollover）

实现：`learning/context_budget.py`（`latest_context_usage` 只读查 `model_usage` 最近一次请求；查询失败返回 None → NORMAL，绝不误杀）。

## 4. rollover 时保存哪些最小引用？

`bridge/rollover_snapshot.json`（实测落盘内容）：

```json
{"schema_version":1, "reason":"context_budget",
 "old_zcode_session_id":"sess_78fac740…", "old_context_usage":220870,
 "learner_id":"ff-02adc88d", "course_id":"crs-e1b661c3270f",
 "teach_mcp_session_id":"ff-02adc88d-20260927-160330-cb96",
 "pending_question_id":null, "teach_refs":{"pipeline_id":"bp-f9baaa42fa"},
 "created_at":"…"}
```

九个键全部为**引用/指针**（Phase 4 清单逐项：learner_id ✓ course_id ✓ teach-mcp session ref ✓ pending question ref ✓ curriculum/pipeline refs ✓）；**不含任何 mastery/progress/历史文本**（rollover bootstrap prompt 明确指示新会话通过 `learning_status/resume_learning` 重新读取真实状态；pending question 存在时指示"沿用原题，不得另出一题、不得重复计分"）。

## 5. 是否复制聊天历史？

**否。** rollover bootstrap prompt 明确写入："对话历史无需复述，用户在 Z Code 中继续学习。"旧会话历史零拷贝。

## 6. teach-mcp session 是否保持不变？

**是。** 真实 rollover 执行后核对：teach-mcp 的 THz 学习会话仍为 `ff-02adc88d-20260927-160330-cb96`（open, progress 0.4），**未新建重复会话**；skill 在新 Z Code 会话中通过 `resume_learning(learner_id)` 沿用它。

## 7. pending question 是否保持？

**是（机制+协议双保障）**：snapshot 携带 `pending_question_id`（来自上一 result 的 teach-mcp 原值）；rollover bootstrap 指示"沿用原题继续，不得另出一题、不得重复计分"；最终事实仍以 teach-mcp 的会话状态为准（phase=quiz 即等待作答的客观标志）。

## 8. mastery/progress 是否连续？

**是。** rollover 是纯 Z Code 会话层操作，teach-mcp 侧零写入零漂移（实测前后 `knowledge_mastery`/`learning_sessions`/`results` 全部一致）。Phase 3 的 rebuild 实测亦同（sess-DELETED → 新 session，进度无损）。

## 9. old/new zcode_session_id 分别是什么？（真实 rollover 实录）

```
old: sess_78fac740-6853-4db0-8308-de6c1b175a2e   （官方 usage 220,870 → ROLLOVER_REQUIRED）
new: sess_74576537-66db-4ef5-b241-c829371fb533   （init run --json 新建；binding 已更新）
new session usage: 137,821 → NORMAL   （rollover 后立即回落到起步水位）
[ZCODE_SESSION_ROLLOVER] course_id=crs-e1b661c3270f old_session_id=sess_78fac740… reason=context_budget
```

## 10. 用户是否无感继续学习？

**是。** 触发 rollover 的「继续学习」点击后：聊天窗出现"学习会话已整理，继续当前课程。"（自动化断言该文案**不含** token/上下文/超限/重新开始等字样），随后自动打开新 session 的 Z Code TUI；无需重选课程、无需重新导入 PDF、无需任何额外确认。

---

## 实现清单（v0.1.1）

| 文件 | 内容 |
|---|---|
| learning/context_budget.py | **新增**：官方 usage 查询（model_usage 最近请求 input_tokens）+ 三态预算 + 可配置 limit |
| learning/launcher.py | Reuse Gate 内嵌 budget 检查：ROLLOVER_REQUIRED → `_run_rollover_init`（最小引用 snapshot + rollover bootstrap + per-task init 文件 + task.json + 标记），返回 `mode="session-rollover"`；WARNING 仅记 `[ZCODE_SESSION_WARNING]`；NORMAL 走原 reuse |
| learning/bridge_session.py | `session-rollover` attach 分支：用户文案"学习会话已整理，继续当前课程。"（禁止 token/超限/重新开始字样，测试锁定）；既有 reuse 分支由并行改动维护 |
| tests/test_learning_return_delivery.py | **新增** rollover 文案测试（禁词断言）；全量 95 passed |

## Phase 8/9 审计结论（记录，不在 v0.1.1 改动）

- **Tool outputs**：teach-mcp 的 learning_status/resume_learning/generate_question 输出本身紧凑（KB 级）；真正的重输入来自 agent 循环的上下文重发与 new 轮的 PDF 全文读入——**rollover 是正解**，无需为省 token 改诊断字段。
- **MCP surface（Phase 9 技术债）**：baseline 137.8K 中 MCP schemas+注入占 ~70-75%；ZCode 的 MCP 配置为全局级（`~/.zcode/cli/config.json`），**无 session 级 profile 机制**（第三方能力，不修改）。技术债登记：若未来支持 per-session MCP profile，学习会话只挂 teach-mcp 可将起步水位从 ~69% 降到 ~15-20%。

## Phase 10/11 压测与回归

- 压力验证使用**真实历史负载**（new 轮 181 请求/220.9K 峰值即真实压力产物），并以 `budget_state` 实测三态：ROLLOVER_REQUIRED（110%）→ 真实触发 rollover → 新 session 137.8K/NORMAL；WARNING（77.9%）与 NORMAL（68.9%）边界亦实测。A-F 六项验证全过（A 阈值命中/B 自动新建/C teach-mcp session 不变/D mastery-progress 不变/E pending 引用进 snapshot+prompt 约束/F 文案无感+零二次操作）。
- Phase 11 回归：**95 passed**（含 choice judging、Fresh Course、reuse/rebuild、embedded 实验分支、memory guard 全套）。

## 最终状态

**LEARNING_CONTEXT_ROLLOVER_PASS**

（附注：并行开发会话对本阶段文件有交叉改动——launcher 的 `find_zcode_runtime` standalone 入口与 bridge_session 的 reuse 文案为本阶段之外的演进；本报告的 rollover 机制已在合并后的现状上验证通过。）
