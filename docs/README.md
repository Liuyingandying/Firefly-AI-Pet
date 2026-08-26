# Firefly AI Pet Documentation

本目录是 Firefly AI Pet 的工程文档导航入口，也是未来开发者、项目贡献者与竞赛评审阅读项目的第一份技术地图。

## 1. Project Overview

### Firefly AI Pet 是什么

Firefly AI Pet 的定位是一个**具身化个人 AI Agent 操作环境（Embodied Personal AI Agent Environment）**。

它不是传统桌宠：

- 不只是聊天工具
- 不只是角色陪伴

它将以下能力整合为统一的桌面环境：

```text
Agent Runtime
      +
Tool Integration
      +
Memory System
      +
Embodied Companion
```

Firefly 通过角色化桌面界面承载 Agent 的运行状态、工具能力、上下文与长期记忆，使个人 AI Agent 具备持续可见、可感知、可交互的具身入口。

### 当前版本

**v0.1.0-firefly-demo**

### 核心目标

- 构建个人 AI Agent 桌面入口
- 统一 Claude / Codex / LLM 调度
- 提供长期记忆能力
- 通过角色化 UI 提供具身交互体验

---

## 2. System Architecture

### [`architecture/`](architecture/)

总体架构设计，包括：

- Firefly AI Pet 总体架构
- Agent Router
- Desktop Companion
- Browser Sensor
- Memory System

### [`engineering/`](engineering/)

工程实现文档，包括：

- 工程结构
- 部署方式
- 测试方案
- 性能分析
- 调试记录

### [`design/`](design/)

产品设计文档，包括：

- UI 设计
- 角色系统
- 交互设计
- 视觉规范

---

## 3. Core Modules

### Desktop Companion

桌面 AI 伴侣核心，负责：

- 桌面窗口
- 状态展示
- 用户交互
- AI 能力入口

### Agent Router

Claude / Codex / LLM 统一调度层，负责：

- 多 Agent 管理
- Provider 路由
- 状态同步
- 权限控制

### PageLens

浏览器智能感知层，负责：

- 页面内容感知
- 关键词提取
- Concept Card
- 浏览器与桌面端桥接

### Memory System

长期记忆架构，目标是实现：

- 用户偏好保存
- 对话历史管理
- Agent 上下文连续性

---

## 4. Development History

### Phase 8 — UI Architecture

记录：

- Desktop Companion 视觉系统
- Workspace
- Pet Shell
- UI 组件设计

相关文档：[PHASE8_UI_ARCHITECTURE.md](PHASE8_UI_ARCHITECTURE.md)

### Phase 9 — Agent Router Architecture

记录：

- 多 Agent 调度
- Claude / Codex 接入
- Provider Router
- 状态管理

相关文档：[PHASE9_AGENT_ROUTER_ARCHITECTURE.md](PHASE9_AGENT_ROUTER_ARCHITECTURE.md)

---

## 5. Competitor Analysis

竞品分析统一存放在 [`competitor_analysis/`](../competitor_analysis/) 目录。

### [N.E.K.O](../competitor_analysis/NEKO/)

分析内容：

- Electron 架构
- 插件系统
- Live2D / VRM 支持
- AI 角色系统

### Other AI Companion Projects

用于记录：

- AI 桌宠
- AI Companion
- Agent 平台

竞品分析用于指导 Firefly 的架构决策，识别可借鉴的工程方案、产品模式与潜在技术风险。

---

## 6. Documentation Reading Guide

第一次阅读本项目时，推荐按以下顺序：

1. [`docs/architecture/`](architecture/)
   了解整体设计思想。

2. [`docs/PHASE9_AGENT_ROUTER_ARCHITECTURE.md`](PHASE9_AGENT_ROUTER_ARCHITECTURE.md)
   了解 Agent 核心调度。

3. [`docs/screenshots/`](screenshots/)
   了解当前产品形态。

4. [`README.md`](../README.md)
   了解项目运行方式。

---

## 7. Future Roadmap

后续工程文档将在本节持续补充，重点方向包括：

- Memory System 完善
- Agent 生态扩展
- 更多模型接入
- 角色系统增强
- 插件体系
