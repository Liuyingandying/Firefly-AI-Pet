# Firefly AI Pet

**一只住在桌面上的萤火虫 AI 伙伴 —— 让 AI 的工作看得见，让密钥与记忆留在你自己的电脑里。**

![Python](https://img.shields.io/badge/Python-3.10%20--%203.13-3776AB?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/Windows-10%2F11%20x64-0078D6?logo=windows&logoColor=white)
[![Release](https://img.shields.io/badge/Release-v1.0--rc2-0064D6)](../../releases)
![License](https://img.shields.io/badge/License-MIT-green.svg)

> 天津大学 AI Agent 创新赛参赛项目 · agent2026-firefly-agent

技术赛道材料：[技术设计](DESIGN.md) · [演示视频](https://www.bilibili.com/video/BV1Bnet6wEWC) · [演示截图](screenshots/) · [版权合规承诺书](版权合规承诺书.docx)。`plugins/` 是当前宿主纳管的插件源码；`optional_services/` 收录可选的语音与 B站服务源码。演示截图来自既有材料，本轮尚未完成独立新目录 GUI 验收。

---

## 项目简介

Firefly AI Pet 是一个运行在 Windows 桌面上的**个人 AI 伙伴**：一只常驻右下角的流萤宠物，承载对话、学习、文档分析与 Agent 协作能力。

它解决的问题是——**把散落在网页、文档、终端里的 AI 能力，收敛成一个看得见、管得住、记得住的桌面入口**：

- **看得见**：Agent 的思考/执行/等待状态，实时映射为流萤的动画与托盘状态；
- **管得住**：模型密钥由用户在本机图形界面中添加、替换、删除，保存即热更新，无需重启；
- **记得住**：长期记忆只接受用户显式指令写入，敏感信息红线过滤，全部数据保存在本机。

项目采用 Python + PySide6 构建，提供免安装便携包，解压即用。

## 核心能力

### 🐞 AI 智能伙伴

- 桌面常驻宠物 + 系统托盘 + AI 控制台三合一交互
- 七态动画（idle/thinking/working/waiting/success/error/sleeping）映射真实 Agent 生命周期
- 快捷提问气泡、对话历史多会话管理、记忆/文档上下文自动装配

### ⚙️ 多模型 Provider 管理

- **AI 模型管理窗口**：卡片式添加 / 替换 / 删除 API Key，保存即热更新（无需重启）
- 内置 TJU LLM、智谱 GLM、DeepSeek 三家 OpenAI 兼容 Provider，按 `TJU → 智谱 → DeepSeek` 自动回退，60 秒总预算
- 凭据保存在本机凭据库（用户数据目录，仓库之外），支持来源标识：`environment` > `credential_store` > `dotenv` > `missing`
- 未配置任何密钥时优雅降级：界面与全部本地功能照常可用

### 🔒 Memory Boundary（记忆边界）

- 长期记忆**只接受显式指令**（"记住…"句式）写入；普通聊天零写入
- 机器来源（自动抽取）只能进入"待确认"建议队列，由用户决定是否采纳
- 敏感信息红线过滤器常开：私钥 / JWT / 云密钥 / 身份证 / 银行卡等模式硬阻断入库
- 语义索引完全本地化（Qdrant 本地模式 + fastembed 本地嵌入），记忆内容不出本机

### 📚 学习模式

- 确定性规则内核裁定掌握度（LLM 只负责"讲课"，不做裁判）
- 课程 / 概念 / 复习计划 / 学习会话全流程追踪（SQLite 本地存储）
- 与 PageLens 文档阅读联动：从教材到出题到复习闭环

### 📄 文档分析

- 支持 PDF / DOCX / PPTX / TXT / MD / XLSX / CSV 附件问答
- PDF 原生文本层优先，扫描件自动回退 RapidOCR 离线识别
- PaperLens 阅读模式：页面渲染 + 概念卡 + 选中即解释
- 文档路由器自动分发：文本问答 / 页面定位 / OCR / 摘要 / 视觉理解

### 🧩 Agent 扩展

- Quick Tools 插件体系：摄像头视觉、视频分析、TJU 信息检索、学习增强（插件体独立分发）
- Claude Code / Codex CLI 生命周期监控与工作台集成
- 白名单加载 + AST 只读探测，插件故障相互隔离

## 系统架构

```mermaid
flowchart TB
    subgraph Desktop["桌面体验层（PySide6）"]
        Pet["流萤宠物<br/>动画 + 托盘"]
        Console["AI 控制台<br/>多会话对话"]
        PM["AI 模型管理<br/>密钥增删改"]
        Panel["记忆 / 便签 / 设置"]
    end

    subgraph Core["核心运行时"]
        CR["Companion Runtime<br/>上下文装配 + 会话"]
        Router["Provider Router<br/>TJU → 智谱 → DeepSeek<br/>60s 预算自动回退"]
        Mem["Memory System<br/>显式写入 + 红线过滤<br/>本地语义索引"]
        Learn["Learning System<br/>规则内核 + SQLite"]
        Doc["文档分析<br/>PyMuPDF + RapidOCR"]
    end

    subgraph Providers["模型服务（OpenAI 兼容）"]
        TJU["TJU LLM"]
        Zhipu["智谱 GLM"]
        DS["DeepSeek"]
    end

    subgraph Local["本机存储"]
        Store[("凭据库")]
        Data[("用户数据<br/>记忆/会话/索引")]
    end

    Pet -->|交互| Console
    Console --> CR
    PM -->|保存密钥| Store
    PM -->|热更新| Router
    CR --> Mem
    CR --> Learn
    CR --> Doc
    CR --> Router
    Router --> TJU & Zhipu & DS
    Mem --> Data
    CR -.读取密钥.-> Store
```

## 技术特点

- **本地优先**：记忆、会话、便签、语义索引全部落在本机用户目录；凭据库在仓库之外，永不进入版本控制
- **优雅降级**：无密钥、缺插件、离线、缺可选组件——每一条依赖缺失路径都有明确的降级行为，绝不崩溃
- **零 SDK 依赖的 Provider 直连**：stdlib `urllib` 直连 OpenAI 兼容接口，固定 60 秒预算、逐跳回退、健康状态持久化
- **确定性记忆边界**：规则引擎（而非 LLM）决定"什么能被记住"，红线过滤器不可关闭
- **原子写与故障隔离**：配置、记忆、会话全部原子替换写入；插件/桥/文档解析器故障相互隔离
- **工程化交付**：PyInstaller onedir 便携打包、GitHub Actions 三版本矩阵 CI（稳定核心测试集）、SHA256 校验发布
- **测试规模**：项目包含约 2600 项本地自动化测试；针对硬件、视觉等环境依赖模块，CI 采用稳定核心测试集持续验证；rc2 发布前完成 166 项关键回归验证

## Demo 展示

**视频演示｜[第二代流萤桌宠系统：聊天、记忆](https://www.bilibili.com/video/BV1Bnet6wEWC)**。这是参赛团队提供的 B站视频链接，聚焦聊天与记忆交互；视频托管于站外，播放取决于 B站可访问性。本轮未独立核验视频内容。

这些是项目已有的界面截图，用于展示交互入口；它们不代表本轮已完成新目录运行或外部服务验收。模型、摄像头和校园检索的使用条件见 [技术设计](DESIGN.md)。

| 桌面伙伴与对话 | 模型设置 |
|:---:|:---:|
| ![桌宠与对话控制台](screenshots/01-main-interface.png) | ![AI 模型管理窗口](screenshots/08-model-settings.png) |
| 从桌宠、托盘和控制台进入对话。 | 在本机配置可用 Provider；截图不展示密钥明文。 |

| 视频阅读入口 | 屏幕视觉入口 |
|:---:|:---:|
| ![视频阅读界面](screenshots/02-video-reading.png) | ![屏幕视觉界面](screenshots/03-screen-vision.png) |
| 本地视频和 B站服务有各自的安装前提。 | 画面处理需要用户触发及可用视觉 Provider。 |

[查看全部 12 张既有演示截图](screenshots/)。

## 快速开始

### 方式一：便携包（推荐普通用户）

1. 从 Release 页下载 `Firefly_AI_Pet_v1.0-rc2_win64.zip`，解压到**用户可写目录**
2. 双击 `Firefly_AI_Pet.exe`（首次启动如遇 SmartScreen 提示，选择"仍要运行"）
3. 托盘图标出现即启动成功；配置密钥见下方"模型密钥配置"

### 方式二：源码运行（开发者）

```powershell
git clone https://github.com/Liuyingandying/Firefly-AI-Pet.git
cd Firefly-AI-Pet
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

要求：Windows 10/11 x64，Python 3.10 – 3.13。

### 模型密钥配置（三选一）

| 方式 | 说明 |
|---|---|
| **AI 模型管理窗口**（推荐） | 设置 → AI 模型管理 → 添加密钥，保存即热更新 |
| 系统环境变量 | `TJULLM_API_KEY` / `ZHIPU_API_KEY` / `DEEPSEEK_API_KEY` |
| `.env` 文件 | 放置于 `_internal\.env`（参考包根 `.env.example`） |

### 首次使用提示

- 首次记忆写入需联网下载本地嵌入模型（约 30MB）；受限网络可设置 `HF_ENDPOINT=https://hf-mirror.com` 后重启
- 退出请使用托盘图标右键 → 退出

## 新用户安装验证

`v1.0-rc2` 便携包经过一次"新用户视角"端到端实测：从 GitHub 下载 → SHA256 校验 → 解压 → 无 Key 首次启动 → 数据隔离核对 → 可选插件安装。

| 验证项 | 结果 |
|---|---|
| 下载完整性（SHA256 与 `.sha256` asset 三方一致） | ✅ |
| 解压结构与启动入口 | ✅ |
| 无 Key 首启（无凭据写入、进程稳定、宠物正常渲染） | ✅ |
| 用户数据目录自动创建（conversation/learning/memory/plugins/runtime 等） | ✅ |
| 用户数据与既有数据零交叉写入（逐文件快照比对） | ✅ |
| 用户数据不进入 Git 仓库与发行包 | ✅ |
| 插件默认安装路径（`%LOCALAPPDATA%\FireflyAI\plugins`，宿主自动创建） | ✅ |

完整方法、证据与未验证项见 [docs/New_User_Migration_Verification.md](docs/New_User_Migration_Verification.md)。

## 项目结构

从入口到可选能力，代码分为四层：

| 层次 | 主要位置 | 评委可以看到什么 |
|---|---|---|
| 交互入口 | `app.py`、`ui/`、`character/` | 桌宠、托盘、对话、角色与状态界面 |
| 核心运行 | `core/`、`memory/`、`providers/` | 会话与上下文、任务分流、记忆边界、模型路由 |
| 可选能力 | `plugins/`、`extensions/pagelens_bridge/`、`voice_client/` | 九个受管插件入口、浏览器桥接与语音客户端 |
| 独立服务 | `optional_services/` | B站 JSONL 服务、语音 HTTP 服务的补充源码；部署与权重另配 |
| 交付与证据 | `packaging/`、`tests/`、`docs/`、`screenshots/` | 构建、测试、设计记录与演示画面 |

详细模块关系和调用链见 [DESIGN.md](DESIGN.md)。

## Roadmap

**已完成（v1.0-rc2）**

- ✅ AI 模型管理窗口与凭据库（添加/替换/删除，保存即热更新）
- ✅ 无密钥优雅降级与三模型自动回退
- ✅ Memory 边界与显式写入守护（冻结冒烟验证）
- ✅ Windows 便携包与三版本矩阵 CI
- ✅ PDF 离线 OCR 与文档附件分析

**计划中（v1.0-rc3+）**

- 🔜 预置本地嵌入模型缓存，实现完全离线首启
- 🔜 自定义 OpenAI 兼容 Provider（用户可添加任意端点）
- 🔥 发布包代码签名
- 🔥 记忆检索下载超时与后台降级
- ⏳ 跨平台（Linux/macOS）评估
- ⏳ 学习模式课程库扩充

## Plugin Ecosystem

Firefly AI Pet supports modular capability extensions.

Plugin repository:

https://github.com/Liuyingandying/Firefly-AI-Pet-Plugins


Included:

- Video Extension
- Vision Extension
- Learning Assistant
- Campus Information Bridge
- Voice Capability

**In-repo extensions（`plugins-integration` 分支）**：全部 5 个插件经 git subtree 合并至
[`extensions/`](extensions/)，与 PageLens 浏览器扩展（`extensions/pagelens_bridge`）同目录共存；
架构、契约、装载机制与比赛展示说明见 [docs/plugin_ecosystem.md](docs/plugin_ecosystem.md)。

## License

本项目基于 [MIT License](LICENSE) 开源。

Copyright © 2026 Fuyong
