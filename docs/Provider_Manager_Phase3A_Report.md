# Provider Manager Phase 3A 实施报告（Provider 状态查询层）

> 日期：2026-09-18
> 依据：`docs/Provider_Manager_Architecture_Report.md` 与 Phase 1/2 报告
> 范围：只读状态查询层 `core/provider_manager.py`——为模型设置 UI 提供统一状态数据。UI 本身、Key 写入界面仍属后续阶段。
> 合规：未修改 UI / Memory / Conversation / Voice / Learning / Provider API；未读取或输出任何真实 Key；未 git commit。

---

## 1. 修改文件

| 文件 | 类型 | 内容 |
|---|---|---|
| `core/provider_manager.py` | **新增**（~170 行） | `ProviderManager`：`list_providers()` / `get_provider_status(id)` / `state_version()` / `connect_runtime_bus(bus)`；4 个 source 常量与 `PROVIDERS_UPDATED_EVENT` |
| `tests/test_provider_manager.py` | **新增**（9 项） | 覆盖清单见 §3 |

依赖方向：`core/provider_manager.py → {core.credential_store, core.providers.catalog, providers.base}`（与 ai_router 同向，`providers` 不反向依赖 core 的新模块）。

## 2. API 与数据流

### 2.1 `list_providers()`

每行结构（键集合被测试锁定）：

```json
{
  "id": "tju",
  "display_name": "TJU LLM",
  "credential_key": "TJULLM_API_KEY",
  "configured": true,
  "source": "credential_store",
  "enabled": true
}
```

- **catalog 复用**：行集合 = `CATALOG` 中 `credential_env` 非空且非 coding_agent 的条目（自动排除 claude-cli/codex-cli；deprecated 的 dashscope 以 `enabled=False` 行呈现供 UI 置灰）。**没有复制任何 provider 清单**——id/状态/凭据绑定全部来自 catalog。
- **source 四值**：`environment` > `credential_store` > `dotenv` > `missing`，检测顺序与 `resolve_setting` 完全一致（同名多键如 text/vision 共用 `TJULLM_API_KEY` 时按层扫描）。
- **Key 永不返回**：行内只有布尔与来源标签；`list_sources` 式的键名枚举也不在此层。测试用 json 全文序列化断言三个合成 secret 值零泄漏。
- `display_name` 为展示层元数据（provider_manager 内映射表，未知 id 回退 id 派生），不影响 catalog 单一事实源。

### 2.2 `get_provider_status(provider_id)`

返回 `{"configured": bool, "source": str}`；未知 id 与无凭据条目（claude-cli/codex-cli）抛 `KeyError`——调用方不可能拿到含糊的假状态。

### 2.3 RuntimeBus 接入

```
保存凭据（Phase 3B UI）
  → reload_default_routers()                 # Phase 2
  → set_updated_publisher → bus.publish_event("providers.updated")   # Phase 2 已接线
  → ProviderManager.connect_runtime_bus(bus) # 本阶段：订阅计数 state_version
  → UI 轮询/订阅 state_version 变化后重新 list_providers()/get_provider_status()
```

- 查询本身是**活查询**（每次调用实时探测 env/store/dotenv），因此事件仅作为廉价的"变化已发生"信号：`state_version()` 单调递增，UI 无需缓存失效逻辑。
- 非本类事件（如 `camera.observed`）不计数；`connect_runtime_bus` 返回退订函数。

## 3. 测试结果

| 套件 | 结果 |
|---|---:|
| `tests/test_provider_manager.py`（新增） | **9 passed** |
| Phase 1/2 + Provider 三件套 + Memory 边界 + screen_vision 全量回归 | 131 passed |
| **合计** | **140 passed / 0 failed** |

9 项新测试与任务要求对应：
1. **已保存 Key 显示 configured**：`credential_store` 来源（含 text/vision 共键联动）；
2. **未配置显示 missing**：fresh 状态 + deprecated 行 `enabled=False` 置灰语义；
3. **env 优先级正确显示**：env > store > dotenv 三层全矩阵（`test_provider_precedence_order_full_matrix`）；
4. **不泄漏 key**：三个合成 secret 经 `json.dumps` 全文断言零出现 + 行键集合锁定；
5. **reload 后状态更新**：真实 `RuntimeBus`（QApplication + 事件泵）驱动——`reload_default_routers()` 发布事件 → `state_version` 递增、查询显示新 source；无关事件不计数；退订后不再计数。

附加：catalog 复用断言（行集合 ≡ catalog 推导集合、CLI 排除）与未知/CLI id 的 `KeyError` 行为。

## 4. 风险与边界

| # | 风险 | 状态 |
|---|---|---|
| 1 | `read_env_file` 每次查询重读 `.env` | 状态页查询频率极低（UI 打开/事件后），可接受；后续 UI 若做轮询可加节流 |
| 2 | dotenv 层检测对真实 `.env` 的依赖（打包态在 `_internal/.env`） | 与 resolve_setting 现行为一致；便携用户主路径是 store，dotenv 仅回退 |
| 3 | `state_version` 为进程内计数，多进程不共享 | 设计如此（UI 与路由同进程）；跨进程一致性由 store 文件为准 |
| 4 | display_name 映射表新增 provider 时可能滞后 | 未知 id 自动回退派生标签，不阻塞 |

### 后续阶段（范围外）

- **Phase 3B（UI）**：`ui/provider_manager_window.py`（本层驱动列表/来源徽标）+ settings_popover 入口 + 保存流（`store.save → reload_default_routers → 总线`，均已有 API）+ `providers/status.py` `_env_available` 计入 store（3 行）。

---

*验证命令：`pytest tests/test_provider_manager.py tests/test_provider_reload.py tests/test_credential_store.py tests/test_provider_router.py tests/test_provider_catalog.py tests/test_provider_status.py tests/test_companion_memory_boundary.py tests/test_write_guards.py tests/test_screen_vision_routing.py tests/test_capture_semantics.py -q` → 140 passed。未执行 git add/commit。*
