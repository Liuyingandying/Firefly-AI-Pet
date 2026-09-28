# Firefly PageLens Bridge

Chrome / Edge 浏览器扩展——将浏览器中的文本选区与打开的 PDF 事件通过
WebSocket 实时发送到 [Firefly AI Pet](https://github.com/Liuyingandying/Firefly-AI-Pet)
桌面端，实现"选中文本 → AI 解释"和"打开论文 → 自动感知"的阅读辅助闭环。

## 功能

| 功能 | 说明 |
|---|---|
| 文本选区捕获 | HTML 页面拖选文本（250 ms 静默去抖），自动发送到 Firefly 桌面端 |
| PDF 打开感知 | 检测任意标签页打开的 PDF（URL + 标题），桌面端弹出论文上下文面板 |
| 右键解释 | 选中文字后右键 →「用 Firefly 解释选区」→ 桌面端弹出解释入口 |
| 页面上下文 | 滚动/选择时自动上报当前标题 + 可见文本切片（节流 5 s，上限 1500 字符） |
| 自动重连 | WebSocket 断开后指数退避重连（1 s → 5 s），附带 20 s keepalive |

## 安装（Edge 或 Chrome）

1. 打开 `edge://extensions`（Chrome 用 `chrome://extensions`）。
2. 开启「开发人员模式」。
3. 点击「加载解压缩的扩展」→ 选择本目录 `extensions/pagelens_bridge`。
4. 确保 Firefly AI Pet 已运行（桌面桥监听 `127.0.0.1:17321`）。

## 与 Firefly AI Pet 的关系

本扩展是 **客户端**（浏览器侧），Firefly 桌面端运行 **服务端**
（`core/pagelens_bridge.py`，asyncio WebSocket server，仅监听 127.0.0.1:17321）。

```
浏览器扩展 (content.js + background.js)
    │  WebSocket (ws://127.0.0.1:17321)
    ▼
Firefly 桌面 (core/pagelens_bridge.py)
    │  Qt Signal
    ▼
Firefly AI Pet (PageLens 面板 / PdfQa 解释链路)
```

两者必须同时运行才能工作。扩展单独加载后如果 Firefly 未运行，会自动重连等待。

## 协议

JSON 消息，类型白名单（与桌面端 `core/pagelens_bridge.py` 保持一致）：

| 消息类型 | 方向 | 说明 |
|---|---|---|
| `bridge_hello` | 扩展 → 桌面 | WebSocket 连接建立后即发 |
| `bridge_ping` | 双向 | keepalive（20 s 间隔） |
| `selection` | 扩展 → 桌面 | 用户选中文本（text / url / page / source） |
| `pdf_opened` | 扩展 → 桌面 | 用户打开了 PDF 文件（url / title） |
| `page_context` | 扩展 → 桌面 | 当前页面标题 + 可见文本切片（heading / text） |

`selection.payload.source` 字段区分来源：
- `"ambient"`：content script 检测到鼠标拖选（桌面仅更新上下文）
- `"user_action"`：右键菜单显式请求（桌面弹出解释面板）

## 隐私与安全

- 仅监听 / 连接 `127.0.0.1`（回环地址），数据不出本机
- 严格 JSON 协议 + 类型白名单，无 eval/exec/shell
- 不注入内置 PDF Viewer（浏览器安全限制）；右键菜单作为兜底
- 无 API 密钥、无遥测、无外部请求

## 已知限制

| 限制 | 原因 |
|---|---|
| 内置 PDF Viewer 的 DOM 选区不可达 | 浏览器平台安全约束（viewer 是扩展页） |
| 页码无法从内置 Viewer 读取 | 同上；发送 `page: -1` |
| HTML 论文（arxiv 等）的选区可用 | content script 正常注入 |

## 开源注意事项

- 本扩展属于 [Firefly AI Pet](https://github.com/Liuyingandying/Firefly-AI-Pet)
  的 `extensions/` 子目录，随主仓库一起开源（MIT License）
- 无独立仓库名称——与桌面端 `core/pagelens_bridge.py` 构成客户端-服务端对，
  独立发布会导致桌面侧无人消费
- 无敏感配置需删除（无 API key / token / 硬编码路径）
- 如需独立分发，建议将 `core/pagelens_bridge.py` 一同打包或提供独立桌面桥
