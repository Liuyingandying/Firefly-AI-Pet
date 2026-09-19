# Firefly AI Pet v1.0-rc2 — 打包与冻结冒烟报告（Packaging RC2 Smoke）

> 日期：2026-09-18
> 范围：rc2 发布候选包构建 + 冻结态冒烟验收（Provider Manager 全链路为主测项）。未 git commit/push；Memory/Conversation 架构零改动；无真实 API Key（全部合成值）。
> 产物：`dist/Firefly_AI_Pet_rc2/`（PyInstaller onedir，466.3 MB，4,621 模块）
> 关联：`docs/Packaging_Plan_v1.md`、`docs/Packaging_Build_First_Report.md`、Provider Manager Phase 1/2/3A/3B/UX_Audit 报告

---

## 1. 构建更新与结果

| 项 | 值 |
|---|---|
| spec 变更 | ① `hiddenimports` 增补 Provider Manager 三模块（`core.credential_store`、`core.provider_manager`、`ui.provider_manager_window`，显式兜底懒加载导入）；② `COLLECT name='Firefly_AI_Pet_rc2'`（EXE 名不变） |
| `build_windows.ps1` 变更 | `DistDir` 同步为 `dist\Firefly_AI_Pet_rc2` |
| 构建 | ✅ 141.7s；`VERIFY: PASS`（11/11）；PyZ 4,621 模块（+3 = PM 三件全部命中）；warn 505 条与 build#1 相同（全良性）；扩展/`.env.example` 正常落位 |
| 配置红线 | ✅ `_internal/config` 恰 4 个模板；凭据库不进包（位于用户数据根 `credentials/`）；无任何真实 Key 入包 |

## 2. 冻结冒烟环境

- 冒烟根：`<冒烟测试根目录>\FireflySmoke\v1_rc2\`（产物完整副本 + 独立 `userdata\`）
- 启动：`FIREFLY_USER_DATA_DIR` 指向冒烟 userdata；**单实例互斥**曾被两个来源阻塞（首轮：用户源码实例 13:18 启动持有互斥体 → rc2 按设计干净退出 exit 0；二轮：残留 debug 实例）——经用户同意后优雅关闭源码实例（stop_pet 双信号，含 ESP32 桥全部退出）再执行
- 诊断工具：py-spy 0.4.2（运行时栈转储）+ UIA 自动化 + 控制台诊断构建（`firefly_dbg.spec`，临时产物）

## 3. 冻结冒烟验证矩阵

| # | 验证项 | 结果 | 证据 |
|---|---|---|---|
| 1 | exe 启动、进程稳定 | **PASS** | 互斥体空闲后启动即存活（多轮 10s+ 观察与 30 分钟级会话） |
| 2 | 托盘 + 宠物动画 | **PASS** | 桌面宠物常驻顶层；多截屏动画帧变化；AgentDock/工具栏随交互出现隐藏 |
| 3 | 控制台对话（Settings→Ask 路径之外的主聊天面） | **PASS** | "流萤 · AI Pet 控制台"完整渲染，消息收发正常 |
| 4 | 无 Key 降级 | **PASS** | 回复 `(all AI providers failed: tju, zhipu, deepseek)`，无崩溃；AgentDock Qwen 红点 |
| 5 | Memory 边界 | **PASS** | "记住我喜欢rc2测试" → `userdata/memory/memory_records.json` 恰 1 条（explicit-command）；普通聊天后仍 1 条；收尾复核一致 |
| 6 | 数据写入 userdata（隔离） | **PASS** | conversation/memory/credentials/suggestions/scratchpad/learning/logs 全落冒烟 userdata；源码仓库 `runtime/companion` 当日 0 写入 |
| 7 | Settings 入口 → AI 模型管理窗口 | **PASS（修复后）** | 冻结版 Settings 弹窗含"AI 模型管理"按钮；点击后 `ProviderManagerWindow("AI 模型设置")` 真实打开（UIA 类名+标题双确认）——依赖 §4 缺陷修复 |
| 8 | dummy Key 保存（经 UI） | **PASS** | 添加密钥对话框（UIA ValuePattern 注入合成值 `sk-rc2-dummy-key-not-real`）→ 确认 → 卡片翻转"● 已配置 / credential_store"；**共享凭据联动**：GLM Vision（ZHIPU key）同轮翻转 |
| 9 | restart 后恢复 | **PASS** | 保存 → 停止 → 重启 → 重开窗口：TJU LLM / Zhipu GLM 直接呈"● 已配置 + 替换/删除密钥"（store 持久化 + 冻结读取链验证） |
| 10 | verify_dist 禁运扫描 | **PASS** | runtime/logs/明文 Key/sqlite/hardware 真值 0 命中 |

## 4. 冻结冒烟发现的真实缺陷与修复

### 缺陷 D1（🔴 已修复）：AI 模型管理按钮在 windowed 构建中静默无效

- **现象**：冻结 rc2 中点击"AI 模型管理"无任何反应；windowed 构建 stderr 为 NullWriter，异常被完全吞掉（无对话框、无日志、进程存活）。
- **捕获手段**：临时 console 诊断构建（`packaging/firefly_dbg.spec`）+ UIA 复现 → stderr 捕获 traceback。
- **根因**：`app.py::_open_provider_manager` 传 `parent=self`，而 **`VisualShell` 继承 `QObject`，不是 QWidget** → `QDialog.__init__(VisualShell)` TypeError（`app.py:845` → `provider_manager_window.py:198`）。
- **修复**：移除非法 `parent=self`（窗口由 `self._provider_manager_window` 引用持活，生命周期不变；1 行 + 注释）。修复后回归：Phase 3B/3A/UX/consolidation **26+9 passed**，`import app` OK，重建后冻结验证 PASS（§3 #7-9）。
- **定性**：Phase 3B 单测未覆盖"真实组合根传参"，属测试盲区而非回归；此修复为 rc2 必需。

### 缺陷 D2（🟠 缓解方案已验证）：fastembed 首次下载可无限期阻塞对话轮

- **现象**：冻结 rc2 第二轮对话卡死"思考中…"；py-spy 栈定格在 `context_builder._memory → mem0_adapter.search → fastembed → HF snapshot_download`（对 huggingface.co 连接无超时，本机网络直连 HF 受阻——与 GitHub 需代理同因）。
- **根因**：记忆检索在轮次开始时惰性初始化嵌入模型；首启下载依赖 HF 连通性。
- **现场处置**：结束卡住实例 → 用 `HF_ENDPOINT=https://hf-mirror.com` 重启 → 下载完成 → 对话与显式记忆写入全部恢复。
- **建议（P0-adjacent）**：① 发布 zip **预置嵌入模型缓存**（`userdata/memory/models` 种子，~30MB，已验证可离线）；② rc3 为检索层增加下载超时→降级路径；③ README 说明该网络前提。
- **定性**：非 rc2 回归（rc1 同链路同样受影响，rc1 冒烟时模型缓存亦不完整——本次顺带发现）；架构降级设计按预期兜底了"写入仍成功"。

### 过程记录（不影响结论）

- 一次 `stop_pet` 需双信号（已知问题，沿用记录）；一次启动误报"进程未存活"实为 tasklist 过滤时序；冒烟副本曾出现"只拷 exe 未同步 `_internal`"的操作失误（已整目录重拷后重验）。

## 5. 用户会话恢复

冒烟结束后已执行 `start_pet.ps1` **恢复用户的源码实例**（supervisor + app + COM5/COM10 桥，4 个 pythonw 进程在线），冒烟数据隔离于 `<冒烟测试根目录>\FireflySmoke\v1_rc2\userdata\`（收尾核对：记忆恰 1 条合成记录；源码仓库 `runtime/companion` 冒烟时段 0 写入）。

## 6. rc2 发布判断

**PASS（含一项已修复缺陷 D1 + 一项已验证缓解 D2）。**

- 冻结 rc2 在"无 Key + 外部能力全缺席"环境下的完整用户旅程成立：启动 → 托盘/动画 → 控制台对话（优雅降级）→ **Settings → AI 模型管理 → 添加/替换/删除密钥 → 热更新生效** → 重启持久恢复 → Memory 边界与数据隔离全部正确。
- 发布物前置（收尾清单）：① zip 打包 `dist/Firefly_AI_Pet_rc2` + SHA256；② 预置嵌入模型缓存或 README 载明 `HF_ENDPOINT` 镜像/联网首启要求；③ 发行说明载明"解压到用户可写目录、`.env` 位置、隐私出网矩阵（引 Scope v1 §6.1）"；④ 签名评估（沿用 rc1 结论）。
- 遗留 P1（不阻塞）：`_internal` 运行期写入迁移、`app_root()` 垫片、Bridge origin 认证、cv2 体积裁剪。

---

*验证与诊断命令留档：`packaging/verify_dist.py --dist dist/Firefly_AI_Pet_rc2`；py-spy 栈转储 `pt_spyR*.txt`；console 诊断日志 `pt_dbg_err.log`。本阶段唯一的生产行为变更为 D1 的一行修复（app.py 移除非法 parent），已冻结回归验证。未执行 git add/commit/push。*
