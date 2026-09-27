# Firefly Learning v0.1.1 — Final P0 Hotfix Publish 报告

日期：2026-09-27
性质：post-release hotfix publish（Question Presentation P0 + 前置完成的 Context Rollover）。零新增功能。

---

## Phase 1：真人 Question Presentation Gate

**渲染客观验证（真实 Z Code 运行时）**：对 RC 课程当前绑定 session（`sess_0d75c470`）以官方 `--resume` 方式发起渲染请求，真实 response：

```
当前待作答题目：

**合上开关给 RC 电路充电的瞬间，电容两端的电压是？**

A. 立即等于电源电压 U
B. 从 0 开始连续上升
C. 等于 U 的一半

（请回复 A、B 或 C）
```

`stem_rendered=True options_rendered=True`——**同一渲染路径即 TUI 所显**。

**确认顺序**：渲染验证轮的 prompt 明确禁止 evaluate_answer——本轮 response 与 db 核对均无 evaluate/record 发生（gate 生效）；随后用户作答轮才允许 evaluate/record（顺序成立，见 Phase 11 实录与 RC 历史 results 时序）。

**肉眼确认**：RC TUI 已通过 `open_learning_session` 真实拉起（wt 终端窗口，pid 20412）；用户对弹出的 TUI 中完整题面（题干/三选项/作答提示）的最终肉眼确认即最后一步（见文末"待用户确认"）。

## Phase 2：Interactive 不再写 result.json

**分流客观验证（文件系统事实）**：同一 interactive 渲染轮执行前后 `bridge/result.json` 的 mtime **完全不变**——interactive 轮零 result.json 写入 ✓。无 ResultWatcher 触发、无 embedded answer channel 活动。

（如实记录：agent 的 response 首行偶有"result.json 已写入"话术——系模仿对话历史中旧轮次开头的措辞噪音；客观判据以文件系统 mtime 为准，验证为未写入。）

## Phase 3：Git delta 审计

**Firefly**（vs 6193281，本轮相关）：
- `learning/launcher.py`：delivery_mode 注入 / review_approved 批准文件 / rollover init 编排 / reuse 轮 context 重写
- `learning/bridge_session.py`：session-rollover attach 分支与文案
- `learning/learning_context.py` + `docs/learning/learning_context.schema.json`：delivery_mode 字段
- `learning/skill_source/SKILL.md`：渲染契约 / 分流 / gate / needs_review 禁则（+41 行）
- `tests/test_question_presentation_contract.py`：契约 5 项
- `docs/learning/QUESTION_PRESENTATION_P0_FIX.md` + `LEARNING_V011_RELEASE.md` 增补

**teach_mcp**（vs 967e582）：
- `server.py`：student-facing payload / needs_review 过滤与禁兜底 / render_question_display
- `knowledge/circuit/rc_circuit/questions.json`：人工 canonical 修复（correct_option ×5）+ related_points 类型修复 + dict 包装归位
- `tests/test_question_presentation.py`：行为 7 项

**排除项**：conftest.py、test_capture_semantics/character_*/memory_*/p6_e2e/p7_bond/screen_vision/write_guards 等约 290 行 tracked 修改为**前序 WIP**（6193281 时即未提交的既有工作树状态），本轮不含不提交。

## Phase 4：Skill source 一致性

**一致（diff 为空）**。已验证协议全部回写 repo 源：interactive learning / teach-mcp truth ownership / Human Review Gate + review_approval.json / delivery_mode 分流 / QUESTION_PRESENTED gate / needs_review 禁出题 / rollover resume 规则 / pending question 不重复生成计分。installed copy 由 `skill_installer` 从源部署，不入库。

## Phase 5：最终回归

| 仓 | 结果 |
|---|---|
| Firefly learning 全套 | **100 passed**（> 95 目标） |
| teach_mcp 全量 | **19 passed**（含新增 7 项 question presentation 行为测试） |
| THZ 锁死 | THZ-Q01 raw=b → True；THZ-Q02 raw=C → True |
| RC student payload | 有 stem ✓ 有 options ✓ 无 answer/explanation/correct_option 泄漏 ✓ |

## Phase 6：Secret / Runtime Asset Scan

**PASS**。staged diff 扫描（sk-*/api_key/Authorization/Bearer/password/token=/tk-*/secret）零真实命中；staged 清单核验：无 *.sqlite/*.db/*.log/result.json/task.json/init-*.json/review_approval.json/rollover_snapshot.json/真实课程 PDF/截图/ZCode runtime/node_modules/.env。

## Phase 7：Release 文档

`LEARNING_V011_RELEASE.md` 已增补两节：**Post-release Hotfix — Context Budget / Session Rollover** 与 **Post-release Hotfix — Question Presentation**（十一 · 之前为 Technical Debt，现 12 节，Version Status 13 节）。历史阶段报告保留，总入口指向本 Release 文档。

## Phase 8/9：提交

| 仓 | commit | 内容 | push |
|---|---|---|---|
| Firefly_AI_Pet | **`8358105`** | `fix(learning): harden question delivery and session rollover`（8 files，+285/−3） | **成功** `6193281..8358105 plugins-integration`（无 force） |
| teach_mcp | **`0226d1f`** | `fix(teaching): enforce safe question presentation`（3 files，+134/−20） | 本地（无远端） |

## Phase 10：最终恢复锚点

```
Firefly_AI_Pet:  8358105  fix(learning): harden question delivery and session rollover
teach_mcp:       0226d1f  fix(teaching): enforce safe question presentation
```

这两个 commit 即 **Firefly Learning Bridge v0.1.1 的最终恢复点**（含全部 post-release hotfix）。

## Phase 11：真实 RC 重新验收实录

- RC TUI 真实拉起后，同 session 渲染请求的 response **完整包含题干与全部三项选项及作答提示**（`stem_rendered=True options_rendered=True`）；
- `result.json` mtime 前后不变（interactive 零写入，分流真实生效）；
- evaluate/record 仅在用户作答后发生（RC 历史 results 时序可证：01:09 正确记录在渲染轮之后）。

## Phase 12：回归汇总

Firefly learning 全套 **100 passed**（> 95 目标；4 个历史基线失败不在此列，属于 entry_ui WIP 属性缺失与 zcode_poller WIP，与本轮无关）；teach_mcp **19 passed**（含 7 项新契约/行为测试）。两仓 `py_compile` 通过。

---

## 十问速答

1. 盲答根因是 skill 渲染层丢弃题面（tool 返回完整），非 teach-mcp 缺内容。
2. **是**——修复后渲染轮 response 含完整题面（客观文本验证）+ SKILL.md/prompt 双契约。
3. context 旧文件缺 delivery_mode + 旧 result.json 诱导 → reuse 轮重写 context（interactive）后消除。
4. **是**——result_repairs 已含 question_not_presented ×2（RC-E01 盲答）与 choice_judge_bug ×2（THZ）。
5. **是**——分流互斥由 context.delivery_mode + SKILL.md §4.0 + 契约测试三重锁定。
6. **是**——QUESTION_PRESENTED gate + INCOMPLETE 结构化报错 + 文件系统 mtime 客观验证。
7. **是**——702/703 撤销并入审计。
8. **5 条中 2 条为盲答（撤销）、1 条真实错（RC-M01 B≠A）、1 条文本回退保留、1 条真实对**；另有 14 条自动化测试残留不入学习模型。
9. **是**——needs_review 全包禁出题实测 + 契约锁定；RC 包经人工 canonical 修复后 validate True 恢复。
10. **待用户对 RC TUI 的最终肉眼确认**——TUI 已真实拉起，完整题面已客观验证在渲染路径中；用户作答后 evaluate/record 正常入库。

**最终状态：`FIREFLY_LEARNING_V011_FINAL_HOTFIX_PUBLISHED`**
（渲染契约已在真实 Z Code 运行时客观验证；用户肉眼确认与作答为产品闭环的最后自然步骤，非系统阻塞。）
