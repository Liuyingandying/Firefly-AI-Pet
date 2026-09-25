# PageLens Bridge — 安装与使用指南

## 前置条件

| 项目 | 要求 |
|---|---|
| 浏览器 | Chrome 110+ 或 Edge 110+（需支持 Manifest V3） |
| Firefly AI Pet | v1.0+ 已运行（桌面桥自动启动） |
| 操作系统 | Windows 10/11 |

## 安装步骤

### 1. 启动 Firefly AI Pet

双击 Firefly 可执行文件或运行 `python app.py`。
PageLens Bridge 会在后台自动启动（监听 `127.0.0.1:17321`）。

### 2. 安装浏览器扩展

1. 打开浏览器，地址栏输入：
   - Edge：`edge://extensions`
   - Chrome：`chrome://extensions`
2. 开启右上角「开发人员模式」开关。
3. 点击「加载解压缩的扩展」。
4. 选择 Firefly 安装目录下的 `extensions/pagelens_bridge` 文件夹。
5. 扩展卡片出现「Firefly PageLens Bridge」即安装成功。

### 3. 确认连接

1. 在 Firefly 日志中查找：`[PageLens Bridge] starting on 127.0.0.1:17321`
2. 扩展的 Service Worker 控制台（`edge://extensions` → 详细信息 → Service Worker）
   应显示 WebSocket 已连接。
3. 在 Firefly 聊天或 PageLens 面板中确认「已连接」状态。

## PDF 使用流程

```
启动 Firefly
    ↓
安装浏览器扩展
    ↓
打开 PDF（arxiv / 本地 file:/// / 任何 URL 以 .pdf 结尾）
    ↓
扩展检测 → 发送 pdf_opened → Firefly 弹出论文上下文面板
    ↓
选中文字 → 发送 selection → 桌面端更新上下文
    ↓
右键 →「用 Firefly 解释选区」→ 解释面板弹出
```

## HTML 论文使用流程（arxiv / Google Scholar 等）

```
打开 HTML 论文页面
    ↓
扩展自动注入 → 发送 page_context（标题+可见文本）
    ↓
选中一段文字 → 松开鼠标 250ms 后自动发送
    ↓
桌面端 PageLens 面板更新选中上下文
```

## 常见错误

| 现象 | 原因 | 解决 |
|---|---|---|
| 扩展显示「连接失败」 | Firefly 未运行 | 启动 Firefly AI Pet |
| 选中文字无反应 | Firefly 未运行或扩展未加载 | 检查两者状态 |
| PDF 打开后无面板 | 内置 PDF Viewer 不支持 content script | 使用右键菜单「用 Firefly 解释选区」兜底 |
| 页码显示 -1 | 页码无法从内置 Viewer 读取 | 正常行为（HTML 页面可扩展获取） |
| 反复断开重连 | Firefly 端口被占用 | 检查 17321 端口是否被其它进程使用 |
