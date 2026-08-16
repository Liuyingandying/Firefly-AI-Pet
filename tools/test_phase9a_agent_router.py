"""Phase 9A — Capability registry + deterministic Agent Router tests.

Covers the 30 required scenarios plus English-prompt coverage: explain/analyze/
summarize -> Claude; review -> Claude; code/refactor/feature/fix -> Codex;
"why does this error happen" -> Claude vs "fix this error" -> Codex; explicit
requested_agent precedence; ChatGPT never fabricates a managed backend; generic
chat -> Claude; unknown -> safe fallback; write confirmation; read analysis no
confirmation; handoff modes (SHORT_TALK vs OPEN_NATIVE vs UNAVAILABLE); vision
never fabricated; registry honesty/asymmetry; determinism; Qt-free; zero
subprocess; zero network; stable reason tokens; score ordering; multi-step
wording never auto-creates a workflow.

The router must be a pure function: no LLM, no process, no network, no Qt.
"""

from __future__ import annotations

import ast
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from core.agent_router import AgentRouter
from core.routing_models import (
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


def _forbidden_in_module(path: Path, names: tuple[str, ...]) -> list[str]:
    """Return forbidden identifiers present in real code (imports, Names,
    Attribute attrs). Comments and docstrings are ignored via AST parsing, so
    a docstring that merely *mentions* e.g. QWidgets does not count."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                head = alias.name.lstrip(".").split(".")[0]
                if head in names or any(head.startswith(n) for n in names):
                    hits.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                head = node.module.lstrip(".").split(".")[0]
                if head in names or any(head.startswith(n) for n in names):
                    hits.append(node.module)
        elif isinstance(node, ast.Name):
            if node.id in names:
                hits.append(node.id)
        elif isinstance(node, ast.Attribute):
            if node.attr in names:
                hits.append(node.attr)
    return sorted(set(hits))


_ROUTER_FILES = (PROJECT_DIR / "core" / "routing_models.py", PROJECT_DIR / "core" / "agent_router.py")


def _req(
    text: str,
    *,
    agent: str | None = None,
    intent: TaskIntent | None = None,
    requires_write: bool | None = None,
) -> TaskRequest:
    return TaskRequest(
        text=text,
        workspace=None,
        requested_agent=agent,
        intent=intent,
        requires_write=requires_write,
        prefer_low_cost=True,
    )


def _top(request: TaskRequest) -> AgentRecommendation:
    return AgentRouter().recommend(request)[0]


def _single(request: TaskRequest) -> AgentRecommendation:
    recs = AgentRouter().recommend(request)
    assert len(recs) == 1, f"expected one recommendation for {request.text!r}, got {len(recs)}"
    return recs[0]


# -- 1-4. analysis/explain/review -> Claude --------------------------------

def test_explain_to_claude() -> None:
    rec = _single(_req("解释这段函数"))
    assert rec.agent_id == "claude"
    assert rec.handoff_mode == HandoffMode.SHORT_TALK


def test_analyze_to_claude() -> None:
    rec = _single(_req("分析这段代码的性能"))
    assert rec.agent_id == "claude"
    assert rec.handoff_mode == HandoffMode.SHORT_TALK


def test_summarize_to_claude() -> None:
    rec = _single(_req("总结一下这个模块"))
    assert rec.agent_id == "claude"
    assert rec.intent in (TaskIntent.EXPLAIN, TaskIntent.ANALYZE)


def test_review_to_claude() -> None:
    rec = _single(_req("帮我 review 一下这段代码"))
    assert rec.agent_id == "claude"
    assert rec.intent == TaskIntent.REVIEW
    assert rec.reason_code == ReasonCode.BEST_FOR_REVIEW
    assert rec.handoff_mode == HandoffMode.SHORT_TALK


# -- 5-8. coding -> Codex --------------------------------------------------

def test_code_implementation_to_codex() -> None:
    rec = _single(_req("实现这个功能"))
    assert rec.agent_id == "codex"
    assert rec.intent == TaskIntent.CODE
    assert rec.handoff_mode == HandoffMode.OPEN_NATIVE


def test_refactor_to_codex() -> None:
    rec = _single(_req("重构这个模块"))
    assert rec.agent_id == "codex"
    assert rec.handoff_mode == HandoffMode.OPEN_NATIVE


def test_add_feature_to_codex() -> None:
    rec = _single(_req("新增一个导出功能"))
    assert rec.agent_id == "codex"
    assert rec.reason_code == ReasonCode.BEST_FOR_CODING


def test_fix_bug_to_codex() -> None:
    rec = _single(_req("修复这个 bug"))
    assert rec.agent_id == "codex"
    assert rec.intent == TaskIntent.DEBUG
    assert rec.handoff_mode == HandoffMode.OPEN_NATIVE


# -- 9-10. debug boundary --------------------------------------------------

def test_why_error_to_claude() -> None:
    rec = _single(_req("为什么这里会报错"))
    assert rec.agent_id == "claude"
    assert rec.intent == TaskIntent.DEBUG
    assert rec.handoff_mode == HandoffMode.SHORT_TALK


def test_fix_error_to_codex() -> None:
    rec = _single(_req("修复这个报错"))
    assert rec.agent_id == "codex"
    assert rec.intent == TaskIntent.DEBUG
    assert rec.handoff_mode == HandoffMode.OPEN_NATIVE


# -- 11-13. requested agent ------------------------------------------------

def test_requested_claude_overrides_default() -> None:
    # Default for this write text would be Codex; explicit Claude wins.
    rec = _single(_req("重构这个模块", agent="claude"))
    assert rec.agent_id == "claude"
    assert rec.handoff_mode == HandoffMode.OPEN_NATIVE  # managed can't write
    assert rec.requires_confirmation is True
    assert rec.reason_code == ReasonCode.NATIVE_SURFACE_REQUIRED


def test_requested_codex_overrides_default() -> None:
    # Default for this read text would be Claude; explicit Codex wins.
    rec = _single(_req("解释这段代码", agent="codex"))
    assert rec.agent_id == "codex"
    assert rec.handoff_mode == HandoffMode.OPEN_NATIVE  # no managed Short Talk
    assert rec.requires_confirmation is False  # read task


def test_chatgpt_request_does_not_fake_backend() -> None:
    rec = _single(_req("帮我改这段代码", agent="chatgpt"))
    assert rec.agent_id == "chatgpt"
    assert rec.handoff_mode == HandoffMode.UNAVAILABLE
    assert rec.reason_code == ReasonCode.BACKEND_UNAVAILABLE
    chat = _single(_req("你好", agent="chatgpt"))
    assert chat.handoff_mode == HandoffMode.UNAVAILABLE
    assert chat.reason_code == ReasonCode.BACKEND_UNAVAILABLE


# -- 14-15. chat / unknown -------------------------------------------------

def test_generic_short_chat_to_claude() -> None:
    rec = _single(_req("你好"))
    assert rec.agent_id == "claude"
    assert rec.intent == TaskIntent.CHAT
    assert rec.handoff_mode == HandoffMode.SHORT_TALK


def test_unknown_task_safe_fallback() -> None:
    rec = _single(_req("xqzkvbl"))
    assert rec.agent_id == "claude"
    assert rec.intent == TaskIntent.UNKNOWN
    assert rec.confidence == Confidence.LOW
    assert rec.handoff_mode == HandoffMode.SHORT_TALK


# -- 16-17. confirmation ---------------------------------------------------

def test_write_task_requires_confirmation() -> None:
    rec = _single(_req("实现一个功能"))
    assert rec.requires_confirmation is True
    rec2 = _single(_req("帮我修改这个模块", requires_write=True))
    assert rec2.requires_confirmation is True


def test_read_only_analysis_no_confirmation() -> None:
    rec = _single(_req("解释这段代码"))
    assert rec.requires_confirmation is False
    rec2 = _single(_req("分析一下这段代码"))
    assert rec2.requires_confirmation is False


# -- 18-20. handoff modes --------------------------------------------------

def test_codex_coding_is_open_native() -> None:
    rec = _single(_req("帮我写一个模块"))
    assert rec.agent_id == "codex"
    assert rec.handoff_mode == HandoffMode.OPEN_NATIVE


def test_claude_short_analysis_is_short_talk() -> None:
    rec = _single(_req("分析这个模块"))
    assert rec.agent_id == "claude"
    assert rec.handoff_mode == HandoffMode.SHORT_TALK


def test_long_claude_task_can_recommend_open_native() -> None:
    # Design supports: requested Claude + write -> OPEN_NATIVE (managed is
    # read-only, so the native surface is the honest execution path).
    rec = _single(_req("重构整个模块", agent="claude"))
    assert rec.agent_id == "claude"
    assert rec.handoff_mode == HandoffMode.OPEN_NATIVE
    assert rec.requires_confirmation is True


# -- 21-22. vision / registry honesty --------------------------------------

def test_vision_intent_does_not_fabricate_capability() -> None:
    rec = _single(_req("看一下这张截图"))
    assert rec.intent == TaskIntent.VISION
    assert rec.handoff_mode == HandoffMode.UNAVAILABLE
    assert rec.confidence == Confidence.LOW
    assert rec.reason_code == ReasonCode.VISION_MANAGED_UNAVAILABLE
    for agent in DEFAULT_CAPABILITY_REGISTRY.agents():
        assert DEFAULT_CAPABILITY_REGISTRY.has(agent, AgentCapability.VISION) is False


def test_capability_registry_matches_current_functionality() -> None:
    reg = DEFAULT_CAPABILITY_REGISTRY
    claude = reg.profile("claude")
    codex = reg.profile("codex")
    chatgpt = reg.profile("chatgpt")
    assert claude is not None and codex is not None and chatgpt is not None
    # Managed Short Talk is Claude-only.
    assert claude.managed_short_talk is True
    assert codex.managed_short_talk is False
    assert chatgpt.managed_short_talk is False
    assert reg.has("claude", AgentCapability.MANAGED_SHORT_TALK)
    assert not reg.has("codex", AgentCapability.MANAGED_SHORT_TALK)
    # Claude is the analysis/review/chat surface.
    for cap in (
        AgentCapability.CHAT,
        AgentCapability.ANALYSIS,
        AgentCapability.EXPLANATION,
        AgentCapability.REVIEW,
    ):
        assert reg.has("claude", cap), f"claude should have {cap.value}"
    # Codex is the native coding/implementation surface.
    for cap in (
        AgentCapability.CODING,
        AgentCapability.DEBUGGING,
        AgentCapability.FILE_READ,
        AgentCapability.FILE_WRITE,
        AgentCapability.TERMINAL,
        AgentCapability.LONG_TASK,
    ):
        assert reg.has("codex", cap), f"codex should have {cap.value}"
    # Both claude and codex have a native launch + lifecycle monitor.
    assert reg.has("claude", AgentCapability.NATIVE_LAUNCH)
    assert reg.has("codex", AgentCapability.NATIVE_LAUNCH)
    assert reg.has("claude", AgentCapability.LIFECYCLE_MONITOR)
    assert reg.has("codex", AgentCapability.LIFECYCLE_MONITOR)
    # ChatGPT has no task backend.
    assert chatgpt.available is False
    assert not reg.has("chatgpt", AgentCapability.CHAT)
    assert not reg.has("chatgpt", AgentCapability.CODING)
    assert not reg.has("chatgpt", AgentCapability.CHAT)
    # Asymmetry is not fabricated in either direction.
    assert not reg.has("codex", AgentCapability.CHAT)
    assert not reg.has("codex", AgentCapability.ANALYSIS)


# -- 23-27. determinism / purity -------------------------------------------

def test_same_input_same_output() -> None:
    router = AgentRouter()
    cases = [
        _req("解释这段代码"),
        _req("实现这个功能"),
        _req("先分析这个模块然后帮我改"),
        _req("修复这个报错"),
        _req("打开 Claude"),
        _req("你好"),
    ]
    for request in cases:
        first = router.recommend(request)
        second = router.recommend(request)
        assert first == second, f"nondeterministic for {request.text!r}"


def test_router_qt_free() -> None:
    for path in _ROUTER_FILES:
        hits = _forbidden_in_module(
            path,
            ("PySide6", "qtpy", "PyQt", "QProcess", "QWidget", "QObject", "QTimer", "Signal"),
        )
        assert not hits, f"{path.name} references Qt: {hits}"


def test_router_zero_subprocess() -> None:
    for path in _ROUTER_FILES:
        hits = _forbidden_in_module(path, ("subprocess", "Popen", "startDetached", "taskkill"))
        assert not hits, f"{path.name} starts processes: {hits}"


def test_router_zero_network() -> None:
    for path in _ROUTER_FILES:
        hits = _forbidden_in_module(path, ("socket", "urllib", "requests", "http", "urlopen", "websocket"))
        assert not hits, f"{path.name} touches the network: {hits}"


def test_reason_token_stable() -> None:
    # Every emitted reason code is a stable enum member, never free text.
    router = AgentRouter()
    seen: set[str] = set()
    for request in (
        _req("解释这段代码"),
        _req("帮我 review 一下这段代码"),
        _req("实现这个功能"),
        _req("先分析这个模块然后帮我改"),
        _req("修复这个报错"),
        _req("打开 Claude"),
        _req("看一下这张截图"),
        _req("帮我改这段代码", agent="chatgpt"),
        _req("重构整个模块", agent="claude"),
        _req("xqzkvbl"),
    ):
        for rec in router.recommend(request):
            assert isinstance(rec.reason_code, ReasonCode)
            seen.add(rec.reason_code.value)
    assert "best_for_coding" in seen
    assert "best_for_review" in seen
    assert "best_for_analysis" in seen


# -- 28-30. ranking / preference / multi-step ------------------------------

def test_top_score_gte_second() -> None:
    router = AgentRouter()
    for request in (
        _req("先分析这个模块然后帮我改"),
        _req("解释这段代码"),
        _req("实现这个功能"),
    ):
        recs = router.recommend(request)
        if len(recs) > 1:
            assert recs[0].score >= recs[1].score


def test_explicit_user_preference_priority() -> None:
    router = AgentRouter()
    default = router.recommend(_req("重构这个模块"))[0]
    assert default.agent_id == "codex"
    for agent in ("claude", "codex", "chatgpt"):
        rec = router.recommend(_req("重构这个模块", agent=agent))[0]
        assert rec.agent_id == agent


def test_multi_step_wording_no_workflow() -> None:
    recs = AgentRouter().recommend(_req("先分析这个模块然后帮我改"))
    assert len(recs) == 2
    assert recs[0].agent_id == "claude"
    assert recs[0].reason_code == ReasonCode.ANALYSIS_FIRST
    assert recs[0].multi_step_candidate is True
    assert recs[1].agent_id == "codex"
    assert recs[1].reason_code == ReasonCode.CODING_CAPABILITY
    assert recs[1].handoff_mode == HandoffMode.OPEN_NATIVE
    assert recs[1].requires_confirmation is True
    # The router never builds a workflow plan or launches anything.
    for rec in recs:
        assert isinstance(rec, AgentRecommendation)


# -- English prompts -------------------------------------------------------

def test_english_explain_to_claude() -> None:
    rec = _single(_req("Explain this function"))
    assert rec.agent_id == "claude"
    assert rec.handoff_mode == HandoffMode.SHORT_TALK


def test_english_review_to_claude() -> None:
    rec = _single(_req("Review this pull request"))
    assert rec.agent_id == "claude"
    assert rec.intent == TaskIntent.REVIEW


def test_english_code_to_codex() -> None:
    rec = _single(_req("Implement the authentication module"))
    assert rec.agent_id == "codex"
    assert rec.handoff_mode == HandoffMode.OPEN_NATIVE


def test_english_fix_error_to_codex() -> None:
    rec = _single(_req("Fix this error"))
    assert rec.agent_id == "codex"
    assert rec.intent == TaskIntent.DEBUG


def test_english_why_error_to_claude() -> None:
    rec = _single(_req("Why does this error happen?"))
    assert rec.agent_id == "claude"
    assert rec.intent == TaskIntent.DEBUG


# -- write-vs-explain boundary ---------------------------------------------

def test_how_to_change_is_explanation() -> None:
    rec = _single(_req("解释如何修改这个配置"))
    assert rec.agent_id == "claude"
    assert rec.intent == TaskIntent.EXPLAIN
    assert rec.handoff_mode == HandoffMode.SHORT_TALK


def test_english_how_to_fix_is_explanation() -> None:
    rec = _single(_req("How do I fix this bug?"))
    assert rec.agent_id == "claude"
    assert rec.intent == TaskIntent.EXPLAIN


# -- open-agent phrasing ---------------------------------------------------

def test_open_claude_routes_open_native() -> None:
    rec = _single(_req("打开 Claude"))
    assert rec.agent_id == "claude"
    assert rec.handoff_mode == HandoffMode.OPEN_NATIVE
    assert rec.reason_code == ReasonCode.OPEN_REQUESTED


def test_open_chatgpt_routes_open_native() -> None:
    rec = _single(_req("打开 ChatGPT"))
    assert rec.agent_id == "chatgpt"
    assert rec.handoff_mode == HandoffMode.OPEN_NATIVE
    assert rec.reason_code == ReasonCode.OPEN_REQUESTED


def test_plain_agent_mention_does_not_hijack() -> None:
    # "检查 Codex 刚改的代码" mentions Codex but is a review request -> Claude.
    rec = _single(_req("请检查 Codex 刚改的代码"))
    assert rec.agent_id == "claude"
    assert rec.handoff_mode == HandoffMode.SHORT_TALK


# -- registry injectability ------------------------------------------------

def test_router_accepts_injected_registry() -> None:
    from core.routing_models import AgentProfile

    fake = CapabilityRegistry(
        {
            "claude": AgentProfile(
                agent_id="claude",
                display_name="Claude",
                capabilities=frozenset({AgentCapability.CHAT}),
                available=False,
                managed_short_talk=False,
            )
        }
    )
    router = AgentRouter(registry=fake)
    rec = router.recommend(_req("解释这段代码", agent="claude"))[0]
    # Unavailable profile -> honest unavailable, not a fabricated success.
    assert rec.agent_id == "claude"
    assert rec.handoff_mode == HandoffMode.UNAVAILABLE
    assert rec.reason_code == ReasonCode.BACKEND_UNAVAILABLE


# -- router never returns empty / never crashes ----------------------------

def test_recommend_never_empty_and_no_crash() -> None:
    router = AgentRouter()
    for text in ("", "   ", " 你好 ", "asdf", "打开", "用 claude", "帮我改"):
        recs = router.recommend(_req(text))
        assert len(recs) >= 1, f"empty result for {text!r}"
        assert isinstance(recs[0], AgentRecommendation)


def main() -> None:
    tests = [
        test_explain_to_claude,
        test_analyze_to_claude,
        test_summarize_to_claude,
        test_review_to_claude,
        test_code_implementation_to_codex,
        test_refactor_to_codex,
        test_add_feature_to_codex,
        test_fix_bug_to_codex,
        test_why_error_to_claude,
        test_fix_error_to_codex,
        test_requested_claude_overrides_default,
        test_requested_codex_overrides_default,
        test_chatgpt_request_does_not_fake_backend,
        test_generic_short_chat_to_claude,
        test_unknown_task_safe_fallback,
        test_write_task_requires_confirmation,
        test_read_only_analysis_no_confirmation,
        test_codex_coding_is_open_native,
        test_claude_short_analysis_is_short_talk,
        test_long_claude_task_can_recommend_open_native,
        test_vision_intent_does_not_fabricate_capability,
        test_capability_registry_matches_current_functionality,
        test_same_input_same_output,
        test_router_qt_free,
        test_router_zero_subprocess,
        test_router_zero_network,
        test_reason_token_stable,
        test_top_score_gte_second,
        test_explicit_user_preference_priority,
        test_multi_step_wording_no_workflow,
        test_english_explain_to_claude,
        test_english_review_to_claude,
        test_english_code_to_codex,
        test_english_fix_error_to_codex,
        test_english_why_error_to_claude,
        test_how_to_change_is_explanation,
        test_english_how_to_fix_is_explanation,
        test_open_claude_routes_open_native,
        test_open_chatgpt_routes_open_native,
        test_plain_agent_mention_does_not_hijack,
        test_router_accepts_injected_registry,
        test_recommend_never_empty_and_no_crash,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception:
            failed += 1
            print(f"FAIL  {fn.__name__}")
            raise
    print(f"Phase 9A agent router tests passed ({len(tests)} tests).")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
