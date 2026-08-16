"""Deterministic Agent Router (Phase 9A).

Pure-rule recommendation engine. The same TaskRequest in always produces the
same ordered AgentRecommendation list out. No LLM, no network, no subprocess,
no Qt, no side effects. The router only decides *which* agent to recommend and
*how* that agent should be reached (managed Short Talk vs native surface vs
unavailable); it never launches anything.

Rule priority (highest first):

    1. explicit requested_agent (field, or "use/let/open <agent>" phrasing)
    2. vision intent          -> no verified managed route (honest unavailable)
    3. write / coding         -> Codex, OPEN_NATIVE (managed Short Talk off)
    4. review                 -> Claude, SHORT_TALK
    5. explain-error / debug  -> Claude (analysis direction), SHORT_TALK
    6. analysis / explain     -> Claude, SHORT_TALK
    7. generic chat / unknown -> Claude, SHORT_TALK (safe fallback)

Write vs explain disambiguation ("解释如何修改" is a question, not a command):

    - how/why markers next to a write verb   -> explanation (Claude)
    - sequence markers + analysis + write    -> multi-step (Claude then Codex)
    - write verb with nothing else           -> coding (Codex)
    - analysis + write with no hint          -> conservative analysis (Claude)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .routing_models import (
    DEFAULT_CAPABILITY_REGISTRY,
    AgentCapability,
    AgentRecommendation,
    CapabilityRegistry,
    Confidence,
    HandoffMode,
    ReasonCode,
    TaskIntent,
    TaskRequest,
)

_KNOWN_AGENTS = frozenset({"claude", "codex", "chatgpt"})

_CJK_RE = re.compile(r"[一-鿿]")
_TOKEN_RE = re.compile(r"[a-z0-9']+")


def _is_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text))


_WHY_MARKERS = (
    "为什么",
    "为啥",
    "为何",
    "怎么回事",
    "什么原因",
    "哪里出错",
    "why",
    "cause",
    "root cause",
    "reason",
)
_HOW_MARKERS = (
    "如何",
    "怎样",
    "怎么",
    "how to",
    "how do",
    "how can",
    "how does",
    "steps to",
)
_ANALYSIS_MARKERS = (
    "解释",
    "说明",
    "讲讲",
    "介绍一下",
    "介绍",
    "分析",
    "总结",
    "比较",
    "评估",
    "判断",
    "读一下",
    "阅读",
    "理解",
    "概述",
    "看看",
    "看一下",
    "帮我看",
    "描述",
    "describe",
    "explain",
    "summarize",
    "analyze",
    "compare",
    "assess",
    "evaluate",
    "read",
    "understand",
    "overview",
)
_REVIEW_MARKERS = (
    "检查",
    "审查",
    "评审",
    "审计",
    "复核",
    "看看这段",
    "有没有问题",
    "有问题吗",
    "review",
    "check",
    "inspect",
    "audit",
)
_ERROR_MARKERS = (
    "报错",
    "错误",
    "出错",
    "异常",
    "崩溃",
    "挂掉",
    "报错信息",
    "error",
    "exception",
    "crash",
    "failure",
    "failed",
    "traceback",
    "stack trace",
    "bug",
)
_WRITE_MARKERS = (
    "修改",
    "实现",
    "重构",
    "新增",
    "删除",
    "编辑",
    "改代码",
    "写代码",
    "生成代码",
    "修复",
    "修一下",
    "修好",
    "改一下",
    "加上",
    "添加",
    "加个",
    "加一个",
    "改进",
    "优化",
    "重写",
    "更新",
    "替换",
    "编写",
    "写个",
    "写一个",
    "写测试",
    "帮我改",
    "把这个",
    "解决",
    "搞定",
    "implement",
    "modify",
    "refactor",
    "rewrite",
    "change",
    "update",
    "edit",
    "fix",
    "add",
    "remove",
    "delete",
    "write",
    "create",
    "build",
    "develop",
    "repair",
    "improve",
    "optimize",
    "generate",
)
_VISION_MARKERS = (
    "图片",
    "截图",
    "照片",
    "图像",
    "视觉",
    "看这张图",
    "这个截图",
    "这张图",
    "效果图",
    "设计稿",
    "screenshot",
    "screen shot",
    "image",
    "picture",
    "photo",
    "vision",
    "ocr",
)
_SEQUENCE_MARKERS = (
    "先",
    "然后",
    "之后",
    "接着",
    "最后",
    "首先",
    "再",
    "先做",
    "first",
    "then",
    "after that",
    "next",
    "finally",
    "afterwards",
)
_CHAT_MARKERS = (
    "你好",
    "您好",
    "嗨",
    "在吗",
    "哈喽",
    "早上好",
    "晚上好",
    "hi",
    "hello",
    "hey",
    "good morning",
    "good evening",
)

_AGENT_ALIASES = {
    "claude": ("claude",),
    "codex": ("codex",),
    "chatgpt": ("chatgpt",),
}
_DIRECT_PREFIXES = (
    "用",
    "让",
    "交给",
    "使用",
    "请",
    "请用",
    "帮我用",
    "把代码交给",
    "use",
    "ask",
    "tell",
)
_OPEN_PREFIXES = ("打开", "启动", "开启", "帮我打开", "帮我启动", "open", "launch", "start")


def _build_requested_triggers() -> dict[str, tuple[tuple[str, bool], ...]]:
    """Per-agent trigger phrases matched against whitespace-stripped text.

    Each entry is (phrase, is_open). ``is_open`` marks the "open/launch the
    agent" phrasing, which routes to OPEN_NATIVE instead of a task.
    """
    triggers: dict[str, list[tuple[str, bool]]] = {}
    for agent, aliases in _AGENT_ALIASES.items():
        for alias in aliases:
            for prefix in _DIRECT_PREFIXES:
                triggers.setdefault(agent, []).append((f"{prefix}{alias}", False))
            for prefix in _OPEN_PREFIXES:
                triggers.setdefault(agent, []).append((f"{prefix}{alias}", True))
            triggers.setdefault(agent, []).append((f"{alias}来", False))
            triggers.setdefault(agent, []).append((f"{alias}帮我", False))
            triggers.setdefault(agent, []).append((f"给我{alias}", False))
    return {agent: tuple(items) for agent, items in triggers.items()}


_REQUESTED_TRIGGERS = _build_requested_triggers()


@dataclass(frozen=True, slots=True)
class _Detected:
    write: bool = False
    review: bool = False
    error: bool = False
    why: bool = False
    how: bool = False
    analysis: bool = False
    vision: bool = False
    sequence: bool = False
    chat: bool = False
    requested: tuple[str, bool] | None = None  # (agent_id, is_open)


class AgentRouter:
    """Deterministic, side-effect-free task -> recommendation engine."""

    def __init__(self, registry: CapabilityRegistry | None = None) -> None:
        self._registry = registry if registry is not None else DEFAULT_CAPABILITY_REGISTRY

    # -- public -----------------------------------------------------------

    def recommend(self, request: TaskRequest) -> list[AgentRecommendation]:
        """Return a sorted (descending score) recommendation list. Never empty."""
        text = (request.text or "").strip()
        flags = self._detect(text)
        explicit = self._explicit_agent(request.requested_agent)
        if explicit is not None:
            is_open = flags.requested[1] if flags.requested is not None else False
            return self._route_requested(request, explicit, is_open, flags)
        if flags.requested is not None:
            agent, is_open = flags.requested
            return self._route_requested(request, agent, is_open, flags)
        return self._route_by_task(request, flags)

    # -- detection --------------------------------------------------------

    @staticmethod
    def _explicit_agent(value: str | None) -> str | None:
        if value is None:
            return None
        agent = str(value).strip().lower()
        return agent if agent in _KNOWN_AGENTS else None

    def _detect(self, text: str) -> _Detected:
        lower = (text or "").lower()
        compact = re.sub(r"\s+", "", lower)
        tokens = set(_TOKEN_RE.findall(lower))

        def has(markers: tuple[str, ...]) -> bool:
            for marker in markers:
                if _is_cjk(marker):
                    if marker in compact:
                        return True
                elif " " in marker:
                    if marker.replace(" ", "") in compact:
                        return True
                elif marker in tokens:
                    return True
            return False

        return _Detected(
            write=has(_WRITE_MARKERS),
            review=has(_REVIEW_MARKERS),
            error=has(_ERROR_MARKERS),
            why=has(_WHY_MARKERS),
            how=has(_HOW_MARKERS),
            analysis=has(_ANALYSIS_MARKERS),
            vision=has(_VISION_MARKERS),
            sequence=has(_SEQUENCE_MARKERS),
            chat=has(_CHAT_MARKERS),
            requested=self._detect_requested(compact),
        )

    @staticmethod
    def _detect_requested(compact: str) -> tuple[str, bool] | None:
        for agent in _KNOWN_AGENTS:
            for phrase, is_open in _REQUESTED_TRIGGERS[agent]:
                if phrase in compact:
                    return (agent, is_open)
        return None

    # -- requested-agent routing -------------------------------------------

    def _route_requested(
        self,
        request: TaskRequest,
        agent: str,
        is_open: bool,
        flags: _Detected,
    ) -> list[AgentRecommendation]:
        profile = self._registry.profile(agent)
        intent = self._text_intent(flags)
        if is_open:
            if profile is not None and AgentCapability.NATIVE_LAUNCH in profile.capabilities:
                return [
                    self._rec(
                        agent, 100, ReasonCode.OPEN_REQUESTED, Confidence.HIGH,
                        intent, HandoffMode.OPEN_NATIVE,
                    )
                ]
            return [
                self._rec(
                    agent, 50, ReasonCode.BACKEND_UNAVAILABLE, Confidence.LOW,
                    intent, HandoffMode.UNAVAILABLE,
                )
            ]
        if profile is None or not profile.available:
            # ChatGPT: dock entry exists but Firefly has no task-execution mode.
            return [
                self._rec(
                    agent, 50, ReasonCode.BACKEND_UNAVAILABLE, Confidence.MEDIUM,
                    intent, HandoffMode.UNAVAILABLE,
                )
            ]
        write_task = (
            flags.write
            or request.requires_write is True
            or request.intent in (TaskIntent.CODE, TaskIntent.DEBUG)
        )
        if agent == "claude":
            if write_task:
                # Managed Short Talk is read-only; honor the request natively.
                return [
                    self._rec(
                        "claude", 100, ReasonCode.NATIVE_SURFACE_REQUIRED,
                        Confidence.MEDIUM, intent, HandoffMode.OPEN_NATIVE,
                        requires_confirmation=True,
                    )
                ]
            return [
                self._rec(
                    "claude", 100, ReasonCode.USER_REQUESTED_AGENT, Confidence.HIGH,
                    intent, HandoffMode.SHORT_TALK,
                )
            ]
        # Codex and other available agents have no managed Short Talk yet.
        return [
            self._rec(
                agent, 100, ReasonCode.USER_REQUESTED_AGENT, Confidence.HIGH,
                intent, HandoffMode.OPEN_NATIVE, requires_confirmation=write_task,
            )
        ]

    # -- text routing ------------------------------------------------------

    def _route_by_task(
        self, request: TaskRequest, flags: _Detected
    ) -> list[AgentRecommendation]:
        text = (request.text or "").strip()
        if flags.vision:
            return [
                self._rec(
                    "claude", 40, ReasonCode.VISION_MANAGED_UNAVAILABLE,
                    Confidence.LOW, TaskIntent.VISION, HandoffMode.UNAVAILABLE,
                )
            ]

        explicit_write = (
            request.requires_write is True
            or request.intent in (TaskIntent.CODE, TaskIntent.DEBUG)
        )
        has_write = flags.write or explicit_write
        read_indicators = flags.review or flags.why or flags.how or flags.analysis

        if has_write:
            if read_indicators and not explicit_write:
                if flags.how or flags.why:
                    # "how/why to change X" is a question, not a command.
                    return [
                        self._rec(
                            "claude", 82, ReasonCode.BEST_FOR_ANALYSIS,
                            Confidence.HIGH, TaskIntent.EXPLAIN, HandoffMode.SHORT_TALK,
                        )
                    ]
                if flags.sequence:
                    # Analysis first, then coding. Never auto-run a workflow.
                    return [
                        self._rec(
                            "claude", 85, ReasonCode.ANALYSIS_FIRST,
                            Confidence.HIGH, TaskIntent.ANALYZE, HandoffMode.SHORT_TALK,
                            multi_step_candidate=True,
                        ),
                        self._rec(
                            "codex", 80, ReasonCode.CODING_CAPABILITY,
                            Confidence.MEDIUM, TaskIntent.CODE, HandoffMode.OPEN_NATIVE,
                            requires_confirmation=True,
                        ),
                    ]
                # Ambiguous write+read: conservative, read-safe analysis.
                return [
                    self._rec(
                        "claude", 75, ReasonCode.AMBIGUOUS_TASK,
                        Confidence.MEDIUM, TaskIntent.ANALYZE, HandoffMode.SHORT_TALK,
                    )
                ]
            intent = TaskIntent.DEBUG if flags.error else TaskIntent.CODE
            return [
                self._rec(
                    "codex", 90, ReasonCode.BEST_FOR_CODING,
                    Confidence.HIGH, intent, HandoffMode.OPEN_NATIVE,
                    requires_confirmation=True,
                )
            ]

        if flags.review:
            return [
                self._rec(
                    "claude", 82, ReasonCode.BEST_FOR_REVIEW,
                    Confidence.HIGH, TaskIntent.REVIEW, HandoffMode.SHORT_TALK,
                )
            ]
        if flags.error and read_indicators:
            return [
                self._rec(
                    "claude", 82, ReasonCode.BEST_FOR_ANALYSIS,
                    Confidence.HIGH, TaskIntent.DEBUG, HandoffMode.SHORT_TALK,
                )
            ]
        if read_indicators:
            intent = TaskIntent.EXPLAIN if (flags.why or flags.how) else TaskIntent.ANALYZE
            return [
                self._rec(
                    "claude", 80, ReasonCode.BEST_FOR_ANALYSIS,
                    Confidence.HIGH, intent, HandoffMode.SHORT_TALK,
                )
            ]
        if flags.chat:
            return [
                self._rec(
                    "claude", 70, ReasonCode.BEST_FOR_ANALYSIS,
                    Confidence.MEDIUM, TaskIntent.CHAT, HandoffMode.SHORT_TALK,
                )
            ]
        return [
            self._rec(
                "claude", 60, ReasonCode.UNKNOWN_TASK,
                Confidence.LOW, TaskIntent.UNKNOWN, HandoffMode.SHORT_TALK,
            )
        ]

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _rec(
        agent_id: str,
        score: int,
        reason: ReasonCode,
        confidence: Confidence,
        intent: TaskIntent,
        handoff: HandoffMode,
        *,
        requires_confirmation: bool = False,
        multi_step_candidate: bool = False,
    ) -> AgentRecommendation:
        return AgentRecommendation(
            agent_id=agent_id,
            score=score,
            reason_code=reason,
            confidence=confidence,
            intent=intent,
            handoff_mode=handoff,
            requires_confirmation=requires_confirmation,
            multi_step_candidate=multi_step_candidate,
        )

    @staticmethod
    def _text_intent(flags: _Detected) -> TaskIntent:
        if flags.vision:
            return TaskIntent.VISION
        if flags.write:
            if flags.error and not (flags.why or flags.how or flags.analysis or flags.review):
                return TaskIntent.DEBUG
            return TaskIntent.CODE
        if flags.error and (flags.why or flags.analysis or flags.how):
            return TaskIntent.DEBUG
        if flags.review:
            return TaskIntent.REVIEW
        if flags.why or flags.how:
            return TaskIntent.EXPLAIN
        if flags.analysis:
            return TaskIntent.ANALYZE
        if flags.chat:
            return TaskIntent.CHAT
        return TaskIntent.UNKNOWN
