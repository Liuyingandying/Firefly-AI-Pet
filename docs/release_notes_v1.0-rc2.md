# Firefly AI Pet v1.0-rc2 — 发行说明

> 发布日期：2026-09-18 · 平台：Windows 10/11 x64 · 形态：免安装便携包
> 本包内容物基于 `dist/Firefly_AI_Pet_rc2/`（PyInstaller onedir）构建与冻结冒烟验收（见仓库 `docs/Packaging_RC2_Smoke_Report.md`）。

---

## 本版重点：Provider Manager（AI 模型管理）

v1.0-rc2 新增**用户可配置的模型凭据管理**，不再依赖手工编辑 `.env`：

- **入口**：宠物 → 工具栏 ⚙ 设置 → "AI 模型管理"。
- **卡片式管理**：每个模型 Provider 一张卡片，显示名称、状态（● 已配置 / ○ 未配置）、凭据来源与本机掩码（`************`，**明文永不显示、输入框永不预填**）。
- **添加 / 替换 / 删除密钥**：保存后**自动热更新**（无需重启，正在进行的请求不受影响），共享同一凭据的多个 Provider（如 TJU 文本/视觉/推理）同步生效。
- **来源标识**：`environment`（系统环境变量，优先级最高）＞ `credential_store`（本机存储）＞ `dotenv`（.env 回退）＞ `missing`。来源为环境变量时窗口会提示"本窗口修改不生效"。
- **测试连接**之外的目录化路由（自定义 OpenAI Compatible 条目、路由顺序编辑）计划于后续版本。

## Credential Store（本机凭据库）

- 密钥保存在 **`%LOCALAPPDATA%\FireflyAI\credentials\credentials.json`**（数据根尊重 `FIREFLY_USER_DATA_DIR` 环境变量）。
- 该位置**在仓库树之外**：凭据永远不会进入 Git、不会写入安装目录的 config 模板、不会出现在日志与错误信息中。
- 写入为原子替换 + 尽力最小权限；文件损坏时自动隔离并按空库恢复，不会破坏既有数据。
- 与现有 `.env` 完全兼容：开发者可继续使用 `.env`/环境变量；普通用户用本窗口即可。

## 无 Key 优雅降级

未配置任何密钥时，应用**完整可用**：宠物、托盘、控制台、便签、PDF 阅读（离线 OCR）、记忆写入边界、学习模式骨架等均正常；发起对话会得到明确提示（`(all AI providers failed: tju, zhipu, deepseek)`）而非崩溃。配置任一密钥后对话能力即激活。

## Memory Boundary（记忆边界）

- 长期记忆**只接受显式指令**（"记住…"句式）；普通聊天永不自动写入（机器来源零直写，测试与冻结冒烟双重验证）。
- 建议候选（如自动抽取）一律进入"待确认"，由用户决定。
- 敏感信息红线过滤器常开：私钥/JWT/云密钥/身份证/银行卡等模式硬阻断入库。
- 记忆数据保存在本机用户目录，语义索引为**本地**嵌入（不联网上传记忆内容）。

## 安装与启动

1. 解压 zip 到**用户可写目录**（例如 `D:\Tools\Firefly_AI_Pet\`；不要解压到 `Program Files`——偏好设置需要写入安装目录）。
2. 双击 `Firefly_AI_Pet.exe`（或 `start.cmd`）。首次启动 Windows SmartScreen 可能提示未签名应用，选择"仍要运行"。
3. 配置模型密钥（三选一）：
   - 推荐：Settings → AI 模型管理 → 添加密钥（保存在本机凭据库）；
   - 或设置系统环境变量（`TJULLM_API_KEY` / `ZHIPU_API_KEY` / `DEEPSEEK_API_KEY`）；
   - 或将 `.env` 文件放到 `_internal\` 目录（模板见包根 `.env.example`）。
4. 退出：托盘图标 → 右键 → 退出。

## 已知限制（阅读后再使用）

| # | 限制 | 说明/建议 |
|---|---|---|
| 1 | **首次记忆写入需联网一次** | 语义索引首启需从 HuggingFace 下载嵌入模型（约 30MB）。受限网络：设置 `HF_ENDPOINT=https://hf-mirror.com` 后重启；或向官方索取含模型缓存的完整包。下载失败时语义检索自动降级（记忆写入与检索基线不受影响）。 |
| 2 | **应用未签名** | SmartScreen/杀软可能告警（含全局键盘钩子）。核对 SHA256 后放行；正式签名在后续版本。 |
| 3 | **便携目录需可写** | 偏好设置写入安装目录 `config/`；解压到只读位置会导致设置无法保存。 |
| 4 | `.env` 位置较深 | 冻结后项目 `.env` 位于 `_internal\.env`；推荐改用系统环境变量或凭据库。 |
| 5 | 可选外部能力默认缺席 | 摄像头视觉、视频分析（B 站）、TJU 信息检索、语音、ESP32 硬件依赖仓库外组件/设备；缺席时功能静默降级，不影响其余部分。 |
| 6 | 单实例 | 同时只允许一个 Firefly 实例（含开发版源码实例）；第二个启动进程会自动退出。 |
| 7 | 工作流（Plan→Implement→Review）为实验特性 | 依赖本机 Claude/Codex CLI；未通过生产级在线复认证，请勿用于无人值守场景。 |
| 8 | 中文输入法与全局热键 | PageLens 热键 `Ctrl+Alt+Shift+L`；若与其他软件冲突可在系统层调整。 |

## 隐私提示

对话文本、屏幕截图（仅显式触发）、文档摘录/页图、视频转录会发送给你配置的云端模型服务商（TJU/智谱/DeepSeek）。记忆内容仅保存在本机；凭据仅保存在本机。详见仓库 `docs/Release_Scope_v1.md` 出网矩阵。

## 校验

发布包附带 `Firefly_AI_Pet_v1.0-rc2_win64.zip.sha256`。校验命令：

```powershell
Get-FileHash .\Firefly_AI_Pet_v1.0-rc2_win64.zip -Algorithm SHA256
```
