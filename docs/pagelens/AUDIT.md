# PageLens Bridge — 架构审计

## 架构图

```
┌───────────────────────────────┐
│  Chrome / Edge 浏览器          │
│                               │
│  content.js (每页注入)          │
│    ├─ 监听 mouseup → 选区       │
│    ├─ 监听 scroll → 页面上下文    │
│    └─ chrome.runtime.sendMessage│
│           │                    │
│  background.js (service worker)│
│    ├─ WebSocket 客户端          │
│    ├─ 右键菜单 contextMenus     │
│    ├─ tabs.onUpdated PDF 检测    │
│    └─ keepalive (20s)          │
└───────────┬───────────────────┘
            │ WebSocket (ws://127.0.0.1:17321)
            ▼
┌───────────────────────────────┐
│  Firefly 桌面 (Python/Qt)      │
│                               │
│  core/pagelens_bridge.py       │
│    ├─ asyncio WebSocket server │
│    ├─ JSON 类型白名单 (16 入/5 出)│
│    ├─ Qt Signal 桥接            │
│    ├─ AI chat (ai_router.chat) │
│    └─ daemon thread            │
│           │ Signal             │
│           ▼                    │
│  PageLens 面板 / PdfQa 解释链路  │
└───────────────────────────────┘
```

## 数据流

```
浏览器                                桌面
  │                                     │
  ├── bridge_hello ──────────────────→ │ 连接建立
  ├── bridge_ping (20s) ─────────────→ │ keepalive
  │                                     │
  ├── selection ─────────────────────→ │ _handle_selection()
  │   {text, url, page, source}        │   → sanitise → Signal
  │                                     │
  ├── page_context ──────────────────→ │ view_state Signal
  │   {url, title, heading, text}      │
  │                                     │
  ├── pdf_opened ────────────────────→ │ _handle_pdf_opened()
  │   {url, title}                     │   → Signal
  │                                     │
  ├── ai_chat_request ───────────────→ │ _schedule_ai_chat()
  │   {term, source}                   │   → ai_router.chat() → response
  │                                     │
  ←────────────────── open_concept ──── │ 桌面动作
  ←────────────────── open_related ──── │
  ←────────────────── open_question ─── │
  ←────────────────── back ──────────── │
```

## 文件说明

### 浏览器扩展（extensions/pagelens_bridge/）

| 文件 | 行数 | 职责 |
|---|---|---|
| `manifest.json` | 16 | Chrome Manifest V3 清单：权限（contextMenus, tabs）、content script 注入规则 |
| `background.js` | 145 | Service Worker：WebSocket 客户端（自动重连/keepalive）、右键菜单、PDF tab 检测 |
| `content.js` | 124 | Content Script：选区捕获（250ms 去抖）、页面上下文（5s 节流）、heading 提取 |
| `README.md` | ~80 | 用户文档（安装/协议/隐私/已知限制） |

### 桌面端桥接（core/pagelens_bridge.py）

| 组件 | 行数范围 | 职责 |
|---|---|---|
| 常量 | L39–87 | 端口 17321、入/出消息类型白名单（16 入 / 5 出）、AI chat 来源白名单 |
| `PageLensBridge` 类 | L90–147 | Qt QObject：信号定义（14 个 Signal）、asyncio 线程/loop 管理 |
| 生命周期 | L168–213 | `start()`（daemon thread + asyncio loop）/ `stop()`（优雅关闭） |
| 出站动作 | L219–245 | `send_action/send_open_concept/send_open_related/send_open_question/send_back` |
| WebSocket 服务 | L248–314 | `_run_loop/_start_server/_run_server/_handle_client`（websockets.serve） |
| 消息分发 | L360–450 | `_handle_incoming`：JSON 解析 → 类型白名单检查 → 分发到各 `_handle_*` 方法 |
| 选区处理 | L454–490 | `_handle_selection`：清洗/截断/来源区分 → Signal |
| PDF 检测 | L494–530 | `_handle_pdf_opened/_handle_pdf_view_state` |
| AI chat | L537– | `_schedule_ai_chat`：ai_router.chat 异步调用 |

## 安全设计

| 层 | 措施 |
|---|---|
| 网络层 | 仅监听 127.0.0.1（回环），数据不出本机 |
| 协议层 | 严格 JSON + 类型白名单（16 入/5 出），未知类型静默丢弃 |
| 执行层 | 无 eval/exec/shell/file-path 操作 |
| 数据层 | 无 API 密钥、无遥测、无 cookie/浏览器数据读取 |
| 扩展层 | Manifest V3 最小权限（contextMenus + tabs），无 host_permissions |
