# Firefly AI Pet v1.0-rc2 — 用户安装验证报告（RC2_User_Install_Test）

> 日期：2026-09-18
> 目的：以**普通用户视角**验证发布包 `Firefly_AI_Pet_v1.0-rc2_win64.zip` 的"解压 → 首启 → 配置密钥 → 重启恢复"完整旅程。
> 环境：干净用户目录 `<用户安装测试目录>\`（zip 直接解压，未触碰源码目录/venv）；数据根经 `FIREFLY_USER_DATA_DIR` 指向 `<用户安装测试目录>\userdata\`（既模拟用户数据目录、又保证与真实数据零混淆）。
> 合成凭据：`sk-rc2-user-test-dummy`（DeepSeek 卡）；无真实 Key；无真实 API 调用；未修改任何代码；未 git commit/push。

---

## 1. 安装与首启

| 步骤 | 结果 |
|---|---|
| 解压 `Firefly_AI_Pet_v1.0-rc2_win64.zip` → 干净目录 | **PASS**（`Expand-Archive` 成功；zip 本体可用性连带验证；解压后结构：exe + `_internal/` + `extensions/` + `.env.example` + `RELEASE_NOTES_v1.0-rc2.md`） |
| 首次启动（互斥体空闲时） | **PASS**（exit 0 的两次"秒退"均系单实例互斥被占——一次为用户源码实例、一次为残留 debug 实例；互斥体空闲后即正常存活，与设计一致） |

**单实例处理披露**：用户源码实例（13:18 启动）持有互斥体阻塞了本次验证；经确认后按上轮授权以 `stop_pet.py` 优雅关闭（双信号；ESP32 桥随 supervisor 一并退出），测试结束后已用 `start_pet.ps1` **恢复源码实例**（4 个 pythonw 在线，其 16:17 对真实数据目录的写入为恢复后的自身正常行为，非测试污染）。

## 2. 验证矩阵（任务要求的 8 项）

| # | 验证项 | 结果 | 证据 |
|---|---|---|---|
| 1 | exe 启动 | **PASS** | 测试实例存活（pid 30416 → 重启后 24568），内存 ~220MB 稳定 |
| 2 | 托盘 | **PASS** | 常驻形态成立（托盘图标 + 后台驻留）；窗口枚举见 PetOverlay/Toolbar/AgentDock/Console 四窗在册 |
| 3 | 流萤动画 | **PASS** | 桌面宠物持续渲染，多截屏姿态/眼睑帧变化 |
| 4 | 设置入口 | **PASS** | 宠物点击唤出工具栏 → ⚙ → Settings 弹窗完整（GENERAL/MEMORY/AI STATUS），AI STATUS 正确显示全部 `○ (unavailable)`（无 Key 环境） |
| 5 | AI 模型管理 | **PASS** | 弹窗底部"AI 模型管理"按钮点击 → **"AI 模型设置"窗口打开**（UIA ClassName=`ProviderManagerWindow` + 标题双确认）——Phase 3B 冻结懒加载导入验证通过 |
| 6 | dummy key 保存 | **PASS** | 卡片"添加密钥"→ QInputDialog → UIA ValuePattern 注入 `sk-rc2-user-test-dummy` → OK → **store 落盘**：`userdata/credentials/credentials.json` 出现 `DEEPSEEK_API_KEY`（16:12:21）；卡片即时翻转"● 已配置"（截图 frame-3ed4cd13） |
| 7 | 重启恢复 | **PASS** | stop → 重启 → 重开窗口：UIA 断言 `PASS: manager window reopened after restart` + `persistence rendered, id=…providerCard.replaceKey-deepseek`——持久化凭据在重启后正确渲染为已配置 |
| 8 | userdata 隔离 | **PASS** | 全部测试数据落 `<用户安装测试目录>\userdata\`（memory/credentials/conversation/learning/logs/runtime…）；真实目录 `%LOCALAPPDATA%\FireflyAI` 在测试窗口期内**零写入**（16:17 后的写入全部来自恢复后的用户源码实例自身） |

补充验证（同轮完成）：无 Key 状态下 AI STATUS 全部 `○ (unavailable)`、未配置卡均显示"添加密钥"、已配置卡显示掩码 `************`（明文零渲染——截图 frame-3ed4cd13 逐卡检视）。

## 3. 过程发现

| # | 级别 | 发现 | 定性 |
|---|---|---|---|
| 1 | 中 | **stop_pet 单信号不足**：quit 后进程可存活数十秒甚至需要第二次信号；期间新启动会因互斥体被占而 exit 0 | 已知问题（rc2 冒烟亦记录）；建议 rc3 排查退出时序 |
| 2 | 低 | 卡片"添加密钥"按钮对 **UIA InvokePattern 无响应**（真点击有效）——导致自动化需坐标+对话框 UIA 混合驱动 | 自动化工具限制，非产品缺陷；产品内真点击路径正常 |
| 3 | 低 | 点击序列曾命中 deprecated（DashScope）卡的添加密钥——其对话框可正常打开 | deprecated 行动作按钮建议 rc3 隐藏（UX Audit 已记） |
| 4 | 信息 | 测试中一次误点落在用户浏览器页面（无输入、无提交，无实际影响） | 已即时停止盲点击，改用 UIA/精准矩形驱动 |

## 4. rc2 发布判断

**PASS。** 发布 zip 的"解压即用"旅程在干净用户目录下完整成立：启动 → 无 Key 正确降级与状态显示 → 设置入口 → AI 模型管理窗口 → UI 保存 dummy Key → 重启后凭据持久恢复 → 数据全部落在隔离的用户数据目录。结合 `Packaging_RC2_Smoke_Report.md`（Memory 边界、无 Key 降级、userdata 隔离在冻结包上的既证结论），**rc2 具备发布条件**。

发布收尾清单（供上传 GitHub Release 时使用）：
- `dist/Firefly_AI_Pet_v1.0-rc2_win64.zip`（210.2 MB）
- `dist/Firefly_AI_Pet_v1.0-rc2_win64.zip.sha256`
  （`7bcd112160792b3fc611a4cc62946c539dbc1e0576a0fa099972568761175078`）
- Release Notes：包内 `RELEASE_NOTES_v1.0-rc2.md` / 仓库 `docs/release_notes_v1.0-rc2.md`

## 5. 遗留（均不阻塞）

1. stop_pet 双信号问题（rc3 排查退出时序）。
2. 卡片按钮的 UIA Invoke 兼容（对辅助功能有利，可作 rc3 无障碍增强项）。
3. fastembed 首启下载：建议发布 zip 预置模型缓存（~30MB）或 README 强调 `HF_ENDPOINT` 镜像（Packaging Plan R5）。
4. 仓库根遗留 `pt_*.log` 等验证临时文件（未跟踪；建议随 rc2 收尾统一清理，本阶段未删除）。

---

*证据链：桌面截屏 frame-e1e9966e（首启无 Key 状态）/ frame-0d522a2f（AI 模型设置窗口+对话框）/ frame-3ed4cd13（保存后已配置渲染）；UIA 断言输出（本文 §2 #5-#7）；store 文件时间戳 16:12:21。测试后现场已恢复：测试实例已停止、用户源码实例已重启。未执行 git add/commit/push。*
