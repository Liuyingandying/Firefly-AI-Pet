# TJU Info Retrieval — Firefly 插件接口契约

> 本插件为**薄适配器**：Firefly 不直接实现检索，而是通过一次性子进程桥接
> 一个独立的桌面检索工程（Source of Truth）。该核心工程为私有项目，
> 不随本开源包分发；本文档公开其宿主侧的完整接入契约，供复现同构桥接。

## 架构

```
Firefly Agent
   ↓
Plugin Adapter（宿主进程内，本插件）
   ↓  每个请求 = 一个全新的隔离子进程（shell=False / timeout / UTF-8 / cwd=工程根）
Bridge CLI（python -m tju_info_retrieval.bridge <command>）
   ↓
TJU_Info_Retrieval 独立工程（检索 / 索引 / 登录态 / GUI 的唯一实现方）
```

适配器自身零检索逻辑——不重建索引、不做排序、不持有存储。所有检索能力
（CNKI 检索源、受控浏览器登录态）都归属外部工程。

## 能力面（QuickToolManifest / FireflyExtension v2）

| 入口 | 签名 | 说明 |
|---|---|---|
| `search` | `search(query: str, top_k: int = 5) -> list[RetrievalResult]` | 走 bridge `search`，登录过期时抛 `TjuAuthRequiredError`（调用方渲染重新登录提示）。调用前由 runner 检查能力门控 |
| `open_login` | `open_login() -> str` | 打开受控 Edge 进行**手动**登录；永不触碰用户凭据 |
| `open` / `open_ui` | `open_ui() -> str` | 启动外部工程独立 GUI（`python main.py`），独立于 On/Off 门控 |
| `status` | `status() -> str` | 纯本地状态查询，**零副作用**（见下） |

## 状态机（四态分明）

| 状态 | 含义 |
|---|---|
| `READY` | 插件可用 且 TJU 登录态新鲜 |
| `AUTH_REQUIRED` | 插件可用，但登录态缺失/过期（>30 天快照即视为过期，真实校验发生在实际使用时） |
| `OFFLINE` | Quick Tools 开关关闭（插件停止） |
| `ERROR` | 外部工程缺失 / 运行环境损坏 |

## Bridge CLI 协议

每个请求由适配器 spawn 一个全新子进程（隔离、无长驻进程）：

```bash
python -m tju_info_retrieval.bridge health          # 健康检查，超时 20s
python -m tju_info_retrieval.bridge auth_status     # 登录态探测，超时 40s
python -m tju_info_retrieval.bridge auth --timeout_s 300   # 手动登录（受控 Edge）
python -m tju_info_retrieval.bridge search --query <q> --top_k 5 --sources CNKI   # 超时 150s
```

- **stdout**：单个 JSON 对象；`search` 成功时含 `{"ok": true, "results": [...]}`。
- **结果归一化**：`RetrievalResult(title, snippet, source, score=None, metadata)`，
  缺失字段保持 `None`，适配器从不修改桥接返回的内容。
- **错误分类**：`error_type == "AuthError"` 或错误文案含「登录」→ `TjuAuthRequiredError`；
  其余失败 → `TjuRetrievalUnavailableError`（超时 / Python 缺失 / 无效 JSON 各有专属文案）。

## 结果模型

```python
@dataclass(frozen=True, slots=True)
class RetrievalResult:
    title: str
    snippet: str | None = None
    source: str | None = None
    score: float | None = None
    metadata: dict = ...   # 桥接返回的其余字段原样透传
```

## 环境变量契约

| 变量 | 作用 |
|---|---|
| `TJU_INFO_RETRIEVAL_ROOT` | 外部工程根目录（含 `main.py`、`src/`、bridge 模块）。**必须配置**——适配器不内置任何默认路径 |
| `TJU_INFO_RETRIEVAL_PYTHON` | 外部工程解释器显式覆盖（最高优先级，不校验直接采用） |
| `TEST_USER_DATA_ROOT` | 仅测试：登录态快照根目录覆盖 |

### 解释器解析链（无硬编码路径）

优先级从高到低，逐个用 `python -c "import playwright"` 探测验证
（结果按 (解释器, 模块) 缓存；显式覆盖不校验）：

1. `TJU_INFO_RETRIEVAL_PYTHON` 环境变量
2. `<工程根>/.venv/Scripts/python.exe`、`<工程根>/venv/Scripts/python.exe`
3. 宿主 venv 的 **base** 解释器（`sys.base_prefix`）——手动终端启动实际解析到的那个
4. `sys.executable`（Firefly 自身解释器，兜底）

全部候选探测失败时返回首个存在者，让子进程自己的 stderr 作为最终事实来源。

## 设计要点（对桥接类插件的通用经验）

1. **status 零副作用**：Quick Tools 面板每次打开都会刷新状态。早期实现把状态查询
   做成了 bridge 子进程调用，导致面板刷新就拉起 Python/Playwright/Edge 浏览器窗口。
   修正：状态改为纯本地检查（工程文件存在性 + 登录态快照的**文件时间戳**新鲜度，
   绝不读快照内容），真实凭据校验延迟到实际使用时。
2. **诚实启动探测**：GUI 启动后等待 ~0.7s 轮询子进程；仍存活才报 `opened`，
   秒退报 `launch_failed` 并把 stderr 尾部落入开发日志（不塞进用户聊天）。
   `already_running` 只对**本插件自己 spawn 的**存活进程成立，退出即清引用可重启。
3. **环境脱敏**：启动子进程前剥离全部 `QT_QPA_*` 环境变量——否则宿主测试态的
   `QT_QPA_PLATFORM=offscreen` 会让 GUI 子进程「存活但无窗口」地隐形运行。
4. **有界诊断日志**：GUI 启动尝试写入轮转日志（>512KB 截尾保留 64KB 尾部），
   头部记录时间戳 / attempt_id / 解释器 / 命令 / cwd / 父 pid / 被剥离的 env 键。
5. **凭据边界**：适配器全程不读、不存、不传任何凭据；登录只走受控浏览器的
   手动流程；登录态（storage state）只归属外部工程，适配器最多看文件 mtime。

## 免责声明

- 核心检索工程（TJU_Info_Retrieval）为私有项目，未随本包分发。
- `search` 面向 CNKI 等校内资源，需要持有对应机构的有效登录授权；
  本插件不包含也不会代管任何账号凭据。
