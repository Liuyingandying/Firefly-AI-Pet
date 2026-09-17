# Firefly PageLens Bridge 扩展（最小实验件）

把浏览器中的文本选区（含论文 PDF 页面）通过 WebSocket 发送到
Firefly 桌面桥（`ws://127.0.0.1:17321`）。桌面侧收到 `selection` 消息后
弹出解释入口并走既有 `PdfQa.explain_selection()` 链路。

## 加载方式（Edge 或 Chrome）

1. 打开 `edge://extensions`（Chrome 用 `chrome://extensions`）。
2. 打开「开发人员模式」。
3. 「加载解压缩的扩展」→ 选择本目录 `extensions/pagelens_bridge`。
4. 确保 Firefly 已运行（桥监听 127.0.0.1:17321）。

## 行为

- **普通网页 / HTML 论文（arxiv、Google Scholar 等）**：content script 在
  鼠标拖选结束时读取 `window.getSelection()`，发送
  `{type:"selection", payload:{text, url, page:-1}}`。
- **内置 PDF Viewer（Edge/Chrome 原生阅读器）**：content script 无法注入
  viewer 扩展页（平台限制，见下表）；扩展另注册了「用 Firefly 解释选区」
  右键菜单（`contextMenus` selection 上下文）作为尽力而为的兜底——若平台
  允许在 viewer 选区上显示扩展右键菜单，`info.selectionText` 即可送达桌面；
  否则该路径不可用。
- 撤销选中后 250ms 静默期再发送，避免拖拽抖动重复上报；单条文本上限
  2000 字符（与 `PdfQa.explain_selection` 一致）。

## 已知限制（真实浏览器侧，需现场验证）

| 问题 | 状态 |
|---|---|
| 内置 PDF Viewer 的 DOM 选区对第三方 content script 不可达 | 已知平台约束；右键菜单兜底待真机验证 |
| 页码无法从内置 Viewer 读取 | 发送 `page:-1`；桌面侧带页码解释需要扩展可读页号（HTML/PDF.js 站点可扩展获得） |
| `bridge_hello`/keepalive 心跳 | 已实现，与 `core/pagelens_bridge.py` 协议一致 |

## 协议（与 `core/pagelens_bridge.py` 保持一致）

```json
// 连接即发
{ "type": "bridge_hello", "payload": {} }
// 选区上报（桌面侧日志：收到 selection event: text=... page=... url=...）
{ "type": "selection",
  "payload": { "text": "Scaled Dot-Product Attention",
               "url": "file:///.../attention.pdf",
               "page": 3 } }
```