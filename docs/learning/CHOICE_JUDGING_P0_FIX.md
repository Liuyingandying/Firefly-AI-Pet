# teach-mcp Choice Judging P0 Fix 报告

日期：2026-09-27
判定：**CHOICE_JUDGING_CORRECTNESS_PASS**（THZ-Q01 raw=b 与 THZ-Q02 raw=C 均以修复后 evaluator 真实重放 correct=true；污染记录已撤销重建；mastery/progress 已一致）

---

## 根因链（Phase 1/2 实测，非猜测）

**Q1：b 是否原本已经被转成 B？**

**没有。** `_normalize`（server.py:53）的规则是 NFKC → 减号统一 → 剥标点（`.`、`、`、空格等）→ **lower**。raw=`b` 归一化后是 `"b"`（小写），raw=`B` 也是 `"b"`。用户观察到的"b 被规范化成 B"来自 evaluate 输出里 `student_answer` 字段回显的原始/展示文本，**判题用的归一值是小写**。

**Q2：B 为什么仍然判 false？**

修复前 choice 分支的比较完全基于**答案全文**，从不提取选项字母：

```python
ans = _normalize(q["answer"])        # "B. 0.1–10 THz" → "b011–10thz"（剥点/空格、小写）
stu = _normalize(student_answer)     # "b" → "b"
for opt in q["options"]:             # "b" vs "a011ghz"/"b01110thz"/… 全不相等 → 不命中
    if stu == _normalize(opt): ...
if correct is None:
    correct = (stu == ans) or (ans in stu)   # "b"≠"b011–10thz"；长串 in 单字母 = False
```

学生答案的**字母形态**与 `answer` 字段的**"字母. 文本"全文形态**之间不存在任何 canonical 对齐——单字母输入在旧逻辑下**必然 False**（与大小写无关，`B`、`C` 同理）。

**Q3：正确答案原先实际存成什么结构？**

正式知识包 `knowledge/thz_isac/thz_isac/questions.json`（原样冻结）：

```json
{"id": "THZ-Q01", "difficulty": "easy",
 "question": "太赫兹频段的频率范围是？",
 "options": ["A. 0.1–1 GHz", "B. 0.1–10 THz", "C. 24–100 GHz", "D. 3–6 GHz"],
 "answer": "B. 0.1–10 THz",
 "explanation": "…", "related_points": ["cp1"]}
```

字段名是 `answer`，值为**完整选项文本（含字母前缀）**；无 `correct_option`、无 `type/mode` 字段。THZ-Q02 同构（`answer: "C. 共享频谱、共享波形、共享硬件"`）。

## 修复内容

### Phase 3：canonical truth（server.py 新增 `_extract_correct_option`）

优先级（与 evaluate 判题同规则）：显式 `correct_option` 字段 → `answer` 本身为单字母 → `answer` 开头"字母+分隔符"前缀（`B.` `B、` `B:` `B)`）。无法可靠提取 → `(None, needs_review=True)`，**禁止静默猜测**。

### Phase 4：学生答案规范化（`_extract_choice_letter`）

确定性规则，只接受**整串**即明确答案表达：`b / B / b. / B. / 选B / 选 b / 答案B / 答案是B / 我选B / 我选 b` → 大写字母。自然语言长句（"我觉得A不对，所以选B"）不匹配任何模式 → None → 回退全文比较，**绝不从句中抓字母**（实测该句返回 None，未误抓 A）。

### Phase 5：判题逻辑

```python
if correct_option and student_letter:
    correct = (student_letter == correct_option)     # B == B → True，完全 deterministic
else:
    # 文本回退：剥掉选项/答案的字母前缀后比较正文（"0.1–10 THz" 直贴也判对）
```

choice 类型**零 LLM 参与**；LLM 仅用于 explanation。输出新增 `choice_judging` 诊断块（= Phase 2 的 [CHOICE_JUDGE_TRACE] 固化）：`student_raw / normalized_student / correct_option / comparison_result / needs_review`。

**回归矩阵（Phase 8，THZ-Q01 expected=B，全过）**：`b ✓ B ✓ b. ✓ B. ✓ 选B ✓ 答案是B ✓ 我选B ✓ → True`；`A ✗ C ✗ D ✗ → False`；`"0.1–10 THz"`（正确全文直贴，fallback 前缀剥离修复后）→ True；`"24–100 GHz"`（干扰全文）→ False。FAILS: 0。

### Phase 6：record_result 只接受 evaluator truth

实读 `record_result_impl`：`correct` 参数直接 INSERT + mastery upsert，**无二次解析/无模型询问/无另一套 answer 比较**。修复中未改动它；重建时以 `evaluation.correct` 原值传入。

### Phase 7：schema / migration

- **authoring 校验**（`knowledge_authoring/builders.py::build_questions`）：有 options 的题若无法提取 canonical `correct_option` → **validation error**（新 authoring/finalize 强制）；产出的 question 条目携带 `correct_option`。
- **存量 migration（deterministic，已执行）**：遍历 `knowledge/` + `draft_packages/` 全部 `questions.json`——16 包 / 86 题：**20 题注入 `correct_option`**（含 THZ-Q01→B、THZ-Q02→C、THZ-Q03→C）、**66 题标 `needs_review: true`**（answer 为无字母前缀正文形态，如 RC 课程的 `"从0开始连续上升"`——人工补 correct_option 前由文本回退判题）。migration 后 `validate_package(thz_isac)` 仍 valid。
- `validate_package`/`_find_question` 侧兼容双形态（correct_option 优先）。

## Phase 9/11：真实重放与状态修复（Phase 10）

重放与重建通过 **teach-mcp server.py 实现函数**（与 MCP stdio 工具同一代码路径；常驻 MCP 进程重启后自动生效）：

```
THZ-Q01: raw='b' normalized_student=B correct_option=B correct=True
THZ-Q02: raw='C' normalized_student=C correct_option=C correct=True
→ record_result ×2（correct=True 原值）
```

**Phase 10 污染修复**：真实用户作答记录位于 session `ff-02adc88d-20260927-162510-7392`（results id 677/678，correct=0，16:25/16:26）。执行：

1. 撤销两条污染行（DELETE）；
2. 审计表 `result_repairs` 落 2 条：`{question_id, old_correct=0, repair_reason="choice_judge_bug", original_result_id=677/678}`；
3. knowledge_mastery 重算（attempts=0/correct=0/mastery=0.0 过渡态）；
4. 修复后 evaluator 重放 + 正式 `record_result(correct=True)` ×2 重建。

**Phase 11 终态（真实 DB 查询）**：

```
results:  THZ-Q01 correct=1  |  THZ-Q02 correct=1
mastery:  {mastery: 1.0, attempts: 2, correct: 2}
session:  {progress: 0.4, status: open}   ← 0.4 为用户 TUI 真实交互推进
repair:   THZ-Q01/THZ-Q02 各一条 choice_judge_bug 审计
```

## 十问速答

1. `b` 原本被 lower 成 `"b"`（非 B）；输出里的 B 是展示回显。
2. 判题只用答案全文比较，字母输入与 `"B. 0.1–10 THz"` 全文无相等/包含关系 → 恒 False。
3. `answer` 字段 = "字母. 完整选项文本"，无 canonical 字段。
4. **是**：schema 增加 `correct_option`（builders 强校验 + 20 题存量注入 + 66 题 needs_review）。
5. **是**：canonical 字母相等即判，零 LLM；文本回退也已做前缀剥离修复。
6. **是**：record_result 仅透传 evaluation.correct（实读确认，未改动）。
7. **是**：THZ-Q01 raw=b → B==B → True（真实重放）。
8. **是**：THZ-Q02 raw=C → C==C → True（真实重放）。
9. **是**：两条污染行撤销（original_result_id=677/678 入 `result_repairs` 审计），修复后重放重建。
10. **是**：mastery 1.0/2/2、progress 0.4、results 双 correct=1，全部一致。

**CHOICE_JUDGING_CORRECTNESS_PASS**
