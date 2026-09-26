# PageLens Bridge — Release Check

> 检查日期：2026-09-24 · Commit：`5db1704`

## 当前版本

| 项 | 值 |
|---|---|
| 扩展版本 | 0.2.0 |
| Manifest | V3 |
| 浏览器 | Chrome 110+ / Edge 110+ |
| 依赖 | 零外部（原生 JS） |

## 已验证功能

| 功能 | 验证方式 | 结果 |
|---|---|---|
| 文本选区捕获（HTML） | content.js mouseup → 250ms 去抖 → selection 消息 | ✅ |
| PDF URL 检测 | background.js tabs.onUpdated → pdf_opened 消息 | ✅ |
| 右键解释菜单 | contextMenus.create + onClicked → user_action 消息 | ✅ |
| 自动重连 | 指数退避 1s→5s + 20s keepalive | ✅ |
| LaTeX→Unicode | chat_markup 40+ 映射（$\nabla$→∇ 等） | ✅ 14 测试 |
| 安全 | 仅回环/JSON 白名单/零 API key | ✅ 扫描 clean |

## 已确认不受影响

| 功能 | 验证 |
|---|---|
| 正常 markdown（bold/bullet/heading/code） | 14 项 chat_latex + 既有 chat 测试 |
| 价格符号（$5 美元） | test_chat_latex 明确断言 |
| 主仓库 Learning Mode | 374/374 全链零回归 |

## 未解决问题

| 问题 | 严重性 | 说明 |
|---|---|---|
| 图标缺失 | 低 | manifest 无 icons 字段，浏览器显示默认拼图；不影响功能 |
| 内置 PDF Viewer 选区不可达 | 平台限制 | 右键菜单兜底已实现，真机效果待验证 |
| LaTeX 复杂排版（矩阵/积分号） | 低 | 仅做 Unicode 替换，复杂公式保持原样可读 |
| `page: -1` | 已知 | 内置 Viewer 无法读取页码，桌面侧已适配 |

## 后续计划

| 优先级 | 内容 |
|---|---|
| P1 | 补充 16/48/128px PNG 图标（消除默认拼图图标） |
| P1 | 真机验证：内置 PDF Viewer 右键菜单兜底路径 |
| P2 | HTML 论文（arxiv/PDF.js）页码提取扩展 |
| P2 | desktop `list_review_items()` → Profile review_due 联动 |

## 结论

✅ **PageLens Bridge 可以进入 GitHub Release。**

- 工作区干净（零未提交修改）
- 功能可用（6 项验证通过）
- 安全合规（零敏感数据）
- 文档齐备（README + AUDIT + PROTOCOL + INSTALL）
