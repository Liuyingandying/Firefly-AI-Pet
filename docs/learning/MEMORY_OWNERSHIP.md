# Learning Memory Ownership Contract（v0.1 冻结）

状态：**冻结**。Learning Bridge v0.1 起生效。任何一方越界保存他方权威数据即架构错误。

## 总原则

**Firefly：read-through display。teach-mcp：source of truth。**

学习事实（掌握度、错误模型、进度、题目）只有一个权威来源：teach-mcp。
其他任何组件保存、缓存、推导这些事实作为"权威值"都被禁止；展示必须实时查询。

---

## Firefly Memory 负责

允许保存（长期、稳定、非学习事实）：

- `learner_id`（Firefly 生成，一次生成终身不变，存 `pet_preferences.json: learning.learner_id`）
- 长期学习偏好，例如：
  - `visual_first`
  - `prefer_examples`
  - `explanation_depth`
- 最近使用过的 `course_id`（已有先例 `learning.last_course_id`）
- 用户对学习模式的稳定偏好（如默认 action、上次打开的课程）

禁止保存：

- mastery 数值
- misconception 记录
- quiz history
- curriculum progress
- session progress
- 当前题目
- 当前章节完成度

Firefly 的 memory_records.json（主 Memory）不承担任何学习状态职责。

## Learning Workspace 负责

（即 `%LOCALAPPDATA%/FireflyAI/learning/courses/<course_id>/`）

只保存：

- 原始学习资源（`source/`）
- `course_id`
- `resource_id`
- manifest（`manifest.json`）
- teach-mcp 返回的 opaque binding IDs（`bridge/course_binding.json`）
- Learning Context 快照（`workspace/.firefly/learning_context.json`，launch 时原子重写）

不得保存第二份 mastery / progress。

## teach-mcp 负责

唯一权威来源：

- learner profile 中的学习状态
- learning_sessions
- mastery
- misconceptions
- quiz results
- curriculum
- next_topic
- session
- teaching progress

存储：`E:\Firefly_AI_MCP\teach_mcp\state\session.db`（SQLite，实测表：learner_profiles / learning_sessions / knowledge_mastery / misconception_records / results）+ `knowledge/`（正式包）+ `draft_packages/`（待审草稿）+ `state/book_pipeline/`（pipeline 状态）。

Firefly / Z Code / skill 一律只通过 teach-mcp 工具读写这些状态，不得直接改写其数据库。

## Z Code 负责

只作为：

- **Agent Host + Tool Orchestrator**

不得作为长期学习状态源。

- Z Code conversation 被删除后：学习进度必须仍可从 teach-mcp 恢复（`resume_learning(learner_id)`，按 learner 取最近 open session，与会话 ID 无关）。
- Z Code 会话文件、checkpoint、Memory 中不得保存被视为权威的学习进度。skill 恢复学习只信 teach-mcp 返回值。

---

## 身份责任方（不可变更）

| ID | 生成方 | 存储 |
|---|---|---|
| `learner_id` | Firefly（一次生成） | pet_preferences.json |
| `course_id` | Firefly Resource Manager | manifest.json |
| `resource_id` | Firefly Resource Manager | manifest.json |
| `session_id` | **teach-mcp**（服务端生成） | teach-mcp session.db |
| `pipeline_id` / `curriculum_id` | teach-mcp | binding 文件只允许原样保存（opaque） |

严格禁止：

- Firefly 生成 session_id
- Z Code 生成 session_id
- TJU LLM（或任何模型）自造 session_id / curriculum_id / pipeline_id

所有后续 teach-mcp 调用必须使用 teach-mcp 返回的 session_id 原值。

## 违规判定（自动防护）

以下字段出现在 Firefly 自维护的持久化文件中作为权威字段，即视为架构错误
（由 tests/test_learning_bridge_memory_guard.py 强制）：

- `mastery`
- `misconception_records`
- `quiz_history`
- `learning_progress`

例外：仅作**实时透传展示**（值来自 teach-mcp 当次查询、不落盘）允许。
