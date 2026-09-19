# 新用户安装验证（New User Installation Verification）

验证日期：2026-09-19 · 对象：`v1.0-rc2` 便携包 + 插件仓 tag `v1.0.0`
验证方式：**真实下载 + 隔离启动实测**——模拟一台无 Python、无项目目录、无历史数据的新 Windows 机器，全程通过 `FIREFLY_USER_DATA_DIR` 将用户数据根指向独立空目录，与开发者日常数据（2,500+ 文件）做逐文件快照比对。

## 验证路径

1. 从 GitHub Release 下载 `Firefly_AI_Pet_v1.0-rc2_win64.zip`（220,368,989 bytes）与 `.sha256` 校验文件
2. `certutil -hashfile … SHA256` 校验一致后解压到用户可写目录
3. 双击 `Firefly_AI_Pet.exe` 首次启动（未配置任何密钥）
4. 观察首启行为：用户数据目录创建、凭据状态、会话内容、与既有数据的隔离
5. 下载插件仓 tag `v1.0.0` 归档，按插件默认安装路径放置后重启验证

## 验证结果

| # | 验证项 | 结果 |
|---|---|---|
| 1 | 下载完整性 | ✅ 下载件 SHA256 与 GitHub `.sha256` asset、本地构建件三方一致 |
| 2 | 解压结构 | ✅ 1,109 项：入口 `Firefly_AI_Pet.exe` + `_internal/` + `extensions/` + `.env.example` + 发行说明 |
| 3 | 启动入口 | ✅ 双击启动，25 秒内稳定运行，桌面宠物正常渲染 |
| 4 | 用户数据目录自动创建 | ✅ 首启自建完整结构：`conversation/ learning/ logs/ memory/ models/ plugins/ runtime/ scratchpad/ suggestions/` |
| 5 | 无 Key 可启动 | ✅ 包内无真实 `.env`；启动后不生成任何凭据，状态机 `idle`，全程无崩溃（设计行为：无密钥优雅降级） |
| 6 | 全新会话 | ✅ 会话文件为空（`turns: []`）；首启迁移检查报告"无遗留数据" |
| 7 | 开发者数据零接触 | ✅ 隔离期间既有数据目录 **0 增 0 删**（2,566 文件快照逐字节比对） |
| 8 | 用户数据不进 Git / Release | ✅ 数据根位于 `%LOCALAPPDATA%`（仓库之外）；Release 包 1,109 项与已发布 Git 树均无任何会话/记忆/凭据内容 |
| 9 | 插件安装路径 | ✅ 宿主自动创建 `plugins/` 目录；发行配置声明默认 `%LOCALAPPDATA%/FireflyAI/plugins`，支持 `FIREFLY_PLUGIN_ROOT` 覆盖 |
| 10 | 插件包契约 | ✅ 3/4 插件含 `plugin.py::create_plugin` 工厂；`tju_info_retrieval` 为接口契约文档分发（符合声明） |
| 11 | 测试后环境恢复 | ✅ 验证结束删除隔离实例后，日常实例恢复正常 |

## 未验证项

以下项需要 GUI 自动化或特殊硬件环境，本次未覆盖（不影响迁移主路径结论）：

1. 插件在快捷工具面板中的 UI 级加载证据与功能调用（目录识别层已验证）
2. 无 Key 状态下对话界面内的降级提示文案
3. AI 模型管理窗口的密钥增删改流程（另有 RC2 用户安装测试报告覆盖）
4. 首次记忆写入触发的嵌入模型下载（约 30MB，受限网络需设置 `HF_ENDPOINT` 镜像）
5. 无串口硬件机器上的首启行为（设计为硬件桥缺失不阻断启动）

## 已知提示

- 首次运行会有 SmartScreen 提示（包未签名），选择"仍要运行"；代码签名在 Roadmap
- 必须解压到**用户可写目录**（Program Files 会导致偏好设置写入失败）
- 同机仅允许运行一个实例（单实例互斥设计）
- 退出请使用托盘右键菜单，避免直接结束进程跳过优雅关闭
- `tju_info_retrieval` 为桥接外部工程的契约分发，普通用户可跳过
