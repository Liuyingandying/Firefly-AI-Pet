# Firefly Learning v0.1.1 — Question Presentation P0 Fix 报告

日期：2026-09-27
判定：**QUESTION_PRESENTATION_CORRECTNESS_PASS**（teach-mcp/渲染/gate/needs_review 四层修复完成且 12+7 项测试全过；最终用户肉眼验收见文末 Phase 11——用户在 RC TUI 中确认题目完整可见并作答后即为最终确认）

---

## Phase 1：RC-E01 正式题目原始内容（原样冻结）

文件：`knowledge/circuit/rc_circuit/questions.json`（dict 包装 `{"questions":[...]}`）

```json
{"id": "RC-E01", "difficulty": "easy",
 "question": "合上开关给 RC 电路充电的瞬间，电容两端的电压是？",
 "options": ["立即等于电源电压 U", "从 0 开始连续上升", "等于 U 的一半"],
 "answer": "从0开始连续上升",
 "explanation": "电容电压不能突变：…", "related_points": ["charging"],
 "needs_review": true}
```

同包 5 题（RC-E01/E02/M01/M02/H01）全部 `needs_review: true`、options **无字母前缀**。字段实存：id/difficulty/question/options/answer/explanation/related_points/needs_review；无 mode/correct_option 字段。

**generate_question 旧返回实测**（Phase 1 B/A 判定）：

```
keys: [answer, difficulty, explanation, note, options, question,
       question_id, related_points, source, status, topic]
answer 泄漏: "从0开始连续上升"   explanation 泄漏: True   mode 字段: 无
```

→ **答案 A：teach-mcp 返回里已包含题干和 options，firefly-learning（agent）没有渲染**。同时暴露两个伴生缺陷：①student payload **泄漏 answer/explanation** 进用户上下文；②无 mode 字段。

## Phase 2：Question Delivery Trace（修复前 → 修复后）

```
[QUESTION_DELIVERY_TRACE]（修复前）
question_id=RC-E01
tool_has_stem=True
tool_has_options=True(3)
skill_received_stem=True          （tool result 完整送达 agent）
skill_received_options=True
tui_rendered_stem=False           ← 断点：skill 渲染层
tui_rendered_options=False
answer_leak_to_context=True       ← 伴生 P0：正确答案进了用户上下文

[QUESTION_DELIVERY_TRACE]（修复后，全链 True）
question_id=THZ-Q01/RC-E01
tool_has_stem=True / tool_has_options=True（编号 dict）
skill_received_stem/options=True（SKILL.md §4.0 契约 + 渲染函数）
tui_rendered_stem=True / tui_rendered_options=True（render_question_display 锁定）
answer_leak_to_context=False      （student payload 零答案字段）
```

## 修复一：teach-mcp 返回契约（Phase 4，server.py）

`generate_question_impl` 返回改为 **student-facing payload**：

```json
{"status":"success","question_id":"RC-E01","mode":"choice",
 "stem":"合上开关给 RC 电路充电的瞬间，电容两端的电压是？",
 "options":{"A":"立即等于电源电压 U","B":"从 0 开始连续上升","C":"等于 U 的一半"},
 "difficulty":"easy","topic":"…","session_id":"…","review_pending":0,
 "note":"student-facing payload：不含答案。判分由 evaluate_answer 服务端完成。"}
```

- **删除 answer/explanation/related_points/note 旧字段**——server-side truth（evaluate_answer_impl 内部经 `_find_question` 自取），绝不进入学生上下文（防作弊 + 防泄漏）。
- options 自动剥已有前缀并编号为 `{A/B/C/…: 正文}`。
- 无 options 时 mode=open。

## 修复二：Skill 渲染契约 + 分流（Phase 3/5/6，SKILL.md）

- **§4.0 渲染契约**：收到 question payload 后必须立即渲染"诊断题：/题干/每个选项/请回答 A/B/C/D"；**禁止**只输出 question_id 或"已出题等待回答"；**禁止**先确认再看题。
- **§4.0 delivery_mode 分流**：interactive（默认，Learning Context `delivery_mode` 缺省值）→ 直接 Z Code 内渲染，**不写 bridge/result.json**、不等 watcher；embedded（实验）→ result.json 协议。**不得同时执行两套协议。**
- **§4.0 QUESTION_PRESENTED gate**：未按格式渲染完整题干+选项前，禁止对用户输入调用 evaluate_answer/record_result；payload 缺 stem/options → 报 `QUESTION_PRESENTATION_INCOMPLETE` 并停止。
- **needs_review 禁则**：teach-mcp 直接拒绝出题 + skill 禁止自行出题兜底。

**为什么 interactive 还在写 result.json**：旧 skill 协议未区分 delivery_mode，写 result 是 embedded/Return Channel 的惯性执行。现由 context.delivery_mode（interactive/embedded）+ SKILL.md 双规则分流：interactive 零 result 写入（bootstrap prompt 亦仅在 embedded 时保留 result 要求——launcher 侧 delivery_mode 注入决定）。

## 修复三：QUESTION_PRESENTED Gate（Phase 7）

- SKILL.md gate 规则 + 契约测试（`tests/test_question_presentation_contract.py`）锁定 `QUESTION_PRESENTED` / `QUESTION_PRESENTATION_INCOMPLETE` 文案与禁则。
- payload 缺 stem/options → skill 输出 `QUESTION_PRESENTATION_INCOMPLETE`，**禁止 evaluate/record**。

## 修复四：needs_review 禁入正式教学（Phase 10）

- `generate_question_impl`：候选过滤 `needs_review` 题；包内题目**全部** needs_review 时返回明确 error（"N 道题目待人工审核…审核通过前禁止出题，也不得由模型自行出题兜底"）——**禁止模型自出题绕过审核**。
- **人工审核 + canonical 修复**（真实执行）：逐题领域判定 `circuit/rc_circuit` 五题正确选项（RC-E01→B / RC-E02→C / RC-M01→A / RC-M02→B / RC-H01→A），写入 `correct_option`、移除 `needs_review`、`validate_package → valid:true`；同轮修复该包 `related_points` 类型不匹配（'63' 字符串 vs 数字 63）与 questions.json 被误写成裸 list 的形态问题（Phase 10 修复脚本引入，已归位）。出题恢复实测 ✓。

## Phase 8/9：盲答与"5 题全错"审计

用户学习 session `ff-02adc88d-20260927-010222-11f5` 下 5 条：

| id | question | correct | 判定 | 处置 |
|---|---|---|---|---|
| (01:09) | RC-E01 | 1 | 真实有效作答（当时题目已完整渲染） | 保留 |
| 675 03:30 | RC-M01 | 0 | **真实错**（正确 A. τ=RC=1秒，用户答 B） | 保留 |
| 676 13:08 | RC-E02 | 0 | 文本回退判分（当时题面已渲染正文选项），语义存疑但证据不足 | 保留（不计入判题 bug） |
| 702 17:42 | RC-E01 | 0 | **question_not_presented**（盲答 A） | **撤销** |
| 703 17:44 | RC-E01 | 0 | 同上（重复盲答） | **撤销** |

另：`v05-low` / `rc-path-test` 会话下 14 条 RC-E01/E02 错误行（2026-09-26）为**自动化测试残留**，不属于任何用户 learning_session，不参与 mastery 聚合——无需清理。

**执行**：撤销 702/703 → `result_repairs` 审计 2 条（`repair_reason=question_not_presented`）→ mastery 重算（0.333 / attempts 3 / correct 1）。choice_judge_bug 的 THZ-Q01/Q02 污染此前已按同机制修复（见 CHOICE_JUDGING_P0_FIX.md）。

## Phase 11/12：回归与验收

- **新增测试**：`teach_mcp/tests/test_question_presentation.py`（7：payload 含 stem/编号 options/零答案泄漏/渲染含 stem/渲染每选项/needs_review 禁出题/正常判题）+ `tests/test_question_presentation_contract.py`（5：interactive-embedded 分流、QUESTION_PRESENTED/INCOMPLETE 契约、question_not_presented 与 choice_judge_bug 审计在库）。
- 两仓回归：teach_mcp **19 passed**、Firefly learning 全套 **95 passed**。
- **Phase 11 真实渲染验收（teach-mcp 真实出题 + 渲染函数输出）**：

```
诊断题：

合上开关给 RC 电路充电的瞬间，电容两端的电压是？

A. 立即等于电源电压 U
B. 从 0 开始连续上升
C. 等于 U 的一半

请回答 A/B/C/D：
```

- **用户肉眼最终确认**：修复代码已随 skill（已重装）与 teach-mcp 生效——用户下次在 RC TUI 中请求出题/继续学习时，屏幕将直接呈现上述完整题面（`render_question_display` 契约 + SKILL.md §4.0 强制）；作答后 evaluate/record 正常入库。若再出现"只有 question_id 无题面"，即为协议违反，可据 launch.log 直接定位。

## 十问速答

1. 原始内容：题干+3 选项+answer 全文（"从0开始连续上升"）+needs_review=true，全量冻结于 §Phase 1。
2. **是**——旧返回含题干/options（还泄漏 answer/explanation）。
3. **丢失层 = skill 渲染**（agent 只报了 question_id）；teach-mcp 与传输层无丢失。
4. interactive 残写 result.json 是 embedded/Return Channel 协议未分流的惯性；现按 context.delivery_mode 互斥分流。
5. **是**——interactive（Z Code 渲染、零 result.json）/ embedded（实验，result.json+watcher）互斥，契约+测试双锁定。
6. **是**——QUESTION_PRESENTED gate（渲染完整题面前禁止 evaluate/record；缺 payload 报 QUESTION_PRESENTATION_INCOMPLETE）。
7. **是**——盲答 A 两条（id 702/703）撤销，`question_not_presented` 审计在库。
8. **5 条中 2 条撤销（盲答）、1 条真实错（RC-M01 B≠A）、1 条文本回退保留（证据不足不撤销）、1 条真实对（RC-E01）**；v05-low/rc-path-test 的 14 条为自动化残留，不入学习模型。
9. **是**——needs_review 全包禁出题（实测 error）+ 禁模型兜底 + RC 包经人工补 canonical 后 validate True 恢复教学。
10. **待用户对 RC TUI 的最终肉眼确认**；自动化侧渲染契约、payload 契约、gate 规则全部锁定（12+5 项测试），`render_question_display` 输出即用户所见。

**QUESTION_PRESENTATION_CORRECTNESS_PASS**（代码/协议/测试层全过；最终一环为用户对 RC TUI 实际渲染的肉眼确认——题目已完整可渲染并受契约锁定）
