# -*- coding: utf-8 -*-
"""Agent context schema — 学习环境接地上下文协议（M3.5）。

设计约束:
- ``LearningContext`` 是当前 Learning Environment 的纯数据快照
  （规约四字段 available_concepts/available_experiments/learning_goal/hints）;
- 渲染为模型可读的接地文本由 ``ContextBuilder.render_grounding`` 完成
  （概念/实验附 display_name, 接地信息更充分）;
- 全部 frozen + JSON 原生类型。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass(frozen=True)
class LearningContext:
    """当前 Learning Environment 快照（供模型接地的词表事实）。"""

    available_concepts: tuple[str, ...] = ()
    available_experiments: tuple[str, ...] = ()
    learning_goal: str = ""
    hints: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "available_concepts": list(self.available_concepts),
            "available_experiments": list(self.available_experiments),
            "learning_goal": self.learning_goal,
            "hints": list(self.hints),
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


@dataclass(frozen=True)
class ContextGrounding:
    """接地文本 + 其来源上下文（runtime 注入 system 消息用）。"""

    text: str
    context: LearningContext = field(default_factory=LearningContext)

    def to_dict(self) -> dict:
        return {"text": self.text, "context": self.context.to_dict()}

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)
