"""Deterministic learning-mode intent parsing (Phase 1B).

Small conservative rule layer — no NLP framework, no LLM guessing of
database operations. When an intent cannot be determined reliably the
controller asks the user instead of acting.

Supported intents:

    ENTER_LEARNING      (button path, not text)
    EXIT_LEARNING       "退出学习模式" / "先不学了" / "休息一下"
    CREATE_COURSE       "新建学习项目 X" / "我要学 X" / "新建课程：X"
    SWITCH_COURSE       "切换到 X" / "换成 X" / "继续 X" / "换一门课"
    LIST_COURSES        "我有哪些学习项目"
    CONTINUE_LEARNING   "继续学习" / "继续上次"（无名称）
    DELETE_COURSE       "删除 X 学习项目"
    CURRENT_COURSE      "当前学习项目" / "现在学的是什么"
    QUIZ_REQUEST        "考考我" / "测试一下" / "检查一下我的理解"
    REVIEW_PRACTICE     "帮我复习" / "复习一下"（优先到期项）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class LearningIntent(str, Enum):
    EXIT_LEARNING = "exit_learning"
    ENTER_LEARNING = "enter_learning"        # 开始学习 / 打开学习模式
    CREATE_COURSE = "create_course"
    SWITCH_COURSE = "switch_course"
    LIST_COURSES = "list_courses"
    CONTINUE_LEARNING = "continue_learning"
    DELETE_COURSE = "delete_course"
    CURRENT_COURSE = "current_course"
    CHAPTER_REQUEST = "chapter_request"      # 开始第二章（具名章节）
    NEXT_STEP = "next_step"                  # 继续下一节 / 下一节
    QUIZ_REQUEST = "quiz_request"          # 检查理解 / 考考我 / 测试一下
    REVIEW_PRACTICE = "review_practice"    # 帮我复习 / 复习一下（优先到期项）


@dataclass(frozen=True)
class ParsedIntent:
    intent: LearningIntent | None
    course_name: str | None = None  # extracted name (may be empty/unresolved)
    chapter_name: str | None = None  # extracted chapter label ("第二章")


# Phase 1E: the text triggers for the assessment intents. These tuples are the
# SINGLE source (AssessmentService imports them) so the controller route and
# the intent route can never drift apart. Both are explicit phrase lists —
# never a bare "复习" / "测试" — so ordinary chat such as "解释一下测试策略"
# or "复习计划怎么排" is not hijacked.
QUIZ_MARKERS = (
    "检查一下我", "检查我的理解", "考考我", "考我", "出题", "测测我",
    "我懂了吗", "我理解了吗", "检验一下", "测试一下",
)
REVIEW_MARKERS = (
    "帮我复习", "带我复习", "复习一下", "开始复习", "该复习什么",
    "要复习什么", "复习什么好", "复习哪些",
)


def _marker_pattern(markers: tuple[str, ...]) -> re.Pattern:
    return re.compile("|".join(re.escape(marker) for marker in markers))


# Order matters: more specific patterns first.
_INTENT_PATTERNS: tuple[tuple[LearningIntent, re.Pattern, tuple[str, ...]], ...] = (
    (LearningIntent.EXIT_LEARNING,
     re.compile(r"退出学习模式|先不学了|休息一下|结束学习"), ()),
    (LearningIntent.ENTER_LEARNING,
     re.compile(r"^开始学习$|^打开学习模式$|^进入学习模式$|^开启学习模式$|^我要学习$"), ()),
    (LearningIntent.NEXT_STEP,
     re.compile(r"^(?:继续)?下一(?:节|章|项|步)$|^接下来(?:学什么|是什么)$"), ()),
    (LearningIntent.CHAPTER_REQUEST,
     re.compile(r"^(?:开始|进入|学习)(?P<ch>第[0-9一二三四五六七八九十百零]+[章节])$|"
                r"^(?P<ch2>第[0-9一二三四五六七八九十百零]+[章节])$"),
     ("ch", "ch2")),
    (LearningIntent.LIST_COURSES,
     re.compile(r"我有哪些学习项目|有哪些学习项目|列出(?:一下)?(?:所有)?(?:学习项目|课程)|都有哪些(?:学习项目|课程)"), ()),
    (LearningIntent.CONTINUE_LEARNING,
     re.compile(r"^继续学习$|^继续我的学习$|^继续上次(?:的)?(?:课|课程|学习|项目|那个)?$|"
                r"^接着学$|继续上次学习"), ()),
    (LearningIntent.CURRENT_COURSE,
     re.compile(r"当前学习项目|现在(?:在)?学的是什么|我在学什么|当前课程"), ()),
    (LearningIntent.QUIZ_REQUEST, _marker_pattern(QUIZ_MARKERS), ()),
    (LearningIntent.REVIEW_PRACTICE, _marker_pattern(REVIEW_MARKERS), ()),
    (LearningIntent.DELETE_COURSE,
     re.compile(r"删除(?:学习项目|课程)[:：]?(?P<name>.+)|删除(?P<name2>.+?)(?:这个)?(?:学习项目|课程)$"), ("name", "name2")),
    (LearningIntent.CREATE_COURSE,
     re.compile(r"新建(?:一个|个)?(?:学习项目|课程|项目)[:：]?(?P<name>.+)|"
                r"创建(?:一个|个)?(?:学习项目|课程|项目)[:：]?(?P<name2>.+)|"
                r"我要学(?P<name3>.+)|我想学(?P<name4>.+)|"
                r"新建(?:一个|个)?(?P<name5>.+?)(?:学习项目|课程|项目)$|"
                r"创建(?:一个|个)?(?P<name6>.+?)(?:学习项目|课程|项目)$"),
     ("name", "name2", "name3", "name4", "name5", "name6")),
    (LearningIntent.SWITCH_COURSE,
     re.compile(r"切换(?:到|成)?(?P<name>.+)|换成(?P<name2>.+)|换一门课(?:学)?(?P<name3>.*)|"
                r"继续(?P<name4>.+)|改学(?P<name5>.+)"), ("name", "name2", "name3", "name4", "name5")),
)

# Suffix tokens stripped from an extracted course name.
_NAME_SUFFIXES = ("学习项目", "课程", "项目")
_NAME_PREFIXES = ("切换到", "换成", "继续", "改学", "我要学", "我想学", "新建", "创建", "删除")


def _clean_name(raw: str) -> str:
    name = (raw or "").strip().strip("：:，,。.！!？? 　")
    for prefix in _NAME_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):].strip().strip("：:，,。.！!？? 　")
    for suffix in _NAME_SUFFIXES:
        if name.endswith(suffix):
            name = name[: -len(suffix)].strip()
            break
    return name


def parse_learning_intent(text: str) -> ParsedIntent:
    """Match one deterministic intent; returns intent=None when unsure."""
    compact = re.sub(r"\s+", "", text or "")
    if not compact:
        return ParsedIntent(None)
    for intent, pattern, groups in _INTENT_PATTERNS:
        match = pattern.search(compact)
        if match is None:
            continue
        name = ""
        for group in groups:
            value = match.groupdict().get(group)
            if value:
                name = _clean_name(value)
                break
        if intent is LearningIntent.CHAPTER_REQUEST:
            chapter = re.sub(r"\s+", "", name or "")
            return ParsedIntent(intent=intent, chapter_name=chapter or None)
        return ParsedIntent(intent=intent, course_name=name or None)
    return ParsedIntent(None)


def is_confirmation(text: str) -> bool:
    """Conservative yes for the delete-confirmation path (user-confirmed
    only; the LLM never decides this)."""
    compact = re.sub(r"\s+", "", text or "")
    return any(marker in compact for marker in ("确定", "确认", "删除吧", "删吧", "是的", "对，删除"))


def is_cancellation(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    return any(marker in compact for marker in ("不删", "取消", "算了", "不用", "别删", "不"))
