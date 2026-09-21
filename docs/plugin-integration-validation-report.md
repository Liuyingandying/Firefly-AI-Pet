# Plugin Integration Validation Report

- 日期：2026-09-19 · 分支：`plugins-integration` · HEAD：`facb610`
- 提交链：`7d40648`（main 基线，未动）→ `05a08f6`（subtree 合并 extensions/，26 files +1997）→ `facb610`（docs 生态指南 + README 增补，2 files +69）
- 结论：**A–E 全部 PASS，建议 push**（`git push origin plugins-integration`）

---

## 1. 集成架构

```
GitHub Firefly-AI-Pet
├── main                 @ 7d40648   （稳定版，本次未动一字）
└── plugins-integration  @ facb610
    ├── extensions/                        ← git subtree 合并（来源 Firefly-AI-Pet-Plugins main @ a1604eb）
    │   ├── firefly_camera_vision/  ├── firefly_video_extension/
    │   ├── learning_focus/         ├── tju_info_retrieval/（仅 INTERFACE.md，契约分发）
    │   ├── pagelens_bridge/（既有 PageLens，零冲突共存）
    │   └── README.md / LICENSE / INTERFACE.md / docs/
    └── docs/plugin_ecosystem.md           ← 生态指南（契约/装载/比赛展示）
独立仓 Firefly-AI-Pet-Plugins（GitHub）      ← 原样保留：main @ a1604eb、tag v1.0.0、Release+PDF、独立维护
```

运行时边界：插件**装载根** = `%LOCALAPPDATA%/FireflyAI/plugins`（或 `FIREFLY_PLUGIN_ROOT`/path_config 覆盖）；仓库 `extensions/` 仅为开发镜像，不被 plugin_loader 扫描。

## 2. A–E 测试结果

### A. 主程序启动验证 — PASS

| 断言 | 证据 |
|---|---|
| app.py 正常启动 | stop→冷启动循环：4 个全新 pythonw 进程（启动时间 13:39:49–50） |
| 桌宠 UI 正常 | 全屏截图（staging/_m1_acceptance_desktop.png）：流萤快捷提问气泡 + 工具栏 + 托盘图标完整渲染 |
| Provider 路由正常 | `core/provider_state.json`：`current_provider="tju"`，failures tju=0 / zhipu=0 |
| Memory 正常 | 用户数据 memory/ 目录 4 项在位 |
| Conversation 正常 | conversation/conversation.json 在位（mtime 随活动写入，无会话故为历史时间——符合设计） |
| 硬件链正常 | led_state.json 6s 前刷新、hardware_state.json 63s 前；状态机 idle/hook |

### B. 插件目录隔离验证 — PASS

- `discover_roots()` 实测返回：仅 `[E:\Firefly_AI_Private_Plugins]`（工作树既有开发者 WIP 覆盖）——**extensions/ 不在发现根中**
- 已提交 `config/path_config.yaml`：`plugin_root: ""` → 便携默认 `%LOCALAPPDATA%/FireflyAI/plugins` ✓
- `MANAGED_PLUGIN_IDS` 原样（4 id 白名单）✓
- 结论：**源码目录插件 ≠ 用户运行插件**，源码侧存在不产生任何运行时行为

### C. 核心功能回归 — PASS（888 passed；12 failed 全部为既存基线，与集成无关）

- 范围：provider router + memory×N + companion×N + learning×N + plugin loader
- 结构零覆盖核验：`git diff 7d40648 facb610 -- core/ ui/ config/` = **空**
- 12 个失败归因（两项证据）：
  - ×4 `test_companion_attachment_limits_v2` + ×1 收集错误：本地环境缺 `openpyxl`（该文件末次改动为历史提交 99dba26，非本分支；CI 环境有依赖不受影响）
  - ×8 learning UI/runtime：与既知失败基线"8 Learning UI"精确吻合（其中 2 个测试文件属未跟踪 WIP 语料 `??`，非本分支内容）
  - 反证：本分支 diff 对 core/ui/config/tests 全为零改动，不可能引入测试失败

### D. 内容一致性验证 — PASS

CRLF 归一化后逐文件 SHA256 比对：`extensions/` vs `Firefly-AI-Pet-Plugins` 克隆——**26 文件全部一致**（四插件目录 + LICENSE + INTERFACE.md + docs/ + README）；`extensions/docs/` 与插件仓 `docs/` 逐字节一致。

### E. 安全扫描 — PASS

扫描两个新增提交的全部 28 个变更文件（26 subtree + README + docs 指南）：

| 禁出项 | 结果 |
|---|---|
| C:\Users 真实用户名 / FAJ | 0 |
| E:\ / D:\ 路径 | 0 |
| API Key / token 实值 | 0 |
| .env 文件 | 0 |
| 用户数据 | 0 |

允许项按策略放行：占位符（`C:\Users\<用户名>` 类）、测试假数据（memory 红线测试 sk- 样本）。

## 3. 修改文件统计

| 提交 | 文件数 | 规模 | 范围 |
|---|---|---|---|
| 05a08f6（subtree） | 26 | +1,997 | 全部 `extensions/` |
| facb610（docs） | 2 | +69 | 仅 `README.md`(+4) 与 `docs/plugin_ecosystem.md`(新) |
| 合计 | 28 | +2,066 | core/ ui/ config/ tests/ **零改动** |

## 4. Git 提交链

```
main:    7d40648 ──────────────────────────────► (未动)
                    │
plugins-integration:
         7d40648 → 05a08f6 (subtree merge, parents: 7d40648 + a1604eb)
                 → facb610 (docs: plugin ecosystem guide)
```

## 5. 已知限制

1. **subtree add 语法偏差**：`git subtree add` 对已存在前缀硬拒绝（extensions/ 既有 PageLens），改用等价手动合并（`-s ours --no-commit` + `read-tree --prefix`），产生双亲合并提交（保留插件仓溯源）。后续同步用 `git fetch <plugins-url> main && git merge -Xsubtree=extensions` 或同法重做。
2. **openpyxl 本地缺失**：1 个 companion 测试文件收集失败 + 4 个 xlsx 用例失败，属本机依赖缺口（CI 不受影响），与集成无关；可在本机 `pip install openpyxl` 消除。
3. **extensions/ 为混合语义目录**（浏览器扩展 + 能力插件），已在 docs/plugin_ecosystem.md 以双类对照说明。
4. **工作区脏文件**：388 项既有 WIP 保持未提交未推送（含 19 个 tracked 修改与 hardware/ 等本会话产出），不在本分支交付范围。
5. **分支未推送**：`plugins-integration` 目前仅在本地。

## 6. 是否建议 push

**建议 push**：`git push origin plugins-integration`（需带代理 `git -c http.proxy=http://127.0.0.1:7890 push origin plugins-integration`）。A–E 全过、范围纯净、双 Release/tag 零触碰、独立仓四要素（Release/v1.0.0 tag/README/独立维护）原样保留。
