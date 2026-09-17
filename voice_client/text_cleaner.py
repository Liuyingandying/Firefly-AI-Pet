# -*- coding: utf-8 -*-
"""Voice Text Sanitizer: 角色回复 → 干净的 TTS 输入 (v1.3.1)。

管线 (与 docs/Voice_v131_Bugfix.md 一致):
  Agent 原始文本
    → 保护代码块 / 行内代码 (含星号/下划线的代码永不被碰)
    → 识别并删除角色动作块 (含CJK的 （…）/(…) 、整行 *她…* 斜体动作、(smiles) 类英文小动作)
    → 清除动作删除产生的空 Markdown 残骸 (**** 等)
    → 清除不应朗读的 Markdown 展示符号 (**bold**/*italic*/~~删除线__/_斜体_/行首#、-、>)
    → 全角括号内纯 ASCII 内容转为逗号停顿 (RVC（Retrieval-based…）→ RVC，Retrieval-based…，)
    → 孤儿星号策略: 空格包围的 * 视为乘号读作"乘", 其余残余 * 与 _ 删除
    → 还原代码 → 规范空白
显示层仍保留完整原文 (display_text != voice_text)。
"""

from __future__ import annotations

import re

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_ANY_PAREN = re.compile(r"（[^（）]*）|\([^()]*\)")
_CODE_FENCE = re.compile(r"```.*?```", re.S)
_CODE_INLINE = re.compile(r"`[^`\n]+`")
_EN_ACTION = re.compile(
    r"\*?\((?:smiles?|laughs?|giggles?|sighs?|nods?|shakes head|blushes?|winks?|"
    r"pauses?|leans (?:in|back)|tilts head)\)\*?",
    re.I,
)
# 整行单星斜体且以人称代词开头的叙述动作: *她轻轻叹了口气。*
_LINE_ITALIC_ACTION = re.compile(
    r"^[ \t]*\*([^*\n]+)\*[ \t]*$", re.M
)
_PRONOUN = re.compile(r"^[她他它们]|\A我")
_BOLD = re.compile(r"\*\*([^*\n]+)\*\*")
_ITALIC = re.compile(r"\*(?!\s)([^*\n]+?)(?<!\s)\*")
_STRIKE = re.compile(r"~~([^~\n]+)~~")
_BOLD_UNDER = re.compile(r"__([^_\n]+)__")
_ITALIC_UNDER = re.compile(r"(?<!_)_(?!\s)([^_\n]+?)(?<!\s)_(?!_)")
_MULTI_STAR = re.compile(r"\*{2,}")
_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+", re.M)
_LIST_MARK = re.compile(r"^[ \t]{0,3}[-*+][ \t]+", re.M)
_QUOTE = re.compile(r"^[ \t]{0,3}>+[ \t]?", re.M)
_FW_PAREN_ASCII = re.compile(r"（([A-Za-z0-9][A-Za-z0-9 .,;:…&#'/\-%+()（）]*)）")
_SPACED_STAR = re.compile(r"(?<=[\d）)A-Za-z]) \* (?=[\dA-Za-z(（])")
_PLACEHOLDER = "\x00{}\x00"


def remove_action_text(text: str) -> str:
    """清洗为仅含对白/正文的 TTS 文本; 输入为空返回空串。"""
    if not text:
        return ""

    # ---- 1) 保护代码 (代码内的括号/星号/下划线一律不动) ----
    protected: list[str] = []

    def _stash(match: re.Match) -> str:
        protected.append(match.group(0))
        return _PLACEHOLDER.format(len(protected) - 1)

    cleaned = _CODE_FENCE.sub(_stash, text)
    cleaned = _CODE_INLINE.sub(_stash, cleaned)

    # ---- 2) 删除角色动作块 ----
    prev = None
    while prev != cleaned:
        prev = cleaned
        cleaned = _ANY_PAREN.sub(
            lambda m: "" if _CJK.search(m.group(0)) else m.group(0), cleaned
        )

    def _drop_italic_action_line(match: re.Match) -> str:
        return "" if _CJK.search(match.group(1)) and _PRONOUN.search(match.group(1)) else match.group(0)

    cleaned = _LINE_ITALIC_ACTION.sub(_drop_italic_action_line, cleaned)
    cleaned = _EN_ACTION.sub("", cleaned)

    # ---- 3) 清除动作删除产生的空 Markdown 残骸 (**（…）** → ****) ----
    cleaned = _MULTI_STAR.sub("", cleaned)

    # ---- 4) 清除 Markdown 展示符号 ----
    cleaned = _STRIKE.sub(r"\1", cleaned)
    cleaned = _BOLD.sub(r"\1", cleaned)
    cleaned = _ITALIC.sub(r"\1", cleaned)
    cleaned = _BOLD_UNDER.sub(r"\1", cleaned)
    cleaned = _ITALIC_UNDER.sub(r"\1", cleaned)
    cleaned = _HEADING.sub("", cleaned)
    cleaned = _LIST_MARK.sub("", cleaned)
    cleaned = _QUOTE.sub("", cleaned)
    # 全角括号内纯 ASCII (术语/缩写) → 逗号停顿, 不再被当作动作删除
    cleaned = _FW_PAREN_ASCII.sub(lambda m: f"，{m.group(1).strip()}，", cleaned)

    # ---- 5) 孤儿星号/下划线: 数学乘号读作"乘", 其余残余删除 ----
    cleaned = _SPACED_STAR.sub(" 乘 ", cleaned)
    cleaned = cleaned.replace("*", "").replace("_", "")

    # ---- 6) 还原代码 ----
    cleaned = re.sub(
        _PLACEHOLDER.format(r"(\d+)"),
        lambda m: protected[int(m.group(1))],
        cleaned,
    )

    # ---- 7) 规范空白 ----
    lines = [ln.rstrip() for ln in cleaned.splitlines()]
    return "\n".join(ln for ln in lines if ln.strip()).strip()
