# Firefly Learning Bridge — Learning Return Channel v0.1 报告

日期：2026-09-27
范围：Z Code Agent ↔ Firefly 双向学习交互通道。未触碰 teach-mcp 业务逻辑、Memory Ownership 原则、Resource Manifest、curriculum 算法、mastery、renderer；未恢复 Z Code GUI 作为学习界面；launch.log 仍仅为诊断。

---

## 架构（已真实验证的双向回路）

```
Firefly（UI / 身份 / 收集回答）
  │ launch_learning_mode(course_id, action[, answer])
  │  ├─ bridge/learning_context.json（+ task_id，action 含 answer）
  │  ├─ bridge/learning_action.json（answer 载荷：question_id + student_answer 原文）
  │  ├─ bridge/task.json（task_id / pending→running / pid）
  │  └─ [LEARNING_CONTEXT_WRITTEN] → [ZCODE_TASK_INJECT] → [ZCODE_PROCESS_STARTED]
  ▼
Z Code headless task（node zcode.cjs -p <bootstrap> --cwd <workspace>）
  │ firefly-learning skill（编排）→ teach-mcp（状态/出题/判分/记录）
  │
  │ ▲ 回程：bridge/result.json（原子写，schema v1 校验）
  ▼
Firefly Result Watcher（QTimer 400ms 轮询，task_id 匹配）
  │  ├─ [LEARNING_RESULT_WAIT] → [LEARNING_RESULT_RECEIVED] → [LEARNING_MESSAGE_DISPLAYED]
  │  ├─ answer 时：[LEARNING_ANSWER_SUBMIT] → [LEARNING_ANSWER_RESULT]
  │  └─ 超时：一次性提示"学习代理仍在处理中……"（不宣告失败，降频继续等）
  ▼
CompanionConsole chat（append_assistant 原样展示，chat LLM 不改写）
  └─ pending question 期间：用户下一条消息 → answer 通道（不进 TJU LLM）
```

## 1. Agent 结果如何返回 Firefly？

**`<course>/bridge/result.json`**（schema：`docs/learning/learning_result.schema.json`；模块：`learning/learning_result.py`）。skill 在每轮"交给用户"的停点（出题/讲完/审核门/错误）原子写入（tmp → fsync → os.replace）。字段：`task_id/course_id/action/status(ok|waiting_review|error)/message_type(lesson|question|review|info|error)/display_text/question{question_id,text,options}/opaque_refs{session_id}/progress_snapshot{source:"teach-mcp"}`。`additionalProperties:false`，mastery 类权威字段出现即 `RESULT_FORBIDDEN_FIELD`。

**迭代记录**：第一版协议写"任务完成时写 result"，真实 headless run 中 agent 出题后停下等待、不写文件（它认为任务没结束）。已改为"每到达交给用户的停点就是本轮最后一步，必须先写 result"，并在 bootstrap prompt 中直接给出 result.json 的**绝对路径**。第三轮真实 run 成功产出合规 result。

## 2. 是否仍依赖 launch.log？

**否。** launch.log 仅诊断。正式通道 = result.json；Firefly 的 watcher 从不解析 launch.log。

## 3. 用户在哪里看到下一题？

**CompanionConsole 聊天窗口**。watcher 校验通过后，`BridgeLearningSession` 用 `display_lines()` 渲染（display_text + 题干 + A/B/C 选项行）并经 `result_ready` 信号 → `console.show_learning_result()` → `chat.append_assistant` 原样显示。聊天 LLM 不参与改写。

## 4. 用户回答如何进入 teach-mcp？

pending question 期间，用户在聊天框输入（如 `B` 或"我觉得是B，因为…"）：

1. `console._send` 的 **bridge 路由门**（位于 tutor/legacy/普通管道之前）调 `session.handle_incoming_text(text)`；
2. session 构造 answer headless task：`launch_learning_mode(course_id, "answer", answer={question_id, student_answer})`，载荷原样落 `bridge/learning_action.json`；
3. skill 读取载荷 → teach-mcp `evaluate_answer` + `record_result`（服务端成对执行）→ 继续教学 → 写 result.json；
4. watcher 收到 → chat 显示判分 + 下一题。

真实 E2E 实测：用户答 `B` → teach-mcp 判 `RC-M01 correct=0`（该题正确答案为 A）、mastery 1.0→0.5、session 推进 quiz/0.4，result 返回讲评 + 新题 `RC-E02`。

## 5. Firefly 是否参与判题？

**否。** `learning_action.json` 只含用户回答原文（测试断言其字段集合中无任何 verdict/mastery 字段）；对错、mastery、misconception 全部由 teach-mcp 决定（`test_11_firefly_records_answer_verbatim_only` + memory guard 覆盖）。

## 6. pending question 如何防串课程？

pending interaction 绑定 `learner_id + course_id + session_id + question_id`（全部为 teach-mcp 原值或 Firefly 既有身份，零自造）。防护：任何新 launch（切课程）先清空 pending；answer launch 复检 manifest.learner_id（`LEARNER_MISMATCH` 拒绝）；result.json 的 `course_id/task_id` 必须匹配当前 task。测试：`test_9_course_isolation`、`test_10_answer_launch_rechecks_learner`。

## 7. 删除 Z Code 会话是否仍可恢复？

**是。** 回路状态机不依赖任何 Z Code 会话：pending 仅是 Firefly 内存中的展示态；学习事实在 teach-mcp。删会话后点「继续学习」→ 新 headless task → `resume_learning(learner_id)` 恢复（v0.1 与本次均实证）。

## 8. Firefly 重启是否可继续？

**是。** pending 不持久化也不需要：重启后点「继续学习」→ headless resume → result.json 重建 pending（`test_13_restart_rebuilds_pending_from_resume_result`）。真正状态始终在 teach-mcp。

## 9. 是否实现真正双向学习交互？

**是，且已真实闭环（非模拟）**：

```
resume run（task-07fb68407e）→ result.json：question RC-M01（options 三项，session_id 原值）
用户答 "B" → answer run（task-446771ad10）→ result.json：讲评（B 错，正确 A=τ=1秒）
           → teach-mcp results: RC-M01 correct=0；mastery 0.5；出下一题 RC-E02
```

## 10. 用户是否完全无需操作 Z Code？

**是。** 不打开 Z Code GUI/终端/launch.log，不说"开始学习"。点「继续学习」→ 聊天窗自动出现教学内容与题目 → 聊天框直接回答 → 判分与下一题自动出现。

## Phase 14 测试（`tests/test_learning_return_channel.py`，15 项）

覆盖任务书 16 项要求：schema 校验、原子写（tmp 无残留）、task_id 匹配、stale 拒绝、question 展示渲染、answer 路由、普通聊天不误入、pending 时进 learning、课程隔离、learner 隔离（LEARNER_MISMATCH）、Firefly 不判题（载荷字段集合断言）、result 禁 mastery 字段、restart 重建、超时一次性提示且晚到结果仍被接受、malformed 结构化报错、agent failure 状态照常展示。全量桥接面回归 **82 passed**。

## 交付物清单

| 类别 | 文件 |
|---|---|
| Schema | docs/learning/learning_result.schema.json、docs/learning/learning_action.schema.json（learning_context.schema.json 增 answer/task_id） |
| 协议模块 | learning/learning_result.py、learning/task_state.py、learning/result_watcher.py、learning/bridge_session.py |
| 既有更新 | launcher.py（task_id/task.json/answer 载荷/bootstrap 规则 9-10+绝对路径）、learning_context.py（answer+task_id）、bridge_dialog.py（course_launched 信号+文案）、console.py（session 注入/show_learning_result/_send 路由门/退出词）、app.py（session 单例+信号装配）、SKILL.md（answer 分派+result 协议+标记） |
| 测试 | tests/test_learning_return_channel.py（15）+ 既有 67 全绿 |

## 已知边界（v0.1 明确不做）

- watcher 超时后仅提示一次并降频继续；无取消/重试按钮（后续 UI 增强）；
- `--surface desktop`（官方 headless 桌面呈现口）未启用——headless + chat 展示已满足闭环，留作后续观察；
- Skill 标记行（[FIREFLY_SKILL_START] 等）依赖模型遵守，属尽力而为；权威判据是 result.json + teach-mcp 状态。

**结论：双向学习交互通道建立完成——继续学习 → 看题 → 回答 → 判分 → 下一步，全程 Firefly 内闭环、零 Z Code 操作、teach-mcp 唯一权威。按要求停止，不扩展。**
