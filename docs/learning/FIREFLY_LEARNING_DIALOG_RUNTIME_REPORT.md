# Firefly Learning Bridge — Runtime Dialog Audit 报告

日期：2026-09-27
范围：仅 Firefly GUI runtime wiring（AbilityPanel click → signal → shell slot → dialog → visibility）。未触碰 teach-mcp / firefly-learning skill / Z Code / resource manager / curriculum / memory ownership。
前置：`FIREFLY_LEARNING_ENTRY_WIRING_REPORT.md`（入口改接）、`FIREFLY_LEARNING_BRIDGE_V01_REPORT.md`（Bridge 本体）

---

## 根因（一句话）

**"Ask…"（Short Ask）入口直接调 `ui.v2.console.open_singleton()` 打开 console，绕过了 `_ensure_companion_console()`——而 bridge 信号接线恰好只挂在那一个函数里。** 用户经 Short Ask 打开的 console 实例上 `learning_bridge_requested` 无任何接收者：按钮点击后 `_on_ability` 执行、信号 emit，然后落入虚空。日志证明 console 层每次都跑了，shell 层从未收到。

## 1. 最近一次真实点击是否进入 bridge route？

**Console 层进入了，shell 层没有。**

`%LOCALAPPDATA%\FireflyAI\logs\learning_entry.log`（用户真实点击时段）末 50 行原样记录：

```
2026-09-27 01:35:25,897 [LEARNING_ROUTE] route=learning_bridge
2026-09-27 01:35:26,137 [LEARNING_ENTRY] source=ability_panel
2026-09-27 01:35:26,137 [LEARNING_ROUTE] route=learning_bridge
2026-09-27 01:35:26,396 [LEARNING_ENTRY] source=ability_panel
2026-09-27 01:35:26,396 [LEARNING_ROUTE] route=learning_bridge
2026-09-27 01:35:27,419 [LEARNING_ENTRY] source=ability_panel
2026-09-27 01:35:27,419 [LEARNING_ROUTE] route=learning_bridge
2026-09-27 01:35:28,566 [LEARNING_ENTRY] source=ability_panel
2026-09-27 01:35:28,566 [LEARNING_ROUTE] route=learning_bridge
2026-09-27 01:35:29,728 [LEARNING_ENTRY] source=ability_panel
2026-09-27 01:35:29,728 [LEARNING_ROUTE] route=learning_bridge
2026-09-27 01:35:30,004 [LEARNING_ENTRY] source=ability_panel
2026-09-27 01:35:30,004 [LEARNING_ROUTE] route=learning_bridge
2026-09-27 01:35:30,263 [LEARNING_ENTRY] source=ability_panel
2026-09-27 01:35:30,263 [LEARNING_ROUTE] route=learning_bridge
2026-09-27 01:36:15,494 [LEARNING_ENTRY] source=ability_panel
2026-09-27 01:36:15,494 [LEARNING_ROUTE] route=learning_bridge
2026-09-27 01:36:15,708 [LEARNING_ENTRY] source=ability_panel
2026-09-27 01:36:15,708 [LEARNING_ROUTE] route=learning_bridge
2026-09-27 01:37:54,750 [LEARNING_ENTRY] source=ability_panel
2026-09-27 01:37:54,750 [LEARNING_ROUTE] route=learning_bridge
2026-09-27 01:37:54,759 [LEARNING_ENTRY] source=tray
2026-09-27 01:48:21,716 [LEARNING_ENTRY] source=ability_panel
2026-09-27 01:48:21,717 [LEARNING_ROUTE] route=learning_bridge
2026-09-27 01:48:28,223 [LEARNING_ENTRY] source=ability_panel
2026-09-27 01:48:28,223 [LEARNING_ROUTE] route=learning_bridge
2026-09-27 01:48:29,322 [LEARNING_ENTRY] source=ability_panel
2026-09-27 01:48:29,322 [LEARNING_ROUTE] route=learning_bridge
2026-09-27 01:48:29,519 [LEARNING_ENTRY] source=ability_panel
2026-09-27 01:48:29,519 [LEARNING_ROUTE] route=learning_bridge
2026-09-27 01:48:29,703 [LEARNING_ENTRY] source=ability_panel
2026-09-27 01:48:29,703 [LEARNING_ROUTE] route=learning_bridge
2026-09-27 01:48:29,839 [LEARNING_ENTRY] source=ability_panel
2026-09-27 01:48:29,839 [LEARNING_ROUTE] route=learning_bridge
2026-09-27 01:48:30,015 [LEARNING_ENTRY] source=ability_panel
2026-09-27 01:48:30,015 [LEARNING_ROUTE] route=learning_bridge
2026-09-27 01:48:30,167 [LEARNING_ENTRY] source=ability_panel
2026-09-27 01:48:30,167 [LEARNING_ROUTE] route=learning_bridge
2026-09-27 01:48:30,447 [LEARNING_ENTRY] source=ability_panel
2026-09-27 01:48:30,447 [LEARNING_ROUTE] route=learning_bridge
2026-09-27 01:48:30,624 [LEARNING_ENTRY] source=ability_panel
2026-09-27 01:48:30,624 [LEARNING_ROUTE] route=learning_bridge
2026-09-27 01:48:30,823 [LEARNING_ENTRY] source=ability_panel
2026-09-27 01:48:30,823 [LEARNING_ROUTE] route=learning_bridge
2026-09-27 01:48:31,039 [LEARNING_ENTRY] source=ability_panel
2026-09-27 01:48:31,039 [LEARNING_ROUTE] route=learning_bridge
2026-09-27 01:48:31,359 [LEARNING_ENTRY] source=ability_panel
2026-09-27 01:48:31,359 [LEARNING_ROUTE] route=learning_bridge
2026-09-27 01:48:31,583 [LEARNING_ENTRY] source=ability_panel
2026-09-27 01:48:31,583 [LEARNING_ROUTE] route=learning_bridge
```

判读：每次点击恰好 **2 行**（console 分支自己的 ENTRY+ROUTE，同毫秒成对）；`open_learning_bridge()` 第一行也会打一条 ROUTE——若 shell slot 执行过，每次点击应是 **3 行**。从未出现第三行。01:35→01:48 的密集连点与"无反馈所以反复点"的行为吻合。01:37:54,759 那条 `source=tray` 是用户也试过托盘入口（托盘路径本来就连通）。

## 2. signal 是否被真实 VisualShell 收到？

**没有。** 证据如上（第三条 ROUTE 缺席）。根因链：

```
app.py:_on_short_ask_requested()          ← "Ask…" 入口（用户实际打开 console 的方式）
  → ui.v2.console.open_singleton(...)     ← 直接打开，绕过接线
app.py:_ensure_companion_console()        ← 唯一挂接线的函数
  → console.learning_bridge_requested.connect(open_learning_bridge)
```

`open_singleton` 是模块级单例、有多个调用方（toolbar console 按钮 → `_ensure_companion_console`；Short Ask → `_on_short_ask_requested`；legacy fallback），但信号接线只存在于其中一个调用方之后。这是 Entry Wiring 审计中"两套平行入口"问题在 console 打开层面的再现。

## 3. open_learning_bridge 是否执行？

**没有。**（同上，shell 层标记零出现。）

## 4. dialog 是否成功构造？

**从未执行到构造。**（故障点在信号接收之前，与 dialog 构造无关。）

## 5. dialog 是否曾被 GC？

**不适用（从未构造）。** 但借本次审计已按 Phase 4 加固生命周期：
- `self._learning_bridge_dialog` 强引用单例保留；
- 新增 `dialog.destroyed.connect(...)` 在销毁时清空引用（关闭后可重建，绝不泄漏、绝不在可见期被 GC）；
- 每次点击 `show()/raise_()/activateWindow()` 复用，不无限建窗。

## 6. 是否存在运行旧代码/旧进程问题？

**不存在。** `[LEARNING_ENTRY] source=ability_panel` 字符串只存在于本次改接后的 console.py——日志有记录即证明运行中的进程加载了 E:\Firefly_AI_Pet 的新源码。进程排查：当前无 Firefly 宠物 `app.py` 进程、无打包 EXE 进程、仅此一份 clone。已额外在 `main()` 增加 `[BUILD_RUNTIME] source_file=... bridge_module=... pid=...` 启动标记，此后每次启动都会在 learning_entry.log 里记录实际加载的源码绝对路径，一劳永逸排除此类疑问。

## 7. 最终 dialog.isVisible 是否为 True？

**True（自动化真实链验证）。** 新增 `tests/test_learning_dialog_runtime.py`（3 项全绿），验证的不是"信号发了"，而是**最终窗口可见**：

1. `test_real_button_to_visible_dialog`：真实 `open_singleton` 创建 console → 模块级 opener 绑定 → **真实点击 `ability.button("study")`** → 真实 `VisualShell.open_learning_bridge` slot 体（unbound 调用）→ owner 强引用 dialog → `processEvents()` → **`isVisible() is True`** + 全部六类标记（SIGNAL_RECEIVE/DIALOG_CREATE/DIALOG_SHOW/DIALOG_STATE visible=True）。
2. `test_second_click_reuses_singleton_dialog`：第二次点击复用同一 dialog 实例（refresh，不重复建窗）且保持可见。
3. `test_constructor_failure_surfaces_error_not_silence`：构造器抛异常 → owner 引用保持 None（无半成品）→ 用户看到结构化错误（QMessageBox.critical「学习模式窗口打开失败」）→ `visible=False error=True` 标记落盘——**绝不静默**。

## 8. 用户真实点击是否肉眼看到窗口？

**待用户重启后最终验收。** 修复后链路每一节点均已可观测/已自动化验证；按 Phase 8 流程：完全退出 Firefly（含托盘）→ 重新启动 → 点击「学习模式」→ 学习入口日志应依序出现：

```
[BUILD_RUNTIME] source_file=... bridge_module=... pid=...   ← 启动时
[LEARNING_CLICK] console_id=...
[LEARNING_ENTRY] source=ability_panel
[LEARNING_ROUTE] route=learning_bridge
[LEARNING_SIGNAL_EMIT] console_id=...
[LEARNING_SIGNAL_RECEIVE] shell_id=... console_id=...       ← 本次修复的关键缺口
[LEARNING_DIALOG_CREATE] dialog_id=...
[LEARNING_DIALOG_SHOW] dialog_id=...
[LEARNING_DIALOG_STATE] dialog_id=... visible=True active=...
```

肉眼应看到 LearningBridgeDialog（最近课程/[继续学习]/[导入学习资料]）。若再失败，日志会精确停在缺的那一行。

---

## 本次改动清单（仅 GUI runtime wiring）

| 文件 | 改动 |
|---|---|
| ui/v2/console.py | **根治**：模块级 `set_learning_bridge_opener()` + `_bind_learning_bridge_opener()`；`open_singleton` 两条分支（复用/新建）都自动绑定 opener——无论哪条路径打开 console，接线必然存在。study 分支加 `[LEARNING_CLICK]`/`[LEARNING_SIGNAL_EMIT]`（带 console_id） |
| app.py | `__init__` 注册 opener（`set_learning_bridge_opener(self.open_learning_bridge)`）；`open_learning_bridge` 加 `[LEARNING_SIGNAL_RECEIVE]`（sender id）/`[LEARNING_DIALOG_CREATE|SHOW|STATE]`、`processEvents` 后 `isVisible` 校验、出屏几何防御（不可见时居中主屏再 show/raise/activate）、最外层 `try/except` + `logger.exception` + 用户可见错误弹窗、`destroyed` 清引用；`main()` 加 `[BUILD_RUNTIME]` 启动标记 |
| tests/test_learning_dialog_runtime.py | **新增** 3 项（见 §7） |

保留的既有接线（`_ensure_companion_console` 内 connect + disconnect-safe）与模块级绑定互相幂等，不产生双触发。

## 回归

`test_learning_dialog_runtime.py` 3/3、`test_learning_entry_wiring.py` 5/5、`test_learning_bridge.py` 25/25、`test_learning_bridge_memory_guard.py` 8/8、`test_learning_mode_ui.py` 全绿、`test_learning_entry_ui.py` 恢复至基线（3 项既存 WIP 失败与本任务无关）——**78 过 / 3 既存基线失败**，`py_compile` 全部通过。

## 结论（8 问速答）

1. console 层进了 bridge 分支（日志实证），shell 层没进——信号无接收者。
2. 否。Short Ask 打开的 console 绕过了唯一接线点。
3. 否。
4. 否（未执行到）。
5. 不适用；已加固强引用 + destroyed 清理。
6. 否（日志字符串证明新代码在跑；已加 BUILD_RUNTIME 启动标记）。
7. True（真实按钮→真实 slot→真实 dialog 的自动化链验证）。
8. 待重启后人工验收；若仍不可见，learning_entry.log 会精确定位断点。
