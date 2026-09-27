# Firefly Learning Bridge v0.1 — Entry Wiring Audit 报告

日期：2026-09-27
分支：plugins-integration（未提交）
前置：`docs/learning/FIREFLY_LEARNING_BRIDGE_V01_REPORT.md`（Bridge 本体已交付并 PoC 通过）

---

## 1. 截图中的「学习模式」此前实际走什么路径？

从真实 UI 控件反向追踪（非按名字猜测），改接前的完整调用链：

```
[学习模式] QToolButton                     ui/v2/ability_panel.py:46-65
  → button.clicked → AbilityPanel.requested.emit("study")     ability_panel.py:63-65
  → CompanionConsole._on_ability("study")                     console.py:224（connect）；:1361-1375（旧分支）
    → self.learning.enter_mode() / exit_mode()                core/learning LearningModeController
    → self.chat.append_assistant(reply)                       聊天框直接输出"进入学习模式了…"
    → self._apply_learning_state() + self._show_learning_entry()
```

即：**legacy core/learning 内环**——完全在 Firefly 主聊天窗口内，由 TJU LLM 对话响应，从不离开 Firefly，与 LearningBridgeDialog / launch_learning_mode / Z Code / teach-mcp 零关联。与用户观察到的现象完全一致。

## 2. 为什么 Bridge PoC 成功但真实 UI 没接进去？

v0.1 交付时的入口接错位置：Bridge 入口（`open_learning_bridge`）只接到了**托盘菜单**（`ui/system_tray.py`「学习模式」项 → `VisualShell.open_learning_bridge`），而用户实际使用的主界面左侧能力区「学习模式」按钮是**另一个既有入口**（v2 console ability panel），它绑定的是 Phase 1B 时代的 legacy 契约。两个入口同名不同路——Bridge 的单元/集成测试从 API 层驱动（`launch_learning_mode` 直调），没有覆盖"从主界面按钮点击开始"这条真实用户路径，导致 PoC 全绿但主入口未接入。

## 3. 主界面和托盘是否曾存在两套入口？

**是。** 改接前盘点：

| 入口 | 改接前路由 |
|---|---|
| 主界面 ability panel「学习模式」 | legacy core/learning（`enter_mode`/`exit_mode` + 聊天框回复） |
| 托盘菜单「学习模式」 | LearningBridgeDialog → `launch_learning_mode` → Z Code |

A. 托盘 → Bridge：**是**（交付时已接）。
B. 主界面 → Bridge：**否**（本次问题根源）。
C. 两套平行入口：**存在**，且主界面才是用户常用入口。

## 4. 现在主界面是否真正调用 LearningBridgeDialog？

**是。** 改接后的调用链（console.py:1361 起新分支）：

```
[学习模式] QToolButton
  → AbilityPanel.requested.emit("study")
  → CompanionConsole._on_ability("study")
    → [LEARNING_ENTRY] source=ability_panel（诊断打点）
    → [LEARNING_ROUTE] route=learning_bridge（诊断打点）
    → console.learning_bridge_requested.emit()          console.py 新增 Signal
  → VisualShell._ensure_companion_console() 中的接线      app.py（与 settings/research 同款 disconnect-connect 模式）
    → VisualShell.open_learning_bridge()
      → [LEARNING_ROUTE] route=learning_bridge
      → LearningBridgeDialog（单例，无 parent）
```

**运行时验证**（离屏构造真实 `CompanionConsole`，真实点击 `ability.button("study")`）：
- 捕获 `[LEARNING_ENTRY] source=ability_panel`、`[LEARNING_ROUTE] route=learning_bridge`
- `learning_bridge_requested` 信号触发 → `open_learning_bridge` 被调
- `runner.ask` 零调用、聊天框零输出（不再出现"进入学习模式了…"）

## 5. 点击课程后是否真实执行 launch_learning_mode？

**是。** `LearningBridgeDialog._on_launch` → 由对话框状态推导 action（`action_for_state`：新资料=new、待审=review、就绪/有会话=resume，处理中禁用）→ `launch_learning_mode(course_id, action, headless=False)`（交互式：优先 `wt.exe -d <workspace> node zcode.cjs`）。测试 `test_4_and_5_course_launch_builds_zcode_command` 以 mock 捕获确认对话框把正确的 `(course_id, action)` 与 `headless=False` 传入；真实 argv 构造由 `test_learning_bridge.py::test_10` 锁定。

## 6. 是否真实启动 Z Code？

**是（交互式路径为 UI 实际所用）。** headless 路径在 v0.1 PoC 已全链真实启动验证（Z Code 产生会话、firefly-learning skill 接管、teach-mcp 状态推进）。本次新装的可观察性使每次启动都留下 `[ZCODE_LAUNCH] command=... pid=...` 记录（`<FireflyData>/logs/learning_entry.log`）。skill 接管的判定特征：launch.log 中出现 skill 协议输出（如 v0.1 PoC 中"学习资料处理完成/WAITING_REVIEW/已交给 Z Code 学习代理"流程）。

## 7. 是否能看到 firefly-learning Skill 接管？

**能看到，且现在有固定观测点：**
- 文件观测：`<course>/bridge/launch.log`（Z Code 子进程 stdout/stderr）——skill 输出（context 校验、teach-mcp 调用汇报、审核摘要）全部落在这里；v0.1 PoC 已实证。
- 状态观测：`bridge/course_binding.json` 出现 opaque id、`workspace/.firefly/learning_context.json` 每次 launch 刷新。
- 日志观测：`<FireflyData>/logs/learning_entry.log` 四类标记（LEARNING_ENTRY / LEARNING_ROUTE / LEARNING_BRIDGE / ZCODE_LAUNCH）。
- UI 观测（本次新增，Phase 6）：启动成功弹「已交给 Z Code 学习代理」+ 课程名 + course_id；启动失败弹「Z Code 学习代理启动失败」+ 结构化 `[code] message`，**绝不静默回退 legacy**（对话框代码中无任何 legacy 回退路径）。

## 8. legacy core/learning 是否还存在、由谁使用？

**完整保留，未删一行。** 改接后 legacy `core/learning`（LearningModeController 及 v2 console 内的学习模式 UI）仍有以下调用方（已逐一确认）：

| 调用方 | 位置 | 说明 |
|---|---|---|
| 聊天触发词「考考我」 | console.py `_STUDY_TRIGGER` → `runner.ask` | 文本进入 legacy 学习模式 |
| 视频学习「继续学习」 | console.py `_on_continue_study`（L1034-1039） | 追加触发词进 legacy |
| 学习模式 UI 内部刷新 | `_apply_learning_state`（L374）、`_show_learning_entry`（L492）、课程卡片/选课器/退出等（L320-916 多处） | legacy 模式激活后的自有界面流 |
| B站链接/「陪我学习」语义路由 | character_conversation_runner.py（runner 侧） | 与按钮无关 |

---

## 本次改动清单

| 文件 | 改动 |
|---|---|
| ui/v2/console.py | 新增 `learning_bridge_requested` Signal；`_on_ability` study 分支改接（打点+发信号），不再调 enter/exit_mode 与聊天框输出 |
| app.py | `_ensure_companion_console` 接线 `learning_bridge_requested → open_learning_bridge`（disconnect-safe）；`open_learning_bridge` 加 ROUTE 打点 |
| ui/system_tray.py | `_open_learning_bridge` 加 ENTRY(source=tray) 打点 |
| learning/diagnostics.py | **新增**：四类标记打点器（logger + `<FireflyData>/logs/learning_entry.log`，无敏感字段） |
| learning/launcher.py | `[LEARNING_BRIDGE]`（course_id/action/context_path）与 `[ZCODE_LAUNCH]`（command/pid/mode）打点 |
| learning/bridge_dialog.py | Phase 6 文案：成功=「已交给 Z Code 学习代理」+课程+course_id；失败=「Z Code 学习代理启动失败」+结构化 code |
| tests/test_learning_entry_wiring.py | **新增** 5 项：主界面按钮→bridge、托盘→bridge、study 分支 AST 禁 legacy 调用、选课→launch（mock 捕获）、失败→结构化错误无回退 |
| tests/test_learning_mode_ui.py | 2 个旧契约测试按新契约更新（按钮=bridge-only，不再切换 legacy 模式） |
| tests/test_learning_entry_ui.py | 12 处 `study` 按钮调用改为完整 legacy 序列直调（enter/exit + _apply + _show），保留 legacy UI 覆盖，与按钮解耦 |

## 回归结果

- 本次新增/更新测试：`test_learning_entry_wiring.py` 5/5 绿；`test_learning_mode_ui.py` 全绿（2 项按新契约更新）。
- 全量受影响面（7 个测试文件）：**94 通过 / 4 失败**——4 个失败均为**既存基线**（`test_learning_entry_ui.py` 3 项：WIP console 缺 `mode_label`/`_learning_rows`/`learning_status_box` 属性；`test_system_tray.py` 1 项：app.py WIP `zcode_poller` 缺失），与本次改动无关（本任务开工前已存在，v0.1 交付报告同款记录）。
- 运行时验证（离屏真实 console + 真实按钮点击）：日志标记、信号、聊天零输出三项全部符合预期。

## 结论（8 问速答）

1. 此前走 legacy core/learning（按钮→`_on_ability`→`enter_mode`→聊天框回复）。
2. Bridge 只接了托盘，主界面按钮绑的是 Phase 1B 旧契约；测试从 API 层驱动，未覆盖真实点击路径。
3. 是，两套平行入口（主界面=legacy，托盘=Bridge）。
4. 是，主界面现在经 `learning_bridge_requested` 信号 → `open_learning_bridge` → LearningBridgeDialog（运行时已验证）。
5. 是，选课后 `_on_launch` → `launch_learning_mode(course_id, action)`（测试锁定参数）。
6. 是，headless 全链 PoC 已真启 Z Code；交互式路径同一 launcher，落 `[ZCODE_LAUNCH]` 日志。
7. 能：launch.log（skill 输出）+ binding/context 文件 + learning_entry.log + UI「已交给 Z Code 学习代理」。
8. legacy 完整保留，使用者=触发词/视频学习/legacy UI 内部流/runner 语义路由；主界面按钮已不再直连它。
