# Firefly Memory M3B.8 — Memory History Restore 报告

**最终状态：MEMORY HISTORY RESTORE: READY**

日期：2026-09-17 · 前置：M3B.5 Drift Audit（STATE C 判定）
性质：经批准的恢复迁移——把外部清理物理删除的 30 条 superseded 历史从备份恢复回 Repository

---

## 1. 删除原因分析（源自 M3B.5 审计）

09-16 23:27–23:40，外部"Memory v1.1 Companion Mode"会话（留档
`docs/Memory_Companion_Mode_v1.1.md`）执行"Stage 5 清理"：把
33 条（3 active + 30 superseded 重复链）**物理清理为 3 条**，随后其 Companion-Mode
自动写入测试新增 1 条（`trigger=companion_auto`）→ 4 条。清理脚本不在本仓库内
（全库检索无该备份命名 writer、无 superseded 物理清理代码路径），未经 M2 生命周期
设计批准——违反冻结契约 C（"SUPERSEDE ≠ DELETE"）。备份
`memory_records.backup-20260916_232734.json` 由该会话规范生成，33 条完整。

## 2. Backup 来源

`runtime/companion/backups/memory_records.backup-20260916_232734.json`
（mtime 09-16 23:27，sha256 前 20 位 `3e42c0103c2193d2ba2c`，33 = 3 active + 30 superseded，
双链结构：23 条 superseded_by → canonical-1，7 条 → canonical-2/3）。

## 3. Restore Plan（dry-run 输出，`runtime/backups/memory_restore/20260917_191623/restore_plan.json`）

| 分类 | 数量 | 说明 |
| --- | --- | --- |
| add_records | **30** | 全部 `lifecycle_status=superseded`；其中 30 条标注 `content_matches_active_expected`（内容与 canonical 重复是去重链的正常形态，非冲突） |
| skip_records | **3** | `id_exists_in_current`（3 条 active canonical 已在当前库） |
| conflicts | **0** | 无 id 冲突、无"缺失 ACTIVE 记录"类需人工裁决项 |

**规则解释（重要）**：30 条 superseded 的内容哈希与当前 active 相同——这是去重链的
设计形态。M3B.8 的"content 冲突 → 保留当前"规则按其立法目的解释为**防止产生新的
ACTIVE 重复**；superseded 历史与 canonical 内容重复是恢复前的原状，照实恢复。
若按字面执行（内容命中即全部跳过），任务目标"30 条 superseded"不可达——此解释
已在 plan 的 `restore_audit` 字段逐条留痕。

## 4. SHA256 与数据变化

| 时点 | total | active | superseded | sha256(前 20) |
| --- | --- | --- | --- | --- |
| 恢复前（当前生产） | 4 | 4 | 0 | `18ef0cd3d069ad48534c` |
| 恢复后（当前生产） | **34** | **4** | **30** | `753b515f62d23685…` |

- 恢复的 30 条：`created_at` / `updated_ts` / `superseded_by` / `supersede_reason` /
  `lifecycle_status=superseded` 全部原样保留（无 reset active、无合并）；
- 3 条 active canonical：id / content / created_ts / lifecycle **逐字段未变**；
- `companion_auto` 自动记录（第 4 条）**保留** ✓；
- 任务书预期"active = 3"与"companion_auto 保留 + total = 34"在算术上不能同时成立
  （companion_auto 记录本身是 active）——如实报告：active = 4（3 原始 + 1 自动记录）。

## 5. 索引链路（§五 Repository = source of truth）

未直接写 Qdrant。恢复后走标准链路（真实 bge + Qdrant，READ_ONLY 核对）：

```
Repository restore(30 add)
  → consistency_report: repo=34 indexed=34 healthy=34
    missing=0 stale=0 orphan=0        ← 恢复记录的原 vector 在 Qdrant 中仍在
  → reconcile(dry_run=True): missing=0 orphan=0 → 无修复工作
  → reconcile(false)：未执行（dry-run 显示零差异，执行无意义）
```

外部清理只删了 Repository 记录、未动 Qdrant 向量——恢复记录与现存向量天然重配对，
**34/34 全部健康**。

## 6. 数据保护（§六）

执行前生成保护备份 `runtime/backups/memory_restore/20260917_191623/`：
- `memory_records.json`（SHA `18ef0cd3…`）
- `memory_suggestions.json`（当时不存在，记录为 ABSENT）
- `qdrant/`（3 文件，目录哈希 `a2f46d7c…`）
- `SHA256S.txt` 清单

Rollback 路径已由测试证明可执行（`test_rollback_restores_previous_state_byte_identical`）。

## 7. 测试结果（§七）

`tests/test_memory_history_restore.py`：**10 passed**（合成镜像数据集：3 active +
30 superseded 双链 + 1 companion_auto，复刻真实事件形态）：

| # | 场景 | 结果 |
| --- | --- | --- |
| 1 | backup 读取（33 条解析） | ✅ |
| 2 | dry-run 不修改（count + SHA 双断言） | ✅ |
| 3 | restore 只新增缺失（34 = 33 + companion_auto） | ✅ |
| 4 | active 记录逐字段不变（content/category/created_ts/lifecycle/source/trigger） | ✅ |
| 5 | superseded 状态 / superseded_by / reason / created_ts 保持 | ✅ |
| 6 | 链完整（superseded_by 全部可解析） | ✅ |
| 7 | 重启后保持（重建 Repository 34 条） | ✅ |
| 8 | 检索不返回 superseded（active-only 过滤不变） | ✅ |
| 9 | export 包含 34 条历史（30 superseded 在内） | ✅ |
| 10 | rollback 字节级可执行 | ✅ |

## 8. 全量回归

`pytest tests/ -q`（--ignore 4 个缺依赖文件）：
**13 failed / 2498 passed / 22 skipped** —— 失败集与基线核心完全一致
（8 Learning UI + 2 app.py WIP + 3 真实视频；桥互斥体项因硬件桥停止而环境性通过）。
Memory 域 220 项 + Boundary/Suggestion/Scratchpad 全绿，零新增失败。

## 9. 最终生命周期状态

```
total      = 34
active     = 4   （3 条原始 active + 1 条 companion_auto 自动记录，均保留）
superseded = 30  （历史链完整恢复，superseded_by 全部指向存在记录）
```

对 M3B.5 STATE C 的闭环：被物理删除的 30 条历史已从规范备份恢复，
Repository 重新满足冻结契约 C（superseded ≠ delete），且未修改任何 active 内容 /
未改变任何 active id / 未触碰 Retrieval / Recall Gate / 语音链路。

## 10. 已知限制与建议

1. 本恢复**只修历史完整性**：不改变检索行为（superseded 仍被检索过滤）、不激活任何
   旧事实、不动 active 集合——与任务目标一致。
2. 建议把 `runtime/backups/memory_restore/` 纳入常规备份轮换。
3. 外部 v1.1 会话的 Companion Mode 仍在生产运行（suggestion 自动抽取开启）：
   其写入已被 Boundary Repair v1 限制为"候选进 pending + 用户确认"，历史完整性风险
   不复现；但建议后续任务审视 v1.1 文档与 M3B 契约的合并口径。
