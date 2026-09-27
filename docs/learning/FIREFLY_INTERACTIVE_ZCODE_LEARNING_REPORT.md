# Firefly Learning Bridge — Interactive Z Code Learning Restore 报告

日期：2026-09-27
性质：产品方向纠偏（Target UX 冻结）：学习发生在 **Z Code**；Firefly 只做入口/身份/资源管理。未触碰 teach-mcp 业务、Memory Ownership、Resource Manifest、curriculum 算法、mastery、renderer、Firefly 学习 UI 布局。

> **INTERACTIVE_LEARNING_V01_FROZEN** —— Session Reuse Gate 验收通过（见文末"Session Reuse Gate"章节）：
> existing session → reuse（--resume 原会话，零新 init）；missing session → rebuild（新建 + teach-mcp 进度无损）。

---

## 实现总览

```
Firefly（学习模式 → 选课程 → 开始/继续学习）
  │ launch_learning_mode(course_id, action)
  │   ├─ Learning Context（含 task_id；action 含 answer）
  │   ├─ bridge/init.json ← init run 的官方 --json 输出（含 sessionId）
  │   ├─ bridge/task.json（task_id / pending→running / pid）
  │   └─ headless init run：node zcode.cjs -p <bootstrap> --cwd <ws> --json
  ▼
BridgeLearningSession（interactive 轮询）
  │ init.json 就绪 → sessionId 提取
  │ → binding.zcode_session_id = <sessionId>（opaque UI 恢复引用）
  │ → open_learning_session：wt.exe -d <ws> node zcode.cjs --resume <sessionId>
  ▼
Z Code TUI（课程专属可交互会话）
  ├─ 立即可见：init 轮的 bootstrap 执行记录（欢迎回来/进度/讲解/题目）
  └─ 用户直接在 TUI 内回答/追问 → skill 持续调 teach-mcp
```

Return Channel（result.json → Firefly 聊天 + 回答拦截）**冻结为 `embedded` 实验模式**：`BridgeLearningSession(mode="embedded")`，默认 `interactive` 不启用；代码与测试保留未删。

## 1. Z Code Desktop 如何创建新 task？

桌面端无公开的"创建 task + prompt"入口（app.asar 审计：有 single-instance/second-instance/open-url/deep-link 与 workspace 打开，但无 prompt/session 注入语义）。**创建任务的官方途径是 CLI**：`zcode -p <prompt> --cwd <ws>` 每次产生一个新的持久化 session（实测各轮独立 `sess_*`）。

## 2. 如何注入 initial learning prompt？

`-p/--prompt`（bootstrap prompt：firefly-learning skill + context 路径 + action + 8 条规则 + result 写入要求）。TUI 无 initial prompt 参数、桌面无注入口——因此采用**两段式**：init run 先以 `-p` 执行 bootstrap（一轮：恢复状态/讲解/出题，不跑完整个课程），其全部输出进入 session 历史。

## 3. 如何指定 course workspace？

`--cwd <课程>/workspace`。workspace 由 Resource Manager 的托管目录派生（`%LOCALAPPDATA%/FireflyAI/learning/courses/<course_id>/workspace`），与原始下载位置解耦。

## 4. 如何取得 zcode_session_id？

init run 追加 `--json`（官方 machine-readable 输出），stdout 由 launcher 重定向到 `bridge/init.json`；`read_init_session_id()` 解析出 `sessionId`（实测：`-p "只回复OK" --json` → `{"sessionId":"sess_9d27…",...}`）。随后写入 `course_binding.json` 的新 opaque 键 `zcode_session_id`（binding 白名单已扩展；会话校验器同步放行）。

## 5. Firefly 如何打开正确课程 session？

`open_learning_session(course_dir, session_id)`：`wt.exe -d <ws> node zcode.cjs --resume <sessionId>`（无 wt 时新控制台兜底；wrapper 探测保留）。**精确按 sessionId 恢复**，实测两个证据：①`--resume` 参数进入 TUI 分支；②`--resume <sess> -p "复述上一条" --json` 同 sessionId 续问成功——会话跨调用持久。RESUME 优先级：binding.zcode_session_id 可用则精确恢复；session 丢失（人为删除/换机）则 init run 天然创建新 session 并更新 binding——学习状态从 teach-mcp 无损恢复，课程永不丢。

## 6. 是否还会打开开发会话？

**不会。** 桌面壳 `ZCode.exe` 的误判路径已被移除（wrapper 探测改为 PATH-only + 回归测试 `test_desktop_shell_is_never_mistaken_for_a_wrapper`）；现在只按 `--resume <sessionId>` 打开课程专属 session，不存在"最近会话"语义（launcher 从不使用 `-c`）。

## 7. 用户实际在哪里学习？

**Z Code TUI**（课程 workspace 中的课程专属交互会话）。init 轮的 bootstrap 输出（“欢迎回来…进度…讲解…题目”）在该会话历史中，用户打开即见，直接在 TUI 内输入 `A/B/C/D`、"我不懂这里"、"再解释一下"继续学习；skill 持续调用 teach-mcp 记录一切。

## 8. Firefly 聊天框是否退出默认教学职责？

**是。** 默认 `interactive` 模式下 `handle_incoming_text` 恒返回 None（普通聊天完全不被拦截）；pending-question 回答拦截与 result→聊天展示仅存在于 `mode="embedded"`（实验分支，Return Channel 代码与测试全部保留未删）。聊天窗只显示两条非模态状态："已交给学习代理，正在打开 Z Code 学习空间……"与"课程已在 Z Code 中打开。直接在 Z Code 中继续学习即可。"

## 9. 删除 Z Code session 后能否恢复学习？

**能。** 每次「继续学习」的 init run 都创建全新 session（新 task_id、新 sessionId）；binding 的 `zcode_session_id` 随之更新为最新引用。人为删除旧 session 后：init 照常执行 → 新 sessionId → 新 TUI 会话，teach-mcp 的学习进度（quiz 40% 等）无损恢复。测试 `test_deleted_zcode_session_is_rebuilt_fresh`（sess-FIRST → sess-SECOND 迁移断言）。

## 10. teach-mcp 是否仍是唯一学习状态源？

**是。** zcode_session_id 是 UI 恢复引用（opaque ref），非学习状态；binding 白名单受校验器约束；skill 在 Z Code 内仍必须 `learning_status/resume_learning` 从 teach-mcp 校准真实状态；Firefly 全程不保存 mastery/progress/quiz。Memory Ownership 契约零变更。

## Phase 11 验收指标状态

| 指标 | 状态 |
|---|---|
| process_opened | ✓（headless init + TUI 两个进程均真实 spawn） |
| task_created | ✓（task.json，task_id 每次 launch 唯一） |
| correct_session_opened | ✓（`--resume <sessionId>` 精确 + binding 绑定测试） |
| skill_started | ✓（init 轮输出为 skill 协议行为，v0.1 PoC 与本轮均实证） |
| mcp_called | ✓（resume/出题/判分真实读写 session.db，E2E 实录） |
| learning_visible_in_zcode | ✓（TUI resume 打开即见 init 轮教学与题目——历史在会话内；最终肉眼验收待用户） |

仅 `ZCode.exe opened` 不再可能出现：launcher 已无桌面壳路径。

## Phase 12 测试（新增/更新，全量 89 passed）

- `test_learning_return_delivery.py`（interactive 7 项）：emit 先于模态/无成功模态框、点击即 attach（init poller 存活）、**interactive 全链**（init.json → binding.zcode_session_id → TUI open → 非模态通知）、init 未完成不打开不展示、默认模式零回答拦截、A/B 课程 session 隔离、session 删除后重建。
- `test_learning_return_channel.py`（15 项）：整体标注 **embedded（experimental）**，`mode="embedded"` 下保留全部 Return Channel 行为。
- `test_learning_bridge.py`：test_14 session_id 规则细化（mint 面严禁 / echo 层豁免 + uuid/secrets import 禁令）。

## 附：本阶段修复的工程问题

- `str.format` 模板含 JSON 大括号 → 改 token replace；
- fake spawner 四参签名与真实五参（stdout_path）不一致导致 kwarg TypeError → 统一五参；
- `_poll` 内函数级 import 绕过 monkeypatch → 提到模块顶部；
- offscreen 测试中 `QMessageBox` 模态会造成假死 → 失败路径统一结构化 + 测试补属性防 AttributeError 入 except。

**结论：Interactive Z Code Learning Restore 落地——Firefly 点「继续学习」→ 自动打开该课程专属 Z Code 交互会话（内含 bootstrap 执行记录与题目）→ 用户在 Z Code 内学习，teach-mcp 唯一权威。按要求停止，不继续 Return Channel 产品化。**

---

## Session Reuse Gate（v0.1 冻结验收，2026-09-27）

### 正确语义（已实现并冻结）

```
首次学习： course 无 zcode_session_id → init run 创建 session A
          → binding.zcode_session_id = A → --resume A 打开 TUI
再次继续： binding 已有 A 且存在性查询通过 → 直接 --resume A
          （零 init run、零 -p bootstrap、task.json/init.json 原封不动）
session 丢失/损坏： 存在性查询失败（reason=resume_failed）
          → REBUILD：init run 创建 session B → skill 调 teach-mcp
            resume_learning 恢复真实进度 → binding 更新为 B
```

实现：`launch_learning_mode` 的 **Session Reuse Gate**（action != "new" 时：
`binding.zcode_session_id` 存在且 `zcode_session_exists()` 通过 →
`open_learning_session(--resume)`，plan.mode="session-reuse"，直接返回；
存在性探测 = 只读查询 Z Code 自身 `~/.zcode/cli/db/db.sqlite` 的 `session`
表（零副作用；查询失败时乐观放行——引用由我方 init 写入，坏会话由 CLI
自行暴露）。标记：`[ZCODE_SESSION_REUSE]` / `[ZCODE_SESSION_REBUILD]`。

### 真实验证（course crs-d91e981f5a92）

| 验证 | 结果 |
|---|---|
| 第一次继续学习 | init run（task-c2e3f330b3）→ 真实 sessionId `sess_b1d61634-7ded-…` → binding 写入 |
| **第二次继续学习** | **mode=session-reuse；argv 为空（零 bootstrap init）；REUSE_SAME_SESSION=True；init.json/task.json mtime 原封不动** |
| Phase 3 重建 | binding 篡改为 `sess-DELETED`（真实 db 查无）→ REBUILD 分支 → 真实 init run（task-6145623db0）→ 新 session `sess_ed24d994-…` ≠ DELETED；**teach-mcp 完全连续**（mastery 0.5/2、results 2、session quiz/0.4 零漂移——rebuild 仅 resume_learning+出题，无副作用写入） |
| 课程隔离 | A/B 各自 binding 独立（`test_course_a_b_bind_their_own_sessions`：sess-A/sess-B 各归各） |

### 测试（tests/test_learning_return_delivery.py，10 项）

1. first launch creates session ✓
2. second launch reuses same session ✓（mode=session-reuse + 同 id open）
3. existing session 不触发 -p init ✓（argv 为空 + task.json 未变）
4. missing session triggers rebuild ✓（REBUILD 标记 + init 重建）
5. rebuild preserves teach-mcp progress ✓（真实 E2E + rebuild prompt 含 resume 语义）
6. course A/B session isolation ✓
7. zcode_session_id remains opaque only ✓（binding 白名单 + context schema 无此字段）

**INTERACTIVE_LEARNING_V01_FROZEN**
