# INTERFACE — Firefly 插件接入契约总览

本文件概述 Firefly AI Pet 对外部插件的统一接入契约；各插件更细的能力说明见其目录内
README / docstring，桥接类插件的完整协议见 [`tju_info_retrieval/INTERFACE.md`](tju_info_retrieval/INTERFACE.md)。

## 统一接入契约（Extension API v2）

| 环节 | 契约 |
|---|---|
| 插件发现 | 宿主启动时扫描插件根目录（Plugin Root）；目录为 Python 包（含 `__init__.py`）且 `plugin.py` 暴露 `create_plugin(parent=None)` 工厂即视为插件 |
| 生命周期 | `initialize(context)` → `start()` → `open()`（Quick Tools 激活）→ `stop()` → `shutdown()` |
| 能力声明 | `QuickToolManifest` 内嵌于插件（id / name / description / capabilities / version / min_api），无需额外 plugin.json |
| 门控 | 逐插件持久化 Enable 开关 + 宿主禁用名单；插件调用前经能力门控检查 |
| Fail-closed | 包结构不符、工厂缺失、返回类型不符或命中禁用名单即拒绝加载，异常隔离在插件边界内 |
| 状态发布 | 插件经状态总线发布 READY / WORKING / OFFLINE / ERROR，Quick Tools 面板据此渲染入口与状态 |

Plugin Root 默认位于 Firefly 用户数据目录的 `plugins` 子目录，可用环境变量
`FIREFLY_PLUGIN_ROOT` 或 `config/path_config.yaml` 的 `paths.plugin_root` 重定向。

## 四插件能力入口一览

| 插件 | 主要入口 | 详细契约 |
|---|---|---|
| firefly_video_extension | `open()`：选择本地视频，输出时长 / 场景 / 关键帧 / 字幕摘要（复用宿主视频管线） | 源码模块 docstring |
| firefly_camera_vision | `status()` / `start()` / `stop()`：设备可用性声明与能力暴露（永不后台开启摄像头） | 源码模块 docstring |
| learning_focus | `open()`、`enter_learning(goal)`、`submit_answer(node_id, answer, expected)`、`review_due_items()` | [`learning_focus/README.md`](learning_focus/README.md) |
| tju_info_retrieval | `search(query, top_k)`、`open_login()`、`open_ui()`、`status()` | [`tju_info_retrieval/INTERFACE.md`](tju_info_retrieval/INTERFACE.md) |

## 桥接类插件

`tju_info_retrieval` 为薄适配器：Firefly 侧零检索逻辑，每个请求通过一次性隔离子进程
调用外部工程的 Bridge CLI，以 JSON 协议返回并归一化。其完整桥接协议、状态机与环境
变量契约见 [`tju_info_retrieval/INTERFACE.md`](tju_info_retrieval/INTERFACE.md)。
