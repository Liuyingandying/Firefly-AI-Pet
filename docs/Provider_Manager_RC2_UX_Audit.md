# Provider Manager rc2 用户体验验收审计（UX Audit）

> 日期：2026-09-18
> 范围：Provider Manager 全链路（Phase 1 存储 → Phase 2 热更新 → 3A 状态层 → 3B 界面）的**人工路径模拟验收**。不修改架构，不改生产逻辑。
> 合规：全程合成 Key（`sk-rc2-synthetic-*`）；零真实 API 调用；reload 为注入式 mock；未 commit。

---

## 1. 验收方法

1. **自动化人工路径模拟**：`tests/test_provider_manager_manual_flow.py`（9 项）按组合根真实接线复现"用户操作序列"——真实 `SettingsPopover`（stub 偏好源）→ 点击"AI 模型管理"按钮 → `provider_manager_requested` 信号 → 与 `app.py` 语义一致的处理器（单实例窗口、打开即刷新）→ 真实 `ProviderManagerWindow` + 真实 `ProviderManager` + 真实 `CredentialStore`（tmp 隔离）→ **真实点击卡片按钮**驱动添加/替换/删除。
2. **真实像素检视**：`QWidget.grab()` 离屏渲染两张截图（非 mock、真实渲染管线），审计员逐像素检视：
   - `docs/screenshots/provider_manager_window_rc2.png`（窗口，含 2 张已配置 + 未配置卡）
   - `docs/screenshots/provider_settings_entry_rc2.png`（Settings 弹窗，含新入口按钮）

## 2. 验证矩阵（任务要求的 6 项）

| # | 验证项 | 结果 | 证据 |
|---|---|---|---|
| 1 | 无 Key 状态显示正确 | **PASS** | 打开即 `○ 未配置` + `来源：missing（未设置）` + 空 Key 行 + `[添加密钥]`；二次点击入口复用同一窗口（app 语义） |
| 2 | 添加 Key 后状态刷新 | **PASS** | 点击卡片`[添加密钥]`（QInputDialog mock 输入合成值）→ store 落值 → reload 调用一次 → 卡片变 `● 已配置` + 掩码 + `来源：credential_store` |
| 3 | 替换 Key 生效 | **PASS** | `[替换密钥]` → store 值更新为新合成值 → reload 调用 → `resolve_setting()` 按真实优先级链解析出**新值**（"生效"以适配器实际解析口径证明） |
| 4 | 删除 Key 生效 | **PASS** | `[删除密钥]` → store 清空 → 卡片回 `○ 未配置` + `来源：missing` |
| 5 | RuntimeBus 刷新正常 | **PASS** | `reload_default_routers()` → publisher → `providers.updated` 事件 → 打开中的窗口自动刷新（刷新计数递增）；无关事件与退订后不刷新 |
| 6 | Provider 来源显示正确 | **PASS** | 四值全矩阵：environment（Phase 3A 测试）> credential_store > dotenv > missing，逐层翻转断言；UI 来源文案附中文释义（"本机存储"/".env 文件回退"/"未设置"） |

## 3. 问题类检查（任务列出的 5 类）

| 问题类 | 结论 | 依据 |
|---|---|---|
| UI 入口不可发现 | **未发现** | Settings AI STATUS 区底部"AI 模型管理"按钮（objectName `openProviderManager`），含 tooltip"添加或替换模型 API Key（保存在本机用户目录，自动热更新）"；截图可见且未挤压既有设置项 |
| 弹窗尺寸异常 | **未发现** | 520×640 + 滚动区；10 张卡片全部可达；测试锁定尺寸界（420–1200 × 480–1000） |
| 中文显示异常 | **未发现** | 截图逐项检视：标题/提示语/状态（● 已配置 ○ 未配置）/来源（来源：…（本机存储））/按钮（替换密钥·删除密钥·添加密钥）全部正确渲染，无豆腐块；测试断言关键中文 token 在控件文本中存在 |
| Key 显示泄漏 | **未发现** | 已配置卡恒显 `************`；添加/替换后遍历窗口全部 QLabel 断言新旧合成明文零出现；输入提示永不预填已存值；Phase 3A 状态层本身不返回值 |
| 保存后状态不刷新 | **未发现** | 保存/删除后立即 `refresh()`；RuntimeBus 事件双通道兜底（§2 第 5 项） |

## 4. 测试结果

| 套件 | 结果 |
|---|---:|
| `tests/test_provider_manager_manual_flow.py`（新增） | **9 passed** |
| 既有 rc2 集合（Phase 3A/3B/2/1 + Provider 三件套 + Memory 边界 + screen_vision + UI consolidation） | 157 passed |
| **合计** | **166 passed / 0 failed**（10.97s）；`import app` 冒烟 OK（前阶段已验） |

新增 9 项：人工路径开窗（含二次点击单实例复用）、添加/替换/删除三流程、RuntimeBus 刷新、入口可发现、尺寸界、中文标签、明文零泄漏。

## 5. 发现与建议（非阻断）

| # | 级别 | 发现 | 建议 |
|---|---|---|---|
| 1 | 低 | environment 来源下编辑保存不生效（env 优先级最高，设计如此） | UI 已有提示语；rc3 可在 env 覆盖时禁用该行保存按钮并标注 |
| 2 | 低 | 删除密钥无二次确认（可随时重新添加） | 如需确认对话框为单行改动，建议体验走查后决定 |
| 3 | 低 | deprecated（dashscope）行显示为可操作的"未配置"卡（enabled 标志仅供置灰） | rc3：deprecated 行隐藏动作按钮 |
| 4 | 信息 | 共享凭据（TJULLM_API_KEY）的 text/vision/reasoning 三行会同时翻转状态 | 符合共享语义；UI 后续可按凭据分组展示 |
| 5 | 信息 | 根目录遗留 `pt_*.log`/`pt_report.xml` 验证临时文件 | 未跟踪、不阻塞；建议收尾清理（本阶段未删除） |

## 6. rc2 UX 验收判断

**PASS（可进入 rc2 收尾）。** 人工路径全链路（入口 → 开窗 → 无 Key 提示 → 添加 → 替换 → 删除 → 事件刷新）在 mock 隔离下行为全部符合预期；五类问题逐项排查未发现真实缺陷；真实渲染像素佐证中文、掩码与布局质量。既有 140 项基线与全部 Provider/Memory/视觉回归保持全绿（166 passed）。

---

*证据文件：本报告 §1 两张截图（已入库 docs/screenshots/）；测试命令 `pytest tests/test_provider_manager_manual_flow.py tests/test_provider_manager.py tests/test_provider_manager_ui.py tests/test_provider_reload.py tests/test_credential_store.py tests/test_provider_router.py tests/test_provider_catalog.py tests/test_provider_status.py tests/test_companion_memory_boundary.py tests/test_write_guards.py tests/test_screen_vision_routing.py tests/test_capture_semantics.py tests/test_ui_consolidation_p0.py -q` → 166 passed。未执行 git add/commit。*
