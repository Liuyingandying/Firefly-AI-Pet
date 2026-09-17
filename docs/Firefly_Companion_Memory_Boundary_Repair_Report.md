# Firefly Companion Memory Boundary Repair v1 Report

**最终状态：COMPANION MEMORY BOUNDARY REPAIR V1: READY**

日期：2026-09-17 · 前置：M3B.5 Drift Audit（STATE C 判定）
约束遵守：未关闭 auto extraction / 未删除 Companion Mode / 未重构 Memory 架构 / 未触碰 runtime 真实数据

---

## 1. Phase 1 — 真实写入调用链（只读审计）

修复前存在**三条**绕过显式同意边界的自动写入链：

| # | 调用链 | 绕过机制 |
| --- | --- | --- |
| W1 | `chat()` → `_try_extract_suggestions()` → `MemoryCandidateExtractor`（LLM）→ 候选 → `_passes_companion_gate()` → **`memory_service.remember(trigger="companion_auto", asserted_explicit=True)`** → Repository | `asserted_explicit=True` 冒充用户显式请求（实测在 EXPLICIT_ONLY 下同样 `created`） |
| W2 | 会话切换 → `ConsoleConsole._summarize_session_before_switch()` → **`memory_service.remember("会话摘要：…", trigger="conversation_summary", asserted_explicit=True)`** → Repository | 同上（≥4 turn 自动触发） |
| W3 | config `memory.write_policy="auto"`（外部会话新建的 config 显式改写冻结默认） | 服务级策略门从 EXPLICIT_ONLY 变 AUTO |

所有 `asserted_explicit=True` 生产调用点清单（修复前）：`suggestion_service.py:181`（W1）、`console.py:1095`（W2）、`memory_manager.py:58` / `memory_manager.py:413`（Memory Manager 用户确认，**合法**，保留）、`extension_api.py:111`（插件显式观察，合法，保留）。

## 2. Phase 2 — 权限隔离实现

### 2.1 MemoryService 硬守卫（防线 1，永久生效）

`memory/service.py` 新增：

```python
AUTO_SOURCE_TRIGGERS = frozenset({"companion_auto", "conversation_summary"})
```

`remember_detailed()` 入口处：`trigger` 命中即**直接返回 None**（无论 `asserted_explicit` 为何值、无论 write_policy）。这是不可绕过的机器来源边界——即使未来某处调用忘改，也写不进 Repository。实测：`companion_auto` / `conversation_summary` + `asserted_explicit=True` → 拒绝；`explicit-command` + `asserted_explicit=True`（用户真说了"记住"）→ 正常写入。

### 2.2 自动直写分支移除（防线 2，调用层）

`suggestion_service.extract_candidates()` 内的 `auto_write_enabled` 直写分支（W1 核心）整体移除。替代语义：候选全部进入 pending；companion gate 仍然有效——**过门候选排在 pending 最前**（连续陪伴 UX），并附带小幅 confidence 提升。`auto_write_enabled` 配置字段保留但语义降级为"过门候选置顶标记"（不再引发任何写入）。

### 2.3 会话摘要摘除（W2）

`ui/v2/console.py::_summarize_session_before_switch` 移除 `memory_service.remember(...)` 调用——摘要仅进入 `ConversationStore.set_summary()`（history/context），不再写长期记忆。

### 2.4 保留的合法写入路径

- **A 用户显式**："记住…/以后…" → detector/`authorize_explicit_write` → `explicit-command` 写入（不变）；
- **B Memory Manager 用户确认**：MemoryPanel「待确认」tab 的 接受/拒绝 → `SuggestionService.accept()/reject()` → `remember(trigger="suggestion:<reason>", asserted_explicit=True)`（用户真实点击确认 = 合法断言，保留）。

## 3. Phase 3 — source 语义

`MemorySuggestion` 新增字段（含 `to_dict` 序列化）：

| 字段 | 取值 | 语义 |
| --- | --- | --- |
| `id` | uuid hex | 候选唯一标识 |
| `source` | `explicit` / `companion_auto` / `conversation_summary` | explicit=用户明确输入（detector 规则命中"记住"类）；companion_auto=LLM 抽取候选；conversation_summary=会话摘要（模型层已定义，当前 console 不再产出摘要候选） |
| `status` | `pending` / `accepted` / `rejected` | 用户决策生命周期（`SuggestionService._decisions` 记录） |
| `created_at` | epoch ms | 候选产生时间 |

**禁止自动来源使用 explicit**：抽取候选统一 `source="companion_auto"` + `status="pending"`，有测试断言 `"explicit" not in source`。

## 4. Phase 4 — Candidate/Suggestion 接口

确认面**已存在**（无需新建）：MemoryPanel「待确认」tab（`ui/memory_panel.py:238-250`）+ `SuggestionService.list_pending()/accept()/reject()`。本次补齐：

- 候选流转：自动抽取 → `pending`（过门置顶）→ 用户 接受 = `remember()` 写入 + `status=accepted`；拒绝 = 不写 + `status=rejected`（决策记录在 `_decisions`）。
- P5A-3 auto-approve（显式候选自动批准）在生产 config 中本就关闭（`explicit_auto_approve_enabled` 默认 False），未改动。
- **未实现**（按任务边界）：pending 的跨重启持久化（维持 v1 内存语义）；Experience 转换。

## 5. Phase 5 — 数据保护

- **runtime 真实数据零触碰**：修复只改代码；`runtime/companion/memory_records.json` 在修复后全部测试与回归中 mtime 未变（仍为外部迁移时点 09-16 23:40），4 条现有记录未删除、未修改。
- **无数据迁移**：pending suggestions 维持内存语义（v1 设计），无格式变更 → 无需迁移备份。若未来实现 pending 持久化，按本节要求先备份。

## 6. 测试结果（`tests/test_companion_memory_boundary.py`，8 项全绿）

| # | 验证 | 结果 |
| --- | --- | --- |
| 1 | **100 轮普通聊天**（生产 config auto 全开 + 每轮真实 JSON 候选抽取） | Memory **0 增长**（文件哈希不变）；extraction 100/100 照常运行；候选全部进 pending ✅ |
| 2 | **"记住我喜欢喝茶"** | 正常写入（trigger=explicit-command，1 条）✅ |
| 3 | **companion_auto** | 抽取候选 `source=companion_auto/status=pending` 进 pending；Memory 0 增长；`remember_detailed(trigger="companion_auto", asserted_explicit=True)` 被硬守卫拒绝 ✅ |
| 4 | **conversation_summary** | 摘要仅进 ConversationStore（静态契约：方法内无 remember/asserted_explicit）；`conversation_summary` 触发写入被守卫拒绝 ✅ |
| 5 | **voice pipeline** | 5 个语音测试文件：36 passed / 15 skipped（跳过为环境性），零回归 ✅ |
| extra | AUTO_SOURCE_TRIGGERS 常量冻结 / pending→确认→写入(path B) / 拒绝不写 / source 语义 / 自动来源无 explicit | 全过 ✅ |

## 7. 回归

全量 `pytest tests/ -q`（--ignore 4 个缺依赖文件）：

**13 failed / 2471 passed / 22 skipped** —— 与 Scratchpad v1 后基线**逐项一致**
（8 Learning UI + 2 app.py WIP + 3 真实视频；桥互斥体项因 ESP32 桥停止而环境性通过）。
零新增失败。

## 8. 修改文件清单

| 文件 | 修改 |
| --- | --- |
| `memory/service.py` | `AUTO_SOURCE_TRIGGERS` 常量 + `remember_detailed` 硬守卫；补 `logger` 定义（修复既有 `log.*` 潜在 NameError） |
| `memory/suggestion/suggestion_service.py` | 移除 companion_auto 直写分支；候选置顶/标记；accept/reject 状态记录 |
| `memory/suggestion/memory_candidate_detector.py` | `MemorySuggestion` 增 `id/source/status/created_at`（默认值向后兼容） |
| `ui/v2/console.py` | 会话摘要移除 memory 直写（保留 set_summary） |
| `tests/test_companion_memory_boundary.py` | 新增（8 项） |

**未触碰**：MemoryRepository / memory_records.json / Qdrant / Recall Gate / Ranking / Persona / BondState / LearningStore / 语音链路 / Companion Mode 存在性 / auto extraction 开关。

## 9. 遗留与建议

1. **Config 残留**：`config/companion.json` 的 `memory.write_policy="auto"` 与 `auto_write_enabled=true` 仍在（用户资产，本任务不修改）。二者语义已分别降级（策略对 AUTO 触发器无效；flag 不再引发写入），但建议用户在了解后自行决定是否改回 `EXPLICIT_ONLY` / `false`。
2. **待确认 tab 是唯一确认面**：用户需通过 MemoryPanel → 待确认 处理候选；建议后续在桌宠气泡加未确认数提醒（超出本阶段）。
3. **pending 不持久化**：重启后未确认候选丢失（v1 设计不变）；若要持久化需新任务书 + 数据备份。
4. 外部会话的 v1.1 文档（`docs/Memory_Companion_Mode_v1.1.md`）与 `Memory_Current_Audit.md` 建议归档到 docs/history，避免与当前契约混淆。

---

**COMPANION MEMORY BOUNDARY REPAIR V1: READY**
