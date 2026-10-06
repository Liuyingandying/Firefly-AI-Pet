"""Optional, turn-local pacing hints. No reply renderer, model or durable state."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
import re
from typing import Mapping, Sequence


PACING_ENV = "FIREFLY_CONVERSATION_PACING"
RECENT_MESSAGES = 12  # At most six existing user/assistant pairs; no new history.


class PacingMode(str, Enum):
    QUICK = "QUICK"
    NORMAL = "NORMAL"
    LINGER = "LINGER"
    DEEP = "DEEP"


_HINTS = {
    PacingMode.QUICK: "保持轻快、短促，顺着当前互动即可，不必展开；保持原本说话方式，不必以问题结尾。",
    PacingMode.NORMAL: "自然回应当前话题，不必急着结束或刻意延长；保持原本说话方式，不必以问题结尾。",
    PacingMode.LINGER: "这一轮可以多停留一会儿，适当表达自己的感受，顺着气氛继续；保持原本说话方式，不急着提问或分析。",
    PacingMode.DEEP: "可以完整回应用户想深入聊的内容；保持原本说话方式，不做咨询报告式分析，不必以问题结尾。",
}


@dataclass(frozen=True)
class PacingDecision:
    mode: PacingMode
    no_questions: bool = False
    no_analysis: bool = False

    def to_prompt(self) -> str:
        hint = f"[Conversation pacing]\n{self.mode.value}:\n{_HINTS[self.mode]}"
        if self.no_questions:
            hint += "不追问原因。"
        if self.no_analysis:
            hint += "不分析用户心理。"
        return hint


# These rules only select expression space, never facts, actions or user diagnoses.
_EXPLICIT = re.compile(
    r"(?P<quick>简单说|简短点|短一点|一句话|别展开|不用展开|不用详细解释)"
    r"|(?P<deep>认真聊聊?|详细解释|详细讲解|仔细解释|深入聊|展开讲)"
    r"|(?P<linger>多陪我聊(?:一?会儿)?|聊久一点)"
)
_CLOSED = re.compile(r"不想说原因|不想说|别问了|不要问|别追问|没什么想说|就陪我待")
_NO_ANALYSIS = re.compile(r"别分析|不要分析|不用分析")
_TECH_TOPIC = re.compile(r"公式|状态空间|推导|电路|时间常数|fpga|uart|复位|代码|编译|算法|python|矩阵", re.I)
_QUESTION = re.compile(r"[?？]|为什么|怎么|如何|什么|解释|推导|证明|帮我看")
_COMPLEX = re.compile(r"推导|怎么推|为什么这样推|证明|分步|逐步|比较.{0,60}(?:差异|区别)")
_SIMPLE = re.compile(r"是什么|什么意思|有什么用|什么作用")
_STRONG = re.compile(r"撑不住|崩溃|做什么都提不起劲")
_MODERATE = re.compile(r"真的(?:挺|很|有点)?累|很难过|特别难受|挺累的")
_STAY = re.compile(r"陪我|陪陪我|待会儿|待一会儿|赖着你|想你|想念你")
_QUICK_ACTS = frozenset({"亲亲", "嗯嗯", "哈哈", "嘿嘿", "嘻嘻", "好呀", "好哒", "贴贴"})


def _request_text(text: str) -> str:
    # Quoted dialogue is not the user's present pacing preference.
    return re.sub(r"""“[^”]*”|「[^」]*」|‘[^’]*’|"[^"]*"|'[^']*'""", "", text)


def _positive_matches(pattern: re.Pattern, text: str):
    return [match for match in pattern.finditer(text)
            if not re.search(r"(?:不想|不要|不必|不用|别|不)$", text[:match.start()].rstrip())]


def _explicit_mode(text: str) -> PacingMode | None:
    matches = _positive_matches(_EXPLICIT, text)
    if not matches:
        return None
    return PacingMode(matches[-1].lastgroup.upper())


def _short_exchange_streak(recent: Sequence[Mapping[str, str]]) -> bool:
    tail = recent[-6:]
    return len(tail) == 6 and all(
        turn.get("role") == ("user" if index % 2 == 0 else "assistant")
        and 0 < len(turn.get("content", "").strip()) <= 30
        and "\n" not in turn.get("content", "")
        for index, turn in enumerate(tail)
    )


def select_pacing(user_text: str, history: Sequence[Mapping[str, str]] = ()) -> PacingDecision:
    text = user_text.strip()
    intent = _request_text(text)
    recent = history[-RECENT_MESSAGES:]
    recent_user = _request_text(next((turn.get("content", "") for turn in reversed(recent)
                                     if turn.get("role") == "user"), ""))
    technical = bool(_TECH_TOPIC.search(text) and _QUESTION.search(text))
    closed = bool(_CLOSED.search(intent))
    no_analysis = bool(_NO_ANALYSIS.search(intent))
    # A new technical topic does not inherit an earlier emotional preference.
    if (not technical and not re.search(r"可以问|问我吧|我愿意说|换个话题|这个先结束", intent)):
        closed = closed or bool(_CLOSED.search(recent_user))
        no_analysis = no_analysis or bool(_NO_ANALYSIS.search(recent_user))
    explicit = _explicit_mode(intent)
    if explicit is not None:
        return PacingDecision(explicit, closed, no_analysis)
    if _positive_matches(_COMPLEX, intent):
        mode = PacingMode.DEEP
    elif technical:
        if len(text) > 100:
            mode = PacingMode.DEEP
        else:
            mode = PacingMode.QUICK if _SIMPLE.search(text) and len(text) <= 50 else PacingMode.NORMAL
    elif re.search(r"换个话题|这个先结束", intent):
        mode = PacingMode.NORMAL
    elif _STRONG.search(intent):
        mode = PacingMode.LINGER if no_analysis else PacingMode.DEEP
    elif _MODERATE.search(intent) or _positive_matches(_STAY, intent):
        mode = PacingMode.NORMAL if "想你" in intent and _short_exchange_streak(recent) else PacingMode.LINGER
    elif text.strip("。！？!?～~…，, ") in _QUICK_ACTS:
        mode = PacingMode.QUICK
    elif re.fullmatch(r"继续|接着说|然后呢|还有呢", text.strip("。！？!? ")):
        mode = _explicit_mode(recent_user) or PacingMode.NORMAL
    elif (_short_exchange_streak(recent) and len(text) <= 30 and not closed
          and not _QUESTION.search(intent)):
        mode = PacingMode.QUICK
    elif len(text) >= 80 and sum(len(t.get("content", "")) >= 80
                               for t in recent[-4:] if t.get("role") == "user") >= 2:
        mode = PacingMode.DEEP
    else:
        mode = PacingMode.NORMAL
    return PacingDecision(mode, closed, no_analysis)


def pacing_hint_for_turn(user_text: str, history: Sequence[Mapping[str, str]] = (),
                         current_task: str = "chat") -> str:
    if os.environ.get(PACING_ENV) != "1" or current_task != "chat":
        return ""
    return select_pacing(user_text, history).to_prompt()
