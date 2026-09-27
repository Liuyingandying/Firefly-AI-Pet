# Firefly Learning Return Channel — Runtime Delivery Audit 报告

日期：2026-09-27
范围：仅 Return Channel 运行链（dialog emit → session → watcher → result.json → CompanionConsole）。未触碰 teach-mcp 业务/mastery/curriculum/Resource Manifest/Memory Ownership/Z Code provider 配置。

---

## 根因（结论先行）

**不是 QMessageBox 阻塞了 emit（顺序实测为 A），而是 `session → console` 的投递连接只挂在 `_ensure_companion_console()` 这一条 console 打开路径上。** 经 Short Ask 等其它入口打开的 console 实例从未注入 BridgeLearningSession——result.json 即便产生、watcher 即便收到，也没有连接到用户可见聊天窗的信号通路。这是 Entry Wiring 轮（opener）与 Provider 轮（env）同款结构性问题的第三种表现：**多入口单例的绑定挂在了单一调用方**。

同时存在的次级问题：模态"成功"框（Phase 2 违规）会在任务执行的 30~70 秒里把用户挡在 OK 按钮上，即便链路全通，体验也是"点了 OK 然后没有任何反馈"。

## Phase 1：成功弹窗与 signal 的执行顺序

实测代码顺序（修复前的 `learning/bridge_dialog.py::_on_launch`）：

```
launch_learning_mode()        （同步：写 context/task.json、spawn Z Code）
→ course_launched.emit(...)   ← 先 emit（顺序 A）
→ QMessageBox.information(模态)
```

即 **emit 未被模态框阻塞**（顺序 A 成立）。但该模态框本身违反 Phase 2：它把用户挡住整个 agent 执行期，且其文案"教学内容会自动出现在聊天窗口"在投递链断裂时成为空头承诺——已按 Phase 2 取消。

## Phase 3：本次真实点击的运行产物（course_id=crs-d91e981f5a92）

| 产物 | 事实 |
|---|---|
| bridge/task.json | **存在**，mtime 03:36:33；`task_id=task-c262a6ff09`，`status=running`，`pid=32608`，action=resume |
| bridge/result.json | **存在**，mtime 03:37:06（任务启动 33 秒后）；schema 合法、`task_id` **等于** task-c262a6ff09、`action=resume`、`status=ok`、`message_type=question`、display_text 以"欢迎回来！上次你学到…进度 40%…"开头并含题目 |
| bridge/launch.log | mtime 03:37:12，有本次新内容 |

**按 Phase 5 分类：Case D/E 之间**——result 已产生且合法（非 A/B/C），断点在 watcher 收到后的投递末端（session→console 连接不保证存在）。task.json 的 `status=running` 未推进为 completed 属已知项：状态推进由下次 launch 或 skill 自身覆盖，v0.1 不做回收（不影响投递）。

## Phase 4/6：逐节点标记与实例一致性（修复后）

现在的完整可观测链（任一环节缺失即定位断点）：

```
[COURSE_LAUNCHED_EMIT] course_id=... task_id=...        （dialog，emit 后立即打点）
[LEARNING_SESSION_ATTACH] course_id=... task_id=...     （session，attach 时）
[LEARNING_RESULT_WAIT] task_id=...                       （watcher 启动）
… 后台：[FIREFLY_SKILL_START] / [MCP_READY] / [LEARNING_ACTION_DISPATCH]（skill 侧，尽力保证）
[LEARNING_RESULT_RECEIVED] task_id=... type=...          （watcher 命中）
[LEARNING_MESSAGE_DISPLAYED] chars=...                   （console 展示后）
```

实例一致性：`BridgeLearningSession` 为 shell 级单例（app.py __init__ 创建一次）；console 绑定经**模块级 binder**（`set_console_bridge_binder` → `open_singleton` 两条分支都调用 `_bind_console_bridge`），shell 的 `_bind_learning_console(console)` 幂等地完成"注入 session + 连接 result_ready/agent_timeout → console.show_learning_result"。**无论 console 经工具栏、Short Ask 还是 fallback 打开，绑定的都是用户当前可见的这个实例。** 新增测试 `test_3_click_attaches_watcher_immediately` 断言点击后 watcher 立即存活；`test_4_chain_delivers_message_to_console` 用**真实 console 的 show_learning_result** 验证投递。

## Phase 2/7：Success 语义修正（已落地）

- **取消模态成功框**：正常流程 emit → `dialog.accept()`（直接回到聊天窗），永不弹"已交给…点 OK"；错误仍弹 `QMessageBox.warning`（结构化 code）。
- 阶段提示改为非模态、进聊天流：attach 时聊天窗立即出现 **"已交给学习代理，正在恢复/执行学习……教学内容会自动出现在这里。"**；超时（15 分钟）一次性提示"学习代理仍在处理中……"并降频继续等（不宣告失败）。
- 成功拆解为 `TASK_SUBMITTED → AGENT_RUNNING → RESULT_READY → MESSAGE_DISPLAYED`，**只有 MESSAGE_DISPLAYED 算本轮交付成功**。

## Phase 9：回归测试（`tests/test_learning_return_delivery.py`，4 项 + 全量）

1. `_on_launch` 成功路径**不含 `QMessageBox.information`**，且 emit 先于 `accept()`/任何模态（AST 级）。
2. 同上覆盖"正常 launch 不调 information"。
3. 真实点击「继续学习」→ **立即** attach（watcher 存活断言）。
4. 全链 `task_submitted(spawn+task_id) → phase hint → result.json → result_received → console message_visible`（真实 console 方法记录）。
5. `process_started=True` 单独不构成投递：结果到达前聊天内容仅允许阶段提示、不得出现教学内容、不得建 pending（`test_5`）。

全量回归：**86 passed**（含 return channel 15、dialog runtime 3、entry wiring 5、provider env 5、memory guard 8、bridge 25、learning_mode_ui 等），`py_compile` 通过。

## Phase 8：真实用户路径（待人工验收）

完全退出 Firefly → 重启 → 学习模式 → 选同一课程（crs-d91e981f5a92）→ 继续学习：

1. 不再有需要点 OK 的成功框；dialog 直接关闭回到聊天窗；
2. 聊天窗立即出现"已交给学习代理，正在恢复/执行学习……"；
3. 约 30~70 秒后自动出现教学内容与题目（该课程当前进度 quiz/40%，teach-mcp 权威）；
4. 直接在输入框回答（如 `B`）→ 判分与下一题自动出现；「退出学习」恢复普通聊天。

若任一环节再失败，`learning_entry.log` 的标记序列会精确停在断点行。

---

## 结论（8 问速答）

1. **否**——emit 先于模态框（顺序 A）；但模态成功框已按 Phase 2 取消（它遮挡执行期并掩盖投递缺口）。
2. **存在**：task-c262a6ff09 / running / pid 32608。
3. **是**：result.json 内容即 skill 真实教学输出（欢迎回来 + 进度 40% + 题目）。
4. **是**：03:37:06 产生，schema 合法、task_id 匹配。
5. **是**（attach 在 emit 时同步完成）；但当时"收到的结果去哪"取决于 console 是否被注入 session——注入连接曾只覆盖工具栏打开路径。
6. **经 Short Ask 打开的 console 此前收不到**（Case E 真因）；现已模块级绑定，所有打开路径幂等注入并连接到当前可见实例。
7. **是**：emit 后 dialog 直接关闭、聊天窗立即出阶段提示、结果自动到达；错误才有弹窗。
8. **待用户重启后肉眼验收**；自动化已覆盖 click→attach→result→visible 全链（86 passed），产物侧该课程的 result.json 已就绪。

完成后停止。未触碰任何禁止项。
