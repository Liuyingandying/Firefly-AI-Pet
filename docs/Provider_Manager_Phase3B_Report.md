# Provider Manager Phase 3B 实施报告（AI 模型设置界面）

> 日期：2026-09-18
> 依据：架构报告 §4（Provider UI）+ Phase 1/2/3A 报告
> 范围：用户可见的模型设置窗口 + Settings 入口 + 保存/删除/刷新闭环。Provider API、Memory/Conversation/Scratchpad/Voice/Learning 零改动。
> 合规：无真实网络请求（测试 transport/注入式 reload）；无真实 Key（全部合成值）；未修改 `.env`；未 git commit。

---

## 1. 修改文件

| 文件 | 类型 | 内容 |
|---|---|---|
| `ui/provider_manager_window.py` | **新增**（~300 行） | `ProviderManagerWindow`（QDialog，标题"AI 模型设置"）+ `ProviderCard`（QFrame 卡片） |
| `ui/settings_popover.py` | 修改（+12 行） | 新增 Signal `provider_manager_requested` + AI STATUS 区按钮"AI 模型管理"。**构造器零改动**（沿用既有 Signal 模式：popover 只发信号，开窗在组合根） |
| `app.py` | 修改（+22 行） | `main()` import 补 `reload_default_routers`；VisualShell 连接 `provider_manager_requested → _open_provider_manager`；处理器构造窗口（ProviderManager + default_store + runtime_bus + reload_fn 注入，非模态 show/raise/activate + 打开时主动 refresh） |
| `tests/test_provider_manager_ui.py` | **新增**（8 项） | 见 §3 |

## 2. 界面与交互设计（对齐任务规格）

- **标题**："AI 模型设置"（QDialog，非模态，520×640，QScrollArea 承载卡片列表）。
- **卡片**（每个 credential-bearing Provider 一张，数据全部来自 Phase 3A `list_providers()`，catalog 单一事实源）：
  - `display_name`（TJU LLM / Zhipu GLM / …）
  - 状态点：`● 已配置`（theme.DOCK_STATUS_GREEN）/ `○ 未配置`（TEXT_SECONDARY）
  - 来源：`来源：environment|credential_store|dotenv|missing`，environment 附提示"优先级最高，本窗口修改不生效"
  - Key 显示：已配置 → `************`；未配置 → 空。**明文永不渲染、永不预填**（提示输入框为空起步；窗口代码路径根本不存在读取已存值的方式——只调用 `store.save/delete`）
  - 按钮：未配置 → `[添加密钥]`；已配置 → `[替换密钥] [删除密钥]`（objectName `addKey-<id>` 等供测试定位）；deprecated 行 `enabled=False` 置灰仅供查看
- **保存流**：输入（QInputDialog，空值即取消，窗口层再做空值守卫——空输入绝不清掉已存 Key）→ `CredentialStore.save()` → `reload_default_routers()` → `refresh()`；RuntimeBus `providers.updated` 事件也会触发刷新（双通道，事件只是优化）。
- **删除流**：`CredentialStore.delete()` → `reload_default_routers()` → `refresh()`。
- **入口**：Settings → AI STATUS 区"AI 模型管理"按钮 → `provider_manager_requested` 信号 → VisualShell `_open_provider_manager()`（关闭 popover、构建/复用窗口、打开时主动 `refresh()`——不依赖事件）。
- **RuntimeBus**：窗口订阅 `providers.updated`（`subscribe_event`），事件到达经 `QTimer.singleShot(0)` 回 GUI 线程刷新；`closeEvent` 退订；ai_router 仍保持 Qt-free（发布器为组合根注入的可调用）。

## 3. 测试结果

| 套件 | 结果 |
|---|---:|
| `tests/test_provider_manager_ui.py`（新增） | **8 passed** |
| Phase 3A `test_provider_manager.py` | 9 passed |
| Phase 1/2 + Provider 三件套 + Memory 边界 + screen_vision 全量回归（即此前 140 基线） | 131 passed |
| `tests/test_ui_consolidation_p0.py`（SettingsPopover 邻居回归） | 9 passed |
| **合计（本阶段验收口径）** | **148 passed / 0 failed（+9 邻居）**，`import app` 冒烟 OK |

8 项新测试与任务要求一一对应：
1. **窗口创建成功** — QDialog + 标题"AI 模型设置" + 10 张卡片；
2. **Provider 列表显示** — 卡片 id 集合 ≡ catalog 推导集合；未配置卡片的状态/来源/按钮可见性断言（`isVisibleTo`）；deprecated 行处理；
3. **Key 输入保存调用** — `_save_key` → `store.save` 断言 + `reload_fn` 恰调用一次 + 状态刷新为 credential_store；空值保存为 no-op（`reload_fn` 零调用）；
4. **删除 Key** — `store.delete` + reload + 卡片回到"○ 未配置/添加密钥"；
5. **Key 不显示明文** — 保存后遍历窗口全部 QLabel 断言明文零出现 + 掩码在位；输入提示永不预填已存值；
6. **providers.updated 后刷新** — 真实 RuntimeBus + 事件泵：reload 发布事件 → 窗口刷新计数 ≥1；无关事件（camera.observed）与退订后均不刷新。

## 4. 风险与已知事项

| # | 风险/事项 | 状态 |
|---|---|---|
| 1 | 测试期间发现：**无 QApplication 时构造 QWidget 会导致 qFatal 瞬时杀进程（无任何 traceback）** | 已修复测试（`wired` 夹具首参注入 `qapp`）；对产品无影响（应用启动必先建 QApplication） |
| 2 | `reload_default_routers()` 每次重建全部默认路由的适配器实例 | 单次构建 ~ms 级、仅在用户主动保存/删除时发生；健康态保留（Phase 2 测试锁定） |
| 3 | environment 来源下保存不生效 | UI 提示语已明示（`_SOURCE_HINTS`）；可选的"禁用保存"强化留待 rc3 |
| 4 | 删除密钥无二次确认 | 密钥可随时重新添加，危害低；如需确认对话框属一行改动，留待体验走查 |
| 5 | 冒烟遗留 scratch：仓库根 `pt_*.log`/`pt_report.xml` 为本轮验证输出 | 属未跟踪临时文件，建议随 Phase 3B 收尾一并清理（遵守不删除纪律，本阶段未动） |

---

*验证命令：`pytest tests/test_provider_manager.py tests/test_provider_manager_ui.py tests/test_provider_reload.py tests/test_credential_store.py tests/test_provider_router.py tests/test_provider_catalog.py tests/test_provider_status.py tests/test_companion_memory_boundary.py tests/test_write_guards.py tests/test_screen_vision_routing.py tests/test_capture_semantics.py -q` → **148 passed**；另 `tests/test_ui_consolidation_p0.py` → 9 passed；`import app` OK。未执行 git add/commit。*
