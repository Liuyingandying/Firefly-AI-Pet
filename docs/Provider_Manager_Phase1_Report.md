# Provider Manager Phase 1 实施报告（Credential Store + 解析优先级）

> 日期：2026-09-18
> 依据：`docs/Provider_Manager_Architecture_Report.md`（§2 Credential Store / §7 兼容方案 / §8 rc2 范围）
> 范围：**仅 Phase 1 管道**——新增凭据存储 + `resolve_setting` 优先级层。UI、app.py 注册、ProviderRouter.reload 均属后续阶段，本阶段未实施（运行时行为与此前逐字节一致）。
> 合规：未触碰 UI / Memory / Conversation / Voice / Learning；未读取或输出任何真实 API Key（测试全部使用合成值与临时目录）；未 git commit。

---

## 1. 修改文件

| 文件 | 类型 | 内容 |
|---|---|---|
| `core/credential_store.py` | **新增**（~200 行） | `CredentialStore`：`save/get/delete/list_sources`；stdlib-only（re/json/os/threading/time/uuid/pathlib） |
| `providers/base.py` | 修改（+21 行） | 模块级 `CredentialSource` 协议 + `_credential_source` 槽位 + `register_credential_source()`；`resolve_setting()` 插入 Store 层 |
| `core/user_paths.py` | 修改（+4 行） | `UserDataPaths.credentials` 属性（`<root>/credentials`）。**刻意不加入 `directories()`/`ensure_layout()`**——目录由 `save()` 按需创建，不改变既有布局与迁移语义 |
| `tests/test_credential_store.py` | **新增**（17 项） | 覆盖清单见 §3 |

git 增量核对（`git status --short` 精确匹配）：`M core/user_paths.py`、`M providers/base.py`、`?? core/credential_store.py`、`?? tests/test_credential_store.py`。**无其他文件被触碰。**

### 关键实现决策

1. **依赖倒置**：`providers/base.py` 保持 stdlib-only，不 import `core.credential_store`——只暴露 `register_credential_source(callable)` 槽位；组合根（后续阶段）注册 `store.get`。默认不注册 = 解析行为与旧实现逐字节一致（现有测试零改动全绿验证）。
2. **解析失败降级**：Store 查询异常（损坏/锁死/后端故障）静默降级到 `.env` 层——凭据库故障**永远不会**打断 Provider 调用；与此对称，`_read()` 的 OSError 采取**响亮失败**（不隔离），保证 `save()` 在临时读故障下不可能覆写清空既有凭据。
3. **半写防护**：唯一 tmp 文件（uuid 命名）+ `flush` + `fsync` + `os.replace`（WinError 5 三次退避重试）+ finally 清理 tmp；进程内 RLock 串行化，跨进程靠 `os.replace` 原子性。
4. **损坏隔离**：JSON 解析失败/结构错误 → 重命名为 `credentials.corrupt-<时间戳>-<id>.json` 后按空库继续（内容保留可人工恢复），不崩溃调用方。
5. **键校验**：仅接受 `UPPER_SNAKE` 环境变量风格键（`^[A-Z][A-Z0-9_]*$`），值 strip 后非空且不得含换行；`list_sources()` **只返回键名，永不返回值**（无批量导出值的 API）。
6. **权限**：写后尽力 `chmod 0o600`（Windows 上仅清只读位，如实记录）；DPAPI 值加密为 rc3 预留，不改变本契约。

## 2. 数据流变化

```
resolve_setting(name)                        # providers/base.py（签名/参数不变）
  1. process env                     ── 命中即返回（开发者/CI 行为不变，最高优先级）
  2. credential store.get(name)      ── 新增层；未注册或未命中 → 跳过；异常 → 跳过
     └─ %LOCALAPPDATA%/FireflyAI/credentials/credentials.json
        （经 user_paths.credentials；FIREFLY_USER_DATA_DIR 可整体重定向）
  3. .env（read_env_file，语义不变）  ── 原有回退完整保留
  4. default 参数
```

- 当前**运行时净效果为零**：没有任何代码路径注册 source（app.py 注册属下一阶段），全部现有消费方（三个文本适配器 / screen_vision 链 / status 快照）取值结果与改动前一致。
- 存储位置不变量（测试锁定）：默认路径**永不在仓库树内**；`FIREFLY_USER_DATA_DIR` 重定向后存储随之隔离；`config/`、`runtime/`、`.env` 三个位置零写入（模块契约 + 测试双重保证）。

## 3. 测试结果（全部通过，0 失败）

| 套件 | 结果 | 说明 |
|---|---:|---|
| `tests/test_credential_store.py`（新增） | **17 passed** | 新建/读取/删除/覆盖写、非法输入拒绝、损坏隔离、tmp 清理、list_sources 不泄漏值、默认路径跟随 user data root、**仓库目录零写入**、env>store、store>.env、.env 回退保留、default 兜底、未注册=旧行为、store 故障降级 |
| `tests/test_provider_router.py` + `test_provider_catalog.py` + `test_provider_status.py` | **26 passed** | Provider API/路由/快照回归（API 不变的直接证据） |
| `tests/test_companion_memory_boundary.py` + `test_write_guards.py` | **27 passed** | Memory/写入边界未被波及的回归证据 |
| `tests/test_screen_vision_routing.py` + `test_capture_semantics.py` | **53 passed** | resolve_setting 第二大消费方回归 |
| **合计** | **123 passed / 0 failed** | |

## 4. 风险与后续阶段边界

| # | 风险 | 状态/对策 |
|---|---|---|
| 1 | rc2 明文存储（用户目录 JSON） | 已知且记录（架构报告 §10）；ACL 0600 已尽力；rc3 DPAPI。威胁模型不劣于现状 `.env`，且脱离 Git 可达路径 |
| 2 | 跨进程并发 save | 进程内锁 + `os.replace` 原子性覆盖；极端竞态下后写者胜（可接受：同键人工操作场景） |
| 3 | env 覆盖造成的"改了不生效"困惑 | 优先级已按设计（env 最高）；Phase 2 UI 需实现来源徽标 + env 覆盖时禁写提示（架构报告 §4） |
| 4 | 本阶段后 resolve_setting 多一层调用开销 | 仅当 source 注册且 env 未命中才发生一次 dict 查询；未注册路径零开销 |

### 后续阶段（本报告范围外，按架构报告推进）

- **Phase 2**：`app.py` 启动注册 `default_store().get`；`ui/provider_manager_window.py`；settings_popover 入口；`ProviderRouter.reload()`；`status.py` `_env_available` 计入 store（3 行）。
- **Phase 3（rc3）**：DPAPI 加密、自定义 OpenAI Compatible 条目、ANTHROPIC_* 纳管。

---

*验证命令：`pytest tests/test_credential_store.py tests/test_provider_router.py tests/test_provider_catalog.py tests/test_provider_status.py tests/test_companion_memory_boundary.py tests/test_write_guards.py tests/test_screen_vision_routing.py tests/test_capture_semantics.py -q`。未执行 git add/commit。*
