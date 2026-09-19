# Firefly AI Pet — Plugin Extension Package

Firefly AI Pet 的插件扩展包（比赛提交 / 开源展示用）。四个插件全部符合
FireflyExtension v2 契约：Python 包（`__init__.py`）+ `plugin.py::create_plugin(parent=None)`
工厂，由宿主 `core/plugin_loader.py` 在启动时自动发现加载，manifest 内嵌于
`QuickToolManifest`（无需 plugin.json）。

## 包内容

| 目录 | 功能 | 分发形式 | 状态 |
|---|---|---|---|
| `firefly_video_extension/` | 本地视频分析：时长 / 场景检测 / 关键帧 / 字幕（调用宿主 VideoProcessor，Phase 2-A 不启用视觉模型） | 完整源码 | ✅ 可直接使用 |
| `firefly_camera_vision/` | 摄像头视觉能力适配器：按需单帧、永不后台开摄像头，设备状态声明卡 | 完整源码 | ✅ 可直接使用 |
| `learning_focus/` | 学习专注：知识图谱规划 × 学习者画像记忆 × 作答证据评估（算法层纯标准库） | 完整源码 + 测试 | ✅ 可直接使用 |
| `tju_info_retrieval/` | 天津大学信息检索**薄适配器**（桥接外部私有工程） | 仅接口契约文档 | 📄 文档分发 |

## 安装

将插件目录放入 Firefly 的插件根目录即可（默认 `%LOCALAPPDATA%/FireflyAI/plugins`；
可用 `FIREFLY_PLUGIN_ROOT` 环境变量或 `config/path_config.yaml` 的 `paths.plugin_root` 指定）。

## 依赖概览

- 宿主模块（由 Firefly 主仓库提供）：`core.extension_api`、`core.quick_tools`、
  `core.video_pipeline`、`core.plugin_api`
- 第三方：PySide6（宿主已含）；`firefly_video_extension` 的视频能力依赖宿主管线的
  FFmpeg / 场景检测 / OCR
- `learning_focus/learner|memory|planner`：纯 Python 标准库，测试可独立运行：
  `python -m unittest discover -s learning_focus/tests`

## 各插件说明

详细功能、架构与设计要点见各插件目录内 README / 文档：

- `firefly_video_extension/`、`firefly_camera_vision/`：见源码模块 docstring
- `learning_focus/README.md`：架构、能力面、数据布局、测试
- `tju_info_retrieval/INTERFACE.md`：桥接协议、状态机、环境变量契约、
  桥接类插件的通用设计经验

## 分发边界说明

- 本包**不含**任何 API 密钥、token、cookie、用户数据或本机绝对路径。
- `tju_info_retrieval` 的核心检索工程为私有项目，本包仅公开宿主侧适配契约；
  复现该插件需要按 `INTERFACE.md` 实现同构的 bridge CLI。
- TJU 登录态（cookies/storage state）归属外部工程，本包不含、也不读取其内容。
