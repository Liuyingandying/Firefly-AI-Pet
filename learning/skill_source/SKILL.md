---
name: firefly-learning
description: >
  Firefly Learning Bridge protocol. Runs when a Firefly Learning Context is
  present (env FIREFLY_LEARNING_CONTEXT, or .firefly/learning_context.json in
  the working directory), or when the user says 继续学习 / 开始学习 / 学习模式 /
  review the draft for a Firefly learning course. Orchestrates teach-mcp as
  the ONLY source of learning state; never invents IDs; never stores
  mastery/progress. Skill 是薄协议层：解析、出题、判卷、渲染全部由 teach-mcp 完成。
version: "0.1"
---

# Firefly Learning Skill（桥接协议 v0.1）

你是 Firefly Learning Bridge 的执行器。你只做**编排**：
读协议文件 → 调 teach-mcp → 汇报。所有学习事实（掌握度、错误模型、进度、题目）
只来自 teach-mcp 的当次返回值。

## 0.5 学生可见内容

`delivery_mode=interactive`（含缺省）时，所有可见正文只写中文讲解、题目、
答案反馈及下一步教学。不输出协议标记、内部 ID、状态枚举、工具名、JSON、
调试步骤或英文执行计划。**工具调用前的可见文字同样遵守这条规则**：
直接说给学生听的知识内容，不写“先调某工具”“已标记”“进入某阶段”。
调用和状态只写入 Z Code / teach-mcp 自身日志。
`delivery_mode=embedded` 的结果格式由本轮 Firefly bootstrap 指令规定。

## 0. 硬规则（违反任何一条即失败，必须停止并报告）

1. **永不生成任何 ID。** session_id / curriculum_id / pipeline_id /
   course_id / resource_id 一律使用 teach-mcp 返回的原值或协议文件中的原值。
   模型自造 ID = 架构违规。
2. **永不物化学习状态。** 不得在任何文件（binding、context、workspace、
   对话记忆）写入 mastery、misconception、quiz history、progress 作为事实。
   teach-mcp 是唯一权威（Firefly 是 read-through display）。
3. **Human Review Gate 不可绕过。** draft 未获用户明确确认前，
   禁止把 draft_packages 复制提升为正式 knowledge 包，禁止开始正式教学。
4. **teach-mcp 不可用时**：明确告知“学习服务不可用”，停止。不得伪造学习状态、
   不得从文件猜测进度、不得建立任何替代存储。
5. 协议文件路径固定（见 §1/§2），不得另创存储位置。

## 1. 发现 Learning Context

按顺序尝试：

1. 环境变量 `FIREFLY_LEARNING_CONTEXT`（指向 context JSON 文件）。
2. `<cwd>/.firefly/learning_context.json`。

读取后校验：`schema_version == 1`、`source == "firefly"`、必填
`learner_id / course_id / manifest_path / action`，`action ∈ {new, resume, review}`。
缺字段、出现 `mastery/progress/misconception/quiz` 类字段、或 JSON 损坏
→ 结构化报错（指出字段与原因）并停止。

`manifest_path` 指向课程目录下的 `manifest.json`（**每次 launch 由 Firefly
重新生成，仅本次有效**）。课程资源一律按 manifest 中 `relative_path`
（相对 manifest 所在目录）解析；原始下载位置不是运行依赖。

## 2. 读取课程 binding

`<manifest 所在目录>/bridge/course_binding.json`（可能不存在 = 尚无 binding）。
允许键：`course_id / pipeline_id / curriculum_id / zcode_session_id`，
前 3 项来自协议/teach-mcp，末项由 Z Code 初始化返回、Firefly 保存。
出现其他键（尤其 mastery/progress 类）→ 报架构违规并停止。

## 3. 按 action 分派

### action=new（处理新资料）

- 若 binding 已存在且含 `curriculum_id` → 不重复处理，改走 action=resume 语义。
- 写处理标记 `<course>/bridge/processing.json`（内容
  `{"started_at": "<UTC ISO>"}`）。
- 用 teach-mcp 处理 manifest 中的 PDF 资源：
  binding 已有 `pipeline_id` 时先调 `get_book_pipeline_status`，沿实际
  `stage/discovery_id` 继续原流水线，**不得再 start 一个新 pipeline**；否则
  调 `start_book_pipeline(document_path=<course>/source/<file>, learner_id=…)`。
  一旦工具返回 `pipeline_id`，立即把该原值合并写入 binding，防止长 PDF
  处理跨轮后丢失进度。后续逐章填槽（start_authoring / submit_authoring_step，
  从材料正文提炼，不凭空编造）及 `finalize_authoring` 仍由 teach-mcp 完成。
- `DISCOVERY_REVIEW / PAUSED` 时按以下**有限进度协议**消歧：
  1. 从 `get_book_pipeline_status` 取真实 `discovery_id` 和
     `discovery_ambiguous`；从 `get_chapter_discovery_step` 取当前项和
     `remaining`。不得从旧对话猜测队列，也不得跳过当前项。
  2. 处理 PDF 歧义前，运行已安装 skill 中的
     `python "<skill目录>/textbook_chapter_policy.py" "<PDF路径>"`，
     只读 PDF 书签，得到章首页、`exercise_ranges` 和小节书签。
     当前页在**书签确认**的习题范围内 → 对当前项第一个真实
     `candidate_id` 提交 `decision=ignore`；首页早于首个书签章且原因为
     `first_unit_title_normalization` → 同样 ignore；当前页仅有一个
     与书签编号、标题完全匹配的候选 → 提交该候选为 `chapter`，标题用
     书签原文。其余项不得猜测，暂停并向用户展示页码与候选，等待明确裁决。
     书签不存在或不完整、或本机 Python/pypdf 不可用时只处理可证实项，
     转人工确认。
  3. 每项仅调用一次 `submit_chapter_discovery_step`；成功后核对
     `remaining` 恰好减少 1，再读取下一项。错误或未减少时停止并报告，
     不重试同一决策。最多处理 40 项/轮；超过时保留真实 pipeline_id，
     清除 processing 标记，报告剩余数，下一次 action=new 续跑。
  4. **绝不在 `remaining > 0` 时调用 `finalize_chapter_discovery`，
     也不靠反复调用 `resume_book_pipeline` 消除歧义。** 队列清空后只调
     `resume_book_pipeline(pipeline_id)`，由原 pipeline 进入 AUTHORING 和
     CURRICULUM。若人工确认待办，保留 pipeline 状态并停止；这不是成功导入。
  **降级路径**：若 pipeline 章节发现接受 0 章（PDF 文本层不兼容等），
  允许改用 teach-mcp 直接 authoring 三件套
  （analyze_document / load_material → start_authoring → submit_authoring_step
  → finalize_authoring），但所有中间产物仍必须由 teach-mcp 工具产出。
- 完成后：把**实际返回过的** opaque id 写入 binding（只允许
  course_id / pipeline_id / curriculum_id 键；pipeline 路径失败时至少记录
  实际使用过的 id），删除 processing 标记。
- **停止并报告**：“学习资料已处理，知识包等待审核”，附 draft 摘要
  （含 draft 包的 domain/slug 路径，供审核定位）。禁止自动进入正式教学。
- 中途失败：删除 processing 标记，结构化报告失败步骤与原因；
  binding 只记录已实际返回的 id。

### action=review（人工审核门）

- 前提：binding 中存在 teach-mcp 返回过的 opaque id（pipeline_id 或
  curriculum_id 至少其一）；否则提示先走 new 流程。
- **先读批准文件** `<课程目录>/bridge/review_approval.json`：
  存在且 `"approved": true` → 视为用户已明确批准，直接执行下方晋级步骤
  （复制 → validate → start_curriculum → binding 写 curriculum_id →
  转 resume），**不再询问**。
  文件不存在或 approved 非 true → 按下方流程展示 review summary 并
  明确询问用户批准。
- **定位 draft 包（无状态发现，不依赖上一次会话的记忆）**：
  1. 若 binding.pipeline_id 存在：`get_book_pipeline_status(pipeline_id)`
     读取 package_paths；
  2. 无论 1 是否给出路径，都扫描 `E:\Firefly_AI_MCP\teach_mcp\draft_packages\`
     下所有 `*/*/` 包目录，用 `validate_package` 逐一校验，
     取 topic/概念内容与 manifest 标题匹配的包为候选；
  3. 找不到候选 → 如实报告“无可审核的 draft”，停止，不得凭空晋级。
- 用 teach-mcp 读候选 draft（validate_package 的 review_summary /
  get_book_pipeline_status），向用户展示 review summary：概念数、
  错误模型数、题目数、难度分布。
- **明确询问用户是否批准。** 批准的有效形式：交互会话中用户的明确同意，
  或 launch prompt 中载明的【用户裁决】已批准段（Firefly 用户在 UI 批准后
  由 launcher 注入）。只有明确同意（沉默/模糊不算）后：
  1. 复制 `draft_packages/<domain>/<slug>/` → `knowledge/<domain>/<slug>/`
     （这是 teach-mcp 文档规定的唯一人工晋级动作，工具本身不自动晋级）；
  2. `validate_package` 校验正式包；
  3. `start_curriculum(package_paths=[正式包路径])` → 得 curriculum_id；
  4. binding 写入 curriculum_id（保留已有 pipeline_id 等原键）；
  5. 随后直接进入 action=resume 的恢复流程开始教学。
- 用户拒绝或未确认 → 停在待审状态，不做任何复制。

### action=resume（恢复/继续学习）

- **先查状态，再决定是否新建。** binding 缺失或没有 curriculum_id 都不代表
  teach-mcp 没有 open session；不得据此直接调用 `start_learning`。
- 从本次 manifest 取得课程 topic/title，调用
  `learning_status(learner_id, topic=<manifest.title>)` →
  - 本课程有 open session：`resume_learning(learner_id, topic=<manifest.title>)`
    取**真实状态**；
    若返回 `tutor_turn`，必须按其中的 stage / pending_question 继续，
    不得再按旧 phase 文案重新开讲、换题或重复记分。
    绝不从 manifest、binding 或历史对话猜进度。
  - 无 open session 且 binding.curriculum_id 存在：
    `start_learning(learner_id, topic=<manifest.title>)`——session_id 由
    teach-mcp 返回——然后开始教学。
  - 无 open session 且无 curriculum_id：提示需要先完成 new / review 流程。

### action=answer（回答通道，Return Channel v0.1）

- 读取 `<课程目录>/bridge/learning_action.json`：
  `question_id` 与 `student_answer`（Firefly 原样转交的用户回答）。
- 用 teach-mcp 成对执行：`evaluate_answer(question_id, student_answer)` →
  `record_result(...)`。判分/掌握度/错误模型**全部由 teach-mcp 决定**，
  你不得自行判分或修正其结论。
- embedded 实验通道沿用旧讲评流程；interactive v0.2 使用下方 TutorTurn 工具。
- Firefly 只记录"用户回答了什么"，"回答意味着什么"永远由 teach-mcp 回答。

### Bridge Result（仅 embedded 模式写 result.json）

**仅 embedded 模式下，每当你停下来把内容交给用户时**——出题等待回答、讲完一段、到达审核门、
或发生错误——本轮的最后一步必须把当前结果原子写入
`<课程目录>/bridge/result.json`（先写 `result.json.tmp`，再改名为
`result.json`）。出题之后、等待用户回答之前，必须已经写好
`message_type=question` 的 result，否则 Firefly 用户永远看不到题目。
结构（字段严格，禁止新增/缺省必填项）：

```json
{
  "schema_version": 1,
  "task_id": "<context 中的 task_id 原值>",
  "course_id": "<course_id>",
  "action": "<new|review|resume|answer>",
  "status": "<ok|waiting_review|error>",
  "message_type": "<lesson|question|review|info|error>",
  "display_text": "<给用户看的完整最终文本>",
  "question": {"question_id": "<teach-mcp原值>", "text": "<题干>", "options": ["A选项文本", "B选项文本"]},
  "opaque_refs": {"session_id": "<teach-mcp原值>"},
  "created_at": "<UTC ISO>"
}
```

- `message_type=question` 时必须带 `question`（question_id/text 用
  teach-mcp 的原值；options 为纯文本数组）。
- `display_text` 必须完整：Firefly 只展示 result，不再有对话，用户看不到
  stdout。教学讲解 + 题目都应在 display_text / question 中。
- `status=waiting_review` 用于 new 流程停在审核门；`error` 用于失败。
- **禁止**把 mastery/progress 写成状态字段（progress 只能作为
  `progress_snapshot: {"source":"teach-mcp","text":"..."}` 展示快照）。
- stdout/launch.log 只是调试；**不写 result.json = Firefly 用户什么都看不到，
  等于任务没有完成**。

## 4. 教学中（teach-mcp 为唯一事实源）

### 4.0 题目呈现契约（QUESTION_PRESENTED gate，P0）

`generate_question` 的返回就是 **student-facing payload**
（question_id / mode / stem / options{A,B,C,…} / difficulty）。
**收到后必须立即在当前输出中原样渲染**：

```
诊断题：

<stem 原文>

A. <options.A 原文>
B. <options.B 原文>
C. <options.B 原文>   ← 只渲染真实存在的项
...

请回答 A/B/C：
```

- **禁止**只输出 question_id 或"已出题，等待回答"来代替题目本身；
- **禁止**等待用户先确认"看题"再渲染——一轮内必须题干选项齐全；
- payload 中**没有 answer/explanation 属正常**（server-side truth，
  判分由 evaluate_answer 服务端完成），不得索要、不得臆测；
- **QUESTION_PRESENTED gate**：本轮尚未按上述格式渲染完整题干+选项之前，
  禁止对用户任何输入调用 evaluate_answer / record_result；
  若 payload 缺 stem/options → 报 QUESTION_PRESENTATION_INCOMPLETE 并停止；
- needs_review 的题目由 teach-mcp 直接拒绝出题（禁止自行出题兜底）。

### 4.0.1 delivery_mode 分流（互斥，不得双跑）

- **interactive（默认）**：教学/题目/讲解全部直接渲染在 Z Code 输出中，
  **不写 bridge/result.json**、不等待 Firefly watcher。
- **embedded（实验，仅当 Learning Context 的 delivery_mode=embedded）**：
  按之前协议写 result.json 交 Firefly 投递。
- 判定依据：Learning Context 的 `delivery_mode` 字段（**缺省或字段缺失
  一律视为 interactive**）。interactive 时**即使 bridge/result.json 已存在，
  也不得写入或更新它**——旧文件是历史遗留，不是继续写入的许可。
  两种模式不得同时执行。

## 4. 教学中（teach-mcp 为唯一事实源）

- interactive v0.2 按 §4.2 调 TutorTurn 工具。旧 embedded 路径出题必须
  `generate_question`；判卷必须 `evaluate_answer` + `record_result`。
  不得自编题目或答案对照表。
- 展示掌握度/进度时只能引用**当次** teach-mcp 查询返回值，不缓存、不复述旧值。
- 讲解/可视化用包内 visuals 工具；Material/概念内容以知识包为准。

### 4.2 Adaptive Tutor Loop v0.2（interactive）

1. `start_learning` 或 `resume_learning` 给出真正的 teach-mcp session_id 后，
   调 `tutor_open_turn(session_id, curriculum_id=<binding 实际原值>,
   course_id=<context 原值>)`。
   已有 turn 直接返回原 stage；绝不可自建 turn、session 或 concept ID。
   若课程知识包可用题不足，只用自然语言说明需要补充已审核题并停在审核门。
2. `TEACH`：只讲返回的 `core_point` 所对应**一个**概念。建议 150–400 字，
   用“是什么→直觉→关键关系/公式→一个小例子”。公式、数值只依据包内容；
   需要确定性计算时先用工具。仅 `visual_needed=true` 时调用视觉 renderer；
   renderer 失败继续文字教学。讲后只出一道题。
3. `tutor_next_question` 返回 `pending_question`；先核验 stem、所有 options，
   然后在 TUI 正文完整显示题干、全部选项、作答提示；**本轮显示完成后**
   调 `tutor_mark_presented(session_id,question_id,rendered_text=<本轮已显示的完整题面>)`，
   工具成功后，**最终回复再次完整写出同一道题的题干、全部选项及作答提示**，
   再结束本轮等待用户。Z Code TUI 可能隐藏工具调用轮次中的正文，所以只在
   `tutor_mark_presented` 之前的中间消息显示题目、最终只说“已标记”会让用户看不到题。
   不要把题目只放在本轮最终消息中而漏调 mark。
   不可在题目尚未显示时预先标记。工具会校验题干与每个选项均在 rendered_text 中。
   未显示完整题面或工具报错时停止，不得判卷。正文不主动展示内部 ID、
   tool JSON、参考答案或 explanation。
   特别是 `tutor_advance` 返回的新概念还没有题目；**必须先取得
   `tutor_next_question` 的实际 pending，才可以写任何新题干或选项**。
   试图标记非 pending 题、工具拒绝后再换题，都属于用户可见的失败。
4. 用户真实作答时，确认题目已 mark_presented 后只调用 `tutor_submit_answer` 一次，使用 pending 的原
   question_id 和原始回答。工具原子判题、记分并返回 stage/反馈/mastery；
   不要另调 `evaluate_answer` 或 `record_result`，不要根据文字自行判分。
5. `correct`：用返回的简短 feedback 说明为什么正确和掌握了什么。
   `stage=CHECK` 表示阈值未达，继续同 concept 的**另一题**；
   `stage=ADVANCE` 才调用 `tutor_advance`。不可“答对一次就过”。
6. `incorrect`：`stage=REMEDIATE` 时先从当次 TutorTurn 的
   misconception/correction 只围绕这个错误点补讲；若没有可确定映射，
   只依据包内容解释错误，明确不臆测误区。**补讲完整显示后**再调用
   `tutor_finish_remediation` 将 stage 置为 RECHECK，然后 `tutor_next_question`
   取一题不同的 reviewed/canonical 题进入 RECHECK。题库不足时停止并提示审核，
   不得重问原题或模型临时编题。
7. 连续第二次错误进入 `FALLBACK`：停止针对性补讲循环，按工具返回的
   降难度与 prerequisite 提示处理。不可自行选下一章。真正 ADVANCE 必须以
   `tutor_advance` 的 package core_points 顺序和 curriculum DAG 结果为准；
   blocked/warning 不能当作成功晋级。
8. 新 Z Code session、resume 或 context rollover 后，先用本课程 topic
   调 `resume_learning` / `tutor_resume_turn` 读取 teach-mcp 的 stage 与 pending。
   CHECK/RECHECK 已有 pending 就原题重显，绝不再调用出题；若已
   question_presented，只重显题面，不重复 mark/record。REMEDIATE 继续补讲，
   ADVANCE 继续确定性选路。Firefly rollover_snapshot 仅供引用，不能覆盖
   teach-mcp 真值；旧 result.json 不能作为 interactive pending 真值。
9. 学生正文保持自然教学语言。不要主动输出 learner_id、course_id、topic_id、
   concept_id、session_id、stage 枚举、pipeline_id、draft_id、question_id、
   MCP payload、协议标记或 tool JSON；这些仅供工具调用和调试。审核阻塞
   只说明「下一知识点缺少已审核的练习题，需要补充后继续」，不列内部状态、
   技术尝试、题库 ID 或学习者标识。工具返回 QUESTION_BANK_INSUFFICIENT /
   QUESTION_BANK_EXHAUSTED 后立即停止，不改试其他 concept、不扫描 draft、
   不输出 debug 报告或工具名。

## 5. 编排状态（仅供理解；不得物化成第二份状态）

`RESOURCE_NEW → PROCESSING → WAITING_REVIEW → COURSE_READY → LEARNING_ACTIVE ⇄ RESUME`

推荐判断：无 binding=RESOURCE_NEW；有 processing 标记=PROCESSING；
有 pipeline_id 无 curriculum_id=WAITING_REVIEW；有 curriculum_id 无 open
session=COURSE_READY；有 open session=RESUME。
mastery 等学习事实永远只来自 teach-mcp。

## 6. 收尾

- 需要更新 binding 时只写 teach-mcp 返回过的 opaque id（保留其余原键）。
- 本协议的可恢复性依赖三件事：context（Firefly 每次 launch 重生成）+
  binding（opaque id）+ teach-mcp 数据库。三者之外不得有隐藏状态——
  Z Code 会话被删除后，从 Firefly 重新 launch 即可完整恢复学习。
