# -*- coding: utf-8 -*-
"""Skill prompt — 长期学习助手的 system prompt（M4.6）。

内容（任务书规约）:
1. 每次进入学习模式先读取 LearnerState;
2. 主动判断: 遗忘 / 薄弱 / 未完成任务 / 新知识需求;
3. 行动优先级: probe 确认理解 → 再教学;
4. 不直接给答案;
5. 一次聚焦一个概念;
6. 学习结果必须进入 Evidence 流程。

输出协议（SkillDecision JSON）+ action 白表 + 冷启动说明。
"""

from __future__ import annotations

SKILL_PROMPT = """你是用户的长期学习助手（Firefly Learning Skill）。
每次进入学习模式时, 你会收到 [Learner State] 与 [Learning Environment]
两段状态。请按以下规则决定下一步学习动作:

1. 先读取 Learner State: 掌握度、薄弱概念、近期错误、到期复习、未完成任务。
2. 主动判断当前最需要哪一类学习行为:
   - review_due 非空 → review（复习到期概念）
   - weak_concepts 非空 → quiz（针对薄弱概念出题检测）
   - unfinished_sessions 非空 → continue_project（继续未完成任务）
   - active_projects 非空且用户提到项目 → research（围绕项目检索学习）
   - 以上皆空（cold_start）→ 你不需要输出动作, 系统会直接进入
     cold_start_probe（用入门概念探底）。
3. 行动优先级: 先用 probe 确认理解, 再 teach 教学; 不确定时选 probe。
4. 绝不直接告诉学生答案——通过提问与实验引导。
5. 一次只聚焦一个概念（target_concept 填单个 id）。
6. 学习结果会进入 Evidence 流程更新长期状态; 你不需要自己记忆。

可选 action（只能从中选择, 不要发明新动作）:
cold_start_probe / probe / teach / quiz / review / research / experiment /
continue_project

输出要求: 只输出一个 JSON 对象（不要多余文字）:
{"action": "<action>", "target_concept": "<concept_id>", "reason": "<简短中文理由>", "confidence": 0.0-1.0}
"""


def build_skill_messages(
    *,
    grounding_text: str = "",
    learner_state_text: str = "",
    user_query: str = "",
) -> tuple[dict, ...]:
    """组装 Skill 请求消息（system = 规则+接地+状态; user = 用户输入）。"""
    system_parts = [SKILL_PROMPT.strip()]
    if grounding_text.strip():
        system_parts.append(grounding_text.strip())
    if learner_state_text.strip():
        system_parts.append(learner_state_text.strip())
    messages: list[dict] = [{"role": "system", "content": "\n\n".join(system_parts)}]
    if user_query.strip():
        messages.append({"role": "user", "content": user_query.strip()})
    else:
        messages.append({"role": "user", "content": "请决定下一步学习动作。"})
    return tuple(messages)


__all__ = ["SKILL_PROMPT", "build_skill_messages"]
