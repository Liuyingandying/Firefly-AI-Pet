# PageLens Bridge — WebSocket 协议文档

> 地址：`ws://127.0.0.1:17321` · 仅回环 · JSON 消息 · 类型白名单

## 消息通用格式

所有消息均为 JSON 对象，包含 `type` 字段区分类型：

```json
{ "type": "<消息类型>", "payload": { ... } }
```

未在白名单中的 `type` 会被桌面端静默丢弃（日志警告，不断连）。

## 浏览器 → 桌面（入站 16 种）

| type | payload 字段 | 说明 |
|---|---|---|
| `bridge_hello` | `{}` | WebSocket 连接建立后即发，桌面重置去重缓存 |
| `bridge_ping` | `{}` | keepalive 心跳（建议 20 s 间隔） |
| `selection` | `text`, `url`, `page`, `source` | 用户选中文本；`source`: `"user_action"`(右键) 或 `"ambient"`(拖选)；`page`: 1 基页码（未知=-1）；text 上限 2000 字符 |
| `pdf_opened` | `url`, `title` | 用户打开了一个 PDF 文件（tabs API 检测） |
| `pdf_view_state` | 自定义 | PDF 查看状态（预留接口，内置 Viewer 不可达） |
| `page_context` | `url`, `title`, `heading`, `text` | 页面标题 + 可见文本切片（heading ≤120 字符, text ≤1500 字符, 5 s 节流） |
| `concepts` | 自定义 | 概念列表（桌面更新"本页概念"区域） |
| `concept_loading` | 自定义 | 概念加载中 |
| `concept_card` | 自定义 | 概念卡片完整数据 |
| `concept_error` | 自定义 | 概念加载错误 |
| `question_loading` | 自定义 | 出题加载中 |
| `question_delta` | 自定义 | SSE 增量 chunk |
| `question_done` | 自定义 | 出题流完成 |
| `question_error` | 自定义 | 出题错误 |
| `view_state` | 自定义 | 浏览器视图状态镜像 |
| `pagelens-desktop-sync-request` | `{}` | 浏览器请求桌面重放状态 |
| `ai_chat_request` | 自定义 | 浏览器 Explain UI 发起的 AI 请求 |

## 桌面 → 浏览器（出站 5 种）

| type | payload | 说明 |
|---|---|---|
| `open_concept` | `{"term": "…"}` | 在浏览器中打开概念 |
| `open_related` | `{"term": "…"}` | 打开相关概念 |
| `open_question` | `{"question": "…"}` | 打开问题面板 |
| `back` | `{}` | 后退 |

## 错误处理

| 场景 | 行为 |
|---|---|
| 未知 `type` | 桌面日志警告，消息静默丢弃，连接不断 |
| JSON 解析失败 | 桌面日志异常，消息丢弃 |
| WebSocket 断开 | 扩展侧 1–5 s 指数退避自动重连；桌面清理 client 引用 |
| 桌面未运行 | 扩展侧连接失败 → 指数退避重连（不影响浏览器正常使用） |
| AI chat 异常 | `question_error` 消息回传浏览器 |

## 安全

| 层 | 措施 |
|---|---|
| 网络 | 仅监听 `127.0.0.1:17321`（回环） |
| 协议 | 严格 JSON + 类型白名单（16 入/5 出） |
| 执行 | 无 eval/exec/shell/file-path 操作 |
| 数据 | 无 API 密钥、无遥测、无外部请求 |
