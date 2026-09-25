# Firefly User Data Migration Report

日期：2026-09-17  
角色：Firefly_AI_Pet Release Engineer  
范围：用户数据路径统一、旧版非破坏迁移、持久化回归；不包含 UI、Memory 规则或 Agent 行为变更。

## 1. 结果

已将生产默认用户数据根统一为：

```text
%LOCALAPPDATA%\FireflyAI\
├── runtime\
├── memory\
├── conversation\
├── scratchpad\
├── suggestions\
├── learning\
├── models\
├── logs\
└── plugins\
```

新增 `core/user_paths.py` 作为唯一默认路径定义和首次启动迁移入口。现有 Store 的构造函数、显式 `path` / `storage_dir` 注入和业务方法未改变。

## 2. 修改文件

### 新增

- `core/user_paths.py`
  - `UserDataPaths`：统一目录契约。
  - `initialize_user_data()`：首次启动迁移、校验和报告。
- `tests/test_user_data_migration.py`
  - 覆盖 fresh install、旧 runtime、重启幂等、34 条 Memory、Scratchpad assets、Conversation restore。
- `Firefly_User_Data_Migration_Report.md`

### 路径接线

- 应用与运行时：`app.py`、`state_broker.py`
- Core：
  - `core/agent_platform.py`
  - `core/artifact_store.py`
  - `core/bond_state.py`
  - `core/companion_runtime.py`（仅更新路径说明）
  - `core/conversation_store.py`
  - `core/handoff.py`
  - `core/learning/store.py`
  - `core/plugin_loader.py`
  - `core/quick_ask_metrics.py`
  - `core/scratchpad/store.py`（仅更新默认布局说明）
  - `core/visual_state_filter.py`
- Memory 持久化：
  - `memory/repository.py`
  - `memory/suggestion_store.py`
  - `memory/mem0_adapter.py`
- 发布启动/桥接工具：
  - `tools/firefly_runtime_supervisor.py`
  - `tools/firefly_led_bridge.py`
  - `tools/esp32_serial_bridge_poc.py`
  - `tools/stop_pet.py`
  - `tools/simulate_event.py`

所有修改均限于默认路径、迁移初始化或相关注释；未修改 UI 布局、Memory 写入/检索/权限逻辑、Agent 路由与业务规则。

## 3. 首次启动迁移

`app.main()` 在取得单实例互斥锁后、构造任何 runtime-backed service 前调用迁移。

迁移原则：

1. 检测项目根的旧 `runtime/`。
2. 创建九个规范用户目录。
3. 按域复制到新目录；源文件永不删除。
4. 每个文件先复制到目标侧临时文件，校验 SHA-256 后用 `os.replace` 原子落位。
5. 目标已存在且哈希相同时记为 `already_present`。
6. 目标已存在但内容不同时记为 `conflict_skipped`，不覆盖并报告错误。
7. 完成后写 `%LOCALAPPDATA%\FireflyAI\migration_report.json`。
8. 只有 `status=completed` 的同版本报告才作为后续启动的幂等完成标记；带错误的迁移会在下次启动重试。

迁移报告包括：源/目标路径、文件大小、SHA-256、每项状态、域校验结果、错误列表、是否发现旧 runtime，以及 `source_deleted=false`。

## 4. 旧路径映射

| 旧位置 | 新位置 |
|---|---|
| `runtime/` 的非领域运行状态 | `runtime/` |
| `runtime/companion/memory_records.json` | `memory/memory_records.json` |
| `runtime/companion/backups/` | `memory/backups/` |
| `runtime/companion/migration/` | `memory/migration/` |
| `runtime/memory/` | `memory/` |
| `runtime/memory/models/` | `models/` |
| `runtime/companion/conversation.json` | `conversation/conversation.json` |
| `runtime/companion/bond_state.json` | `conversation/bond_state.json` |
| `runtime/companion/scratchpad/` | `scratchpad/` |
| `runtime/companion/memory_suggestions.json` | `suggestions/memory_suggestions.json` |
| `runtime/learning/`（若旧版存在） | `learning/` |
| 项目 `logs/` | `logs/` |

Learning 当前已经使用 `%LOCALAPPDATA%\FireflyAI\learning`；本次仅让它复用统一路径接口，SQLite Schema 与业务 API 未改变。

## 5. 数据完整性

### Memory

- 当前旧文件：`runtime/companion/memory_records.json`
- 当前实测：34 条记录可由 `JsonMemoryRepository` 完整加载。
- 文件大小：22,728 bytes。
- SHA-256：`753B515F62D23685154616EBA2B839847F8F91AB021A9001A267C5D2D5ADE8C7`
- 迁移校验：JSON 根结构必须包含 `records` 列表；报告记录迁移后数量与 SHA-256。
- 专项测试确认 34 条记录内容集合迁移前后相同。

未修改 MemoryRecord 字段、MemoryService、显式写入边界、Suggestion 确认规则或检索排序。

### Scratchpad

- 迁移保持 `items.json` 和 `assets/` 的相对结构。
- 校验每个带 `asset_path` 的条目在迁移目标中都有对应文件。
- 使用真实 `ScratchpadStore` 重新加载，并验证 asset bytes 可读。
- 当前工作区旧 runtime 中没有 Scratchpad 数据；因此该能力由隔离构造数据验证，没有伪造当前用户数据结论。

### Conversation

- 当前旧 `conversation.json` 大小：130,237 bytes，共 11 个会话。
- 迁移后由真实 `ConversationStore` 重新加载，验证会话 ID、历史消息和自定义标题。
- Conversation Store 公共 API 与窗口规则未改变。

### Learning

- 当前 `%LOCALAPPDATA%\FireflyAI\learning\learning_store.sqlite3` 存在，大小 364,544 bytes。
- 迁移层对发现的 `.sqlite3` 使用只读 URI 执行 `PRAGMA integrity_check`。
- Learning Store 默认路径改为统一接口，Schema、Mastery、Quiz 与 Review 逻辑不变。

## 6. 插件与模型

- 正式默认插件根：`%LOCALAPPDATA%\FireflyAI\plugins`。
- 为保持现有开发环境兼容，`<插件根目录>` 暂时保留为可选的第二发现根；缺失时不影响启动，不是发行硬依赖。
- `FIREFLY_PLUGIN_PATH` 追加机制保持不变。
- Mem0 数据与向量索引进入 `memory/`；嵌入模型缓存进入 `models/`。

## 7. 验证

### 通过

- 新增迁移测试及 Memory / Conversation / Scratchpad / Learning / Plugin 回归：`76 passed`。
- 迁移主组与持久化基础回归：`66 passed`。
- 本次涉及的 Python 文件全部通过 `py_compile`。
- `git diff --check` 未发现空白错误；只输出工作区既有 CRLF/LF 提示。
- 静态扫描未发现生产默认数据路径继续绑定项目 `runtime/` 或 `logs/`；命中仅剩文档说明、迁移源路径和被明确排除的开发 smoke/import 工具。

### 与本次无关的既有失败

扩展相关回归组结果：`84 passed, 1 failed`。失败为：

```text
tests/test_character_conversation_runner.py::test_firefly_short_ask_bypasses_agent_router
AttributeError: SimpleNamespace has no attribute paper_context
```

失败点是现有 PageLens 分支与旧测试桩不匹配，不经过本次路径或迁移代码。依据任务边界未修改 UI/Agent 行为来掩盖该失败。

## 8. 未执行事项

- 未在真实 `%LOCALAPPDATA%` 上主动执行迁移；代码将在下一次正常应用启动时执行。
- 未删除、移动或改写项目目录中的任何旧用户数据。
- 未提交 Git。
- 未修改 UI、Memory 逻辑、Agent 行为或业务功能。

## 9. 状态

USER DATA MIGRATION: READY

