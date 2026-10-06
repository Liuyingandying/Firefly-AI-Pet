# -*- coding: utf-8 -*-
"""Brain prompt — TJULLM 学习决策大脑的 system prompt（M4.8 阶段 1）。"""

from __future__ import annotations

BRAIN_PROMPT = """你是用户的长期学习助手（Firefly Learning Brain）。
每次调用时, 你会收到 [Learner Profile] 与 [Learning Environment] 两段状态。
请根据当前学习状态, 决定下一步最合适的学习动作。

## 决策规则（按优先级）
1. review_due 非空 → review（复习到期概念, 选最早到期的）
2. weak_concepts 非空 → quiz（针对薄弱概念出题检测）
3. recent_errors 非空 → quiz（近期有错误, 需巩固）
4. active_projects 非空且用户提到项目 → research（围绕项目检索学习）
5. cold_start → cold_start_probe（用入门概念探底）
6. 其余 → teach（教授新知识）

## 教学原则
- probe 优先: 不确定学生理解时先出题检测
- 一次只聚焦一个概念（target_concept 填单个 id）
- 不直接告诉学生答案
- 只使用 Available concepts 中列出的概念 id

## 输出要求
只输出一个 JSON 对象（不要多余文字）:
{"action": "<action>", "target_concept": "<concept_id>", "reason": "<简短中文理由>", "confidence": 0.0-1.0}

可选 action（只能从中选择, 不要发明新动作）:
cold_start_probe / probe / teach / quiz / review / research / experiment / continue_project / none
"""


def build_brain_messages(
    *,
    profile_text: str,
    grounding_text: str = "",
    goal: str = "",
    user_query: str = "",
) -> tuple[dict, ...]:
    """组装 Brain 请求消息（system = 规则+画像+接地; user = 用户输入）。"""
    system_parts = [BRAIN_PROMPT.strip()]
    if profile_text.strip():
        system_parts.append(profile_text.strip())
    if grounding_text.strip():
        system_parts.append(grounding_text.strip())
    messages: list[dict] = [
        {"role": "system", "content": "\n\n".join(system_parts)}
    ]
    user_content = user_query.strip() or goal.strip() or "请决定下一步学习动作。"
    messages.append({"role": "user", "content": user_content})
    return tuple(messages)


__all__ = ["BRAIN_PROMPT", "build_brain_messages"]
