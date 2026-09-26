# Plugin Ecosystem

Firefly AI Pet 采用**扩展化架构**：核心运行时保持稳定，能力通过两类扩展接入。

## Overview

| 类型 | 定位 | 形态 |
|---|---|---|
| **Agent Capability Plugins** | 为 Agent 增加可调用能力（视觉、视频分析、学习专注、校园信息检索、语音） | Python 包，符合 FireflyExtension v2 契约，宿主运行时装载 |
| **Browser Intelligence Extensions** | 为浏览器侧提供上下文智能（网页概念发现 → AI 解释 → 追问探索） | 浏览器扩展（PageLens），与宿主经桥接通信 |

两类扩展共享同一设计原则：**能力可插拔、故障可隔离、核心不可撼动**——任何扩展缺失或故障，Core 全功能不受影响。

## Extension Layout

本仓库 `extensions/` 目录（`plugins-integration` 分支起）同时承载两类扩展：

```
extensions/
├── firefly_camera_vision/     # Agent Capability：摄像头视觉适配器（按需单帧，永不后台开启）
├── firefly_video_extension/   # Agent Capability：本地视频分析（时长/场景/关键帧/字幕）
├── learning_focus/            # Agent Capability：学习专注（知识图谱×画像记忆×作答证据）
├── tju_info_retrieval/        # Agent Capability：校园信息检索（接口契约分发）
├── firefly_voice/             # Agent Capability：语音能力管理（TTS+RVC 服务状态/开关/显式启停）
├── pagelens_bridge/           # Browser Intelligence：PageLens 浏览器扩展桥
├── INTERFACE.md               # 桥接类插件通用契约说明
├── README.md / LICENSE / docs/# 插件仓根文件（subtree 同步）
```

独立插件仓：https://github.com/Liuyingandying/Firefly-AI-Pet-Plugins
（经 git subtree 合并进入 `extensions/`；插件仓继续独立维护并保留 v1.0.0 Release）

## Plugin Contract

全部 Agent Capability Plugins 通过**统一契约**接入（FireflyExtension v2），契约全文见
[`extensions/tju_info_retrieval/INTERFACE.md`](../extensions/tju_info_retrieval/INTERFACE.md)
与各插件目录内文档。要点：

- Python 包（`__init__.py`）+ `plugin.py::create_plugin(parent=None)` 工厂
- manifest 内嵌于 `QuickToolManifest`（无需 plugin.json）
- 宿主 `core/plugin_loader.py` 启动时自动发现：**白名单管理 + AST 只读探测**（未管理插件在导入前即被忽略）
- 插件故障相互隔离，永不传染宿主

## Runtime Loading

**源码目录与本机运行时目录是两个位置**：

| 位置 | 用途 | 谁写入 |
|---|---|---|
| 仓库 `extensions/`（本目录） | 开发、评审、版本管理（subtree 同步自插件仓） | 开发者 |
| `%LOCALAPPDATA%\FireflyAI\plugins` | **运行时装载根**——宿主启动时仅从这里发现插件 | 用户安装（复制插件目录至此） |

也就是说：仓库内的 `extensions/firefly_camera_vision/` 等目录**不会被宿主自动加载**；
要在本机启用插件，请将插件目录复制到运行时装载根（可用 `FIREFLY_PLUGIN_ROOT`
环境变量或 `config/path_config.yaml` 的 `paths.plugin_root` 重定向）。详见
`extensions/README.md` 的"安装流程（普通用户）"。

## TJU AI Agent Competition

本扩展生态是天津大学 AI 智能体大赛作品的核心展示面之一：

- **多 Agent**：宿主 Agent Runtime 裁决多 Agent 源（Claude / Codex / 手动）并映射七态生命周期
- **工具调用**：Quick Tools 面板即 Agent 的工具面——四个能力插件即四类可调用工具，白名单 + AST 探测保证调用安全
- **信息检索**：`tju_info_retrieval` 契约接入校园学术检索代理（独立仓 `tju-info-retrieval-agent`，多源 CNKI/IEEE/万方）
- **Memory**：显式写入边界 + 红线过滤 + 本地语义索引，插件与记忆互不越界
- **Browser Intelligence**：PageLens 浏览器扩展提供网页上下文，与文档分析管线共同构成 Agent 的"眼睛"
- **Voice Capability**：`firefly_voice` 插件管理 TTS+RVC 语音链路（服务状态 / 开关 / 显式启停 / Voice Settings 面板）
  服务端完整源码已随插件仓发布（`firefly_voice/voice_module/`，edge-tts → RVC → 声卡，
  模型权重因许可证外置）——宿主 `voice_client/` 与服务端经 `127.0.0.1:8300` HTTP 解耦
