# Provider Manager Phase 2 实施报告（生命周期接入 + 热更新）

> 日期：2026-09-18
> 依据：`docs/Provider_Manager_Architecture_Report.md`（§5/§6/§7）与 `docs/Provider_Manager_Phase1_Report.md`
> 范围：将 CredentialStore 接入 Provider 生命周期——启动注册、路由热更新、Runtime 通知。UI 仍不在本阶段（Phase 3）。
> 合规：未修改 Memory/Conversation/Voice/Learning；未读取或输出真实 API Key；未修改 `.env`；未向项目目录写入凭据（测试全部走 tmp 隔离）；未 git commit。

---

## 1. 修改文件

| 文件 | 变更 |
|---|---|
| `core/ai_router.py` | ① `ProvidersUpdated`（frozen dataclass：reason/reloaded_routers，供 RuntimeEvent payload）；② 模块级 `_live_routers`（WeakSet+锁，登记所有**默认构造**路由）与 `set_updated_publisher()`；③ `ProviderRouter.__init__` 记录 `_providers_injected` 并登记非注入路由；④ **`ProviderRouter.reload()`**：`_state_lock` 内重建默认适配器三元组、保留健康态；注入实例返回 False 不重建；⑤ `_build_default_providers()` 静态方法（`__init__` 与 reload 共用）；⑥ **`reload_default_routers() -> int`**：遍历存活默认路由逐一 reload，任一重建后触发一次 publisher |
| `app.py` | ① import 扩展（`RuntimeEvent`/`ProvidersUpdated`/`set_updated_publisher`/`default_store`/`register_credential_source`）；② `main()` 在 `initialize_user_data` 之后注册 `register_credential_source(default_store().get)`（先于 VisualShell，覆盖其后一切路由构造）；③ shell 创建后将更新发布器接到 RuntimeBus：`reload_default_routers()` → `RuntimeEvent(kind="providers.updated", source="provider_manager", payload=ProvidersUpdated())` |
| `tests/test_provider_reload.py` | **新增 8 项**（覆盖任务要求的 5 场景 + 注册表/发布器/payload） |

依赖方向核实（任务要求 `core → providers`，禁止 `providers → ui/app`）：`app.py → {core.credential_store, providers.base, core.ai_router, core.runtime_bus}`；`core/ai_router.py → providers.base/providers.*`（无 Qt、无 UI、无 app）；`providers/base.py` 自 Phase 1 起仅持 callable 槽位。✅

## 2. Provider 生命周期变化

```
启动：main()
  ├─ register_credential_source(default_store().get)     # 凭据库进入解析链
  ├─ VisualShell：companion_runtime 等构造 ProviderRouter()
  │    └─ 每个默认构造路由登记进 ai_router._live_routers（WeakSet）
  └─ set_updated_publisher(→ shell.runtime_bus "providers.updated")

Key 变更后（Phase 3 UI 将调用）：
  reload_default_routers()
    ├─ 遍历存活默认路由 → router.reload()
    │    └─ _state_lock 内重建 (TJUQwen/ZhipuGLM/DeepSeek)()  # 凭据在构造时经
    │       resolve_setting 重新解析（env > store > .env）
    │    └─ _state（current_provider/failures）原样保留；进行中请求在旧实例上完成
    └─ 任一重建 → publisher() → RuntimeBus "providers.updated" → UI 状态刷新准备
```

兼容性保持：
- `ProviderRouter(providers=[...])` 注入契约不变——注入路由 `reload()` 返回 False、不进登记表、永不被重建（测试锁定）。
- 适配器/`resolve_setting` 签名零改动；`screen_vision`/`status.py` 等 per-call 消费方自 Phase 1 起自动热。
- `ProvidersUpdated` 是"域名模块自有 frozen dataclass"，满足 RuntimeBus"payload 不得是裸 dict"的总线纪律。

## 3. 测试结果

| 套件 | 结果 |
|---|---:|
| `tests/test_provider_reload.py`（新增） | **8 passed** |
| Phase 1 `test_credential_store.py` | 17 passed |
| Provider 三件套（router/catalog/status） | 26 passed |
| Memory 边界 + 写入守卫 | 27 passed |
| screen_vision routing + capture semantics | 53 passed |
| `import app` / `import core.companion_runtime` 冒烟 | OK（验证新增 import 与 main 接线） |
| **合计** | **131 passed / 0 failed** |

8 项新测试与任务要求一一对应：
1. `test_saved_key_visible_to_provider` — 保存 TJULLM_API_KEY 后 Provider 读到新值（Authorization 头断言）；
2. `test_reload_picks_up_new_key` — 改 Key + reload → 新值生效、实例确实重建（身份断言）；
3. `test_delete_falls_back_to_env_file` — 删除 Key + reload → 回退 `.env` 值；
4. `test_without_store_legacy_behavior` — 无注册 store：幽灵 store 不可见，reload 后仍走 `.env`；
5. `test_reload_preserves_health_state_and_chat` — 注入"首个实例失败"的 tju stub：失败计数 1、current_provider=zhipu；reload 后 `state` 深拷贝相等；后续聊天用新 Key 在 tju 成功；
6. `test_reload_default_routers_covers_all_and_publishes` — 两个默认路由都被重建、publisher 恰触发一次；
7. `test_injected_providers_never_rebuilt` — 注入路由 reload False、不在登记表；
8. `test_providers_updated_payload_is_frozen_dataclass` — 总线 payload 契约。

## 4. 风险

| # | 风险 | 对策/状态 |
|---|---|---|
| 1 | **companion_runtime 自建路由**：两处 `ProviderRouter()` 在 shell 初始化时创建，凭据定格 | `reload_default_routers()` 已覆盖（WeakSet 登记所有默认构造实例）——这是为满足"全部默认路由热更新"而增加模块级函数的原因 |
| 2 | reload 与进行中聊天并发 | 重建在 `_state_lock` 内、仅替换 `self.providers` 元组；进行中请求持有旧适配器引用自然完成（旧凭据语义，可接受并已文档化） |
| 3 | publisher 回调异常 | 发布发生在全部重建完成之后；`publish_event` 对关闭总线为 no-op、QueuedConnection 异步投递，不阻塞保存流程 |
| 4 | `_env_available`（status.py）尚未计入 store | 架构报告 §7 预留的 3 行改动，归入 Phase 3（与 UI 一起交付，避免本阶段扩散）；当前 UI 红点仍只反映进程 env |
| 5 | 测试登记表串扰 | autouse fixture 每用例前后 `clear()` WeakSet 与 publisher；state 文件全部指向 tmp |

---

*验证命令：`pytest tests/test_provider_reload.py tests/test_credential_store.py tests/test_provider_router.py tests/test_provider_catalog.py tests/test_provider_status.py tests/test_companion_memory_boundary.py tests/test_write_guards.py tests/test_screen_vision_routing.py tests/test_capture_semantics.py -q` → 131 passed。未执行 git add/commit；未修改 `.env`；未向项目目录写入任何凭据。*
