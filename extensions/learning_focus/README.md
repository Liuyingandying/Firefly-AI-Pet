# Learning Focus — Firefly 外部插件（学习专注能力）

> 学习域能力插件（规划 × 记忆 × 评估），符合 FireflyExtension v2 契约，
> 由 Firefly 在启动时从插件根目录自动发现加载。

## 接入方式

- 插件根：Firefly 主仓库 `config/path_config.yaml` 的 `paths.plugin_root`（或环境变量 `FIREFLY_PLUGIN_ROOT`）
- 发现条件：目录为 Python 包（`__init__.py`）+ 暴露 `plugin.py::create_plugin(parent=None)`
- 生命周期：`initialize(context)` → `start()` → `open()*`（Quick Tools）→ `stop()` → `shutdown()`

## 能力

| 入口 | 说明 |
|---|---|
| `open()` | Quick Tools 激活：展示学习状态摘要 + 推荐下一步 |
| `enter_learning(goal)` | 进入学习模式：状态摘要 + 规划任务（宿主 Skill 调用） |
| `submit_answer(node_id, answer, expected)` | 作答评估：证据 → 掌握度更新 → 落盘 |
| `review_due_items()` | 复习到期查询（QTimer 周期调用） |

## 架构

- **规划**：`planner/knowledge_graph.py` + `planner/planner.py` —— 自研最小知识图谱
  （节点分层 prerequisite/core/extension、Kahn 拓扑排序）+ 加权任务规划
  （未见过→diagnose，有误解→teach（先讲后考门控），低掌握→teach/quiz，复习到期→review）。
  内置「自动控制原理」演示图谱作为离线兜底，并留有 `build_graph_from_dict` LLM 结构化构建适配点。
- **记忆**：`memory/learning_memory.py` —— 学习者画像 `profile.json`（原子写）+ 追加式事件
  日志 `events.jsonl`（可重放）。定义 `ExtensionMemoryBackend` 协议：默认文件后端，
  实现该协议即可无缝切换为 Firefly 宿主的 ExtensionMemory。
- **评估**：`learner/evaluator.py`（确定性判定 + judge 适配点，线上可由宿主 LLM 判定）、
  `learner/state.py`（证据折算档位 + 遗忘曲线 `R = 0.9^(elapsed_days / stability)` 复习调度）。

## 数据

- 权威状态：`~/.firefly/learning_focus/profile.json` + `events.jsonl`
  （可用环境变量 `LEARNING_FOCUS_DATA_DIR` 覆盖）
- 轻量事实：经宿主 `ExtensionMemory.remember()` 写入（可被宿主检索）

## 设计参考

- 知识规划：LearningMAP 思想（自研实现）
- 学习者记忆：Inno Agent L1 思想（自研实现）
- 未接入 Tutor-MCP 运行时（仅算法学理留作 v2 复习调度升级来源）

## 测试

算法层为纯标准库实现，测试零外部依赖：

```bash
python -m unittest discover -s learning_focus/tests
```
