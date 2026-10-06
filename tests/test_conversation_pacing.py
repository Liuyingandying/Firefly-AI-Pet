"""Small opt-in pacing experiment gates; no live provider or UX scoring.

Tests selection and one short generation hint. They do not prescribe generated
phrasing, replace the original owners, or invoke a reply reviewer.
"""
from __future__ import annotations

import ast
from copy import deepcopy
from dataclasses import FrozenInstanceError
import inspect

import pytest

from character.character_loader import CharacterLoader
from core.bond_rules import BondSignalType
from core.companion_config import CompanionConfig, SuggestionSettings
from core.companion_context_builder import CompanionContextBuilder
from core.companion_runtime import CompanionRuntime
from core.conversation_pacing import (
    PacingDecision, PacingMode, pacing_hint_for_turn, select_pacing,
)


def short_history(pair_count=8):
    history = []
    for index in range(pair_count):
        history.extend([
            {"role": "user", "content": ("嗯", "好呀", "嘿嘿", "在呢")[index % 4]},
            {"role": "assistant", "content": "嗯～"},
        ])
    return history


@pytest.mark.parametrize("text,expected", [
    ("早上好，萤宝", PacingMode.NORMAL),
    ("亲亲", PacingMode.QUICK),
    ("抱抱我", PacingMode.NORMAL),
    ("我想你了", PacingMode.LINGER),
    ("今天有点累，陪我待会儿", PacingMode.LINGER),
    ("今天没什么想说的", PacingMode.NORMAL),
    ("我最近真的撑不住了，想认真聊聊。", PacingMode.DEEP),
    ("UART是什么？", PacingMode.QUICK),
    ("这个状态空间公式为什么这样推？", PacingMode.DEEP),
])
def test_product_scenarios_select_pacing_without_generating_replies(text, expected):
    assert select_pacing(text).mode == expected


def test_eight_short_pairs_and_current_relationship_intent_keep_distinct_pacing():
    history = short_history(8)
    before = deepcopy(history)
    assert select_pacing("嗯嗯", history=history).mode == PacingMode.QUICK
    assert select_pacing("我想你了嘛", history=history).mode == PacingMode.NORMAL
    assert history == before


def test_complex_technical_turn_overrides_nickname_and_short_streak():
    decision = select_pacing("亲爱的，这个状态空间公式为什么这样推？", history=short_history(8))
    assert decision.mode == PacingMode.DEEP


def test_explicit_current_preferences_and_low_emotion_are_not_psychology_analysis():
    assert select_pacing("有点困").mode == PacingMode.NORMAL
    assert select_pacing("今天没什么想说的").no_questions
    no_analysis = select_pacing("别分析，陪我待会儿。")
    assert no_analysis.no_analysis
    assert no_analysis.to_prompt() != PacingDecision(no_analysis.mode).to_prompt()
    no_probing = select_pacing("别追问，我不想说原因。")
    assert no_probing.no_questions
    assert no_probing.to_prompt() != PacingDecision(no_probing.mode).to_prompt()
    concise = select_pacing("简单说，这个状态空间公式为什么这样推？", history=short_history(8))
    assert concise.mode == PacingMode.QUICK


def test_quoted_or_negated_preferences_and_topic_reset_are_not_current_requests():
    assert select_pacing("你上次说‘简短点’是什么意思？").mode != PacingMode.QUICK
    assert select_pacing("不要认真聊，今天随便聊。").mode != PacingMode.DEEP
    history = [{"role": "user", "content": "别追问，别分析，我不想说原因。"},
               {"role": "assistant", "content": "好，先陪你待一会儿。"}]
    for text in ("换个话题，UART是什么？", "换个话题，今天随便聊。"):
        reset = select_pacing(text, history=history)
        assert not reset.no_questions and not reset.no_analysis


def test_current_structure_requests_quotes_and_negated_turn_do_not_follow_short_streak():
    history = short_history(8)
    for text in ("逐步解释 AXI 协议怎么握手。", "比较 AXI 和 APB 的差异。"):
        assert select_pacing(text, history=history).mode == PacingMode.DEEP
    assert select_pacing('你上次说"一句话"是什么意思？', history=history).mode == PacingMode.NORMAL
    assert select_pacing("AXI/APB怎么选？", history=history).mode == PacingMode.NORMAL
    for recent in ((), history):
        assert select_pacing("不是想你，换个话题。", history=recent).mode == PacingMode.NORMAL


def test_history_is_a_six_pair_read_window_with_no_retained_preferences():
    recent = short_history(6)
    old = [{"role": "user", "content": "别追问，别分析，我想认真聊聊。"},
           {"role": "assistant", "content": "过去的测试回合。"}]
    combined = old + recent
    assert select_pacing("嗯嗯", history=combined) == select_pacing("嗯嗯", history=recent)
    # A new call without history cannot inherit the previous decision flags.
    select_pacing("别追问，别分析，我不想说原因。", history=combined)
    fresh = select_pacing("早上好，萤宝", history=())
    assert fresh.mode == PacingMode.NORMAL and not fresh.no_questions and not fresh.no_analysis


def test_decisions_are_frozen_and_all_fixed_hints_stay_micro_sized():
    for mode in PacingMode:
        decision = PacingDecision(mode, no_questions=True, no_analysis=True)
        hint = decision.to_prompt()
        assert isinstance(hint, str) and 0 < len(hint) < 100
        assert "PERSONA CONTEXT READ LAYER" not in hint
        assert hint == decision.to_prompt()
    with pytest.raises(FrozenInstanceError):
        PacingDecision(PacingMode.NORMAL).mode = PacingMode.DEEP


def test_feature_is_default_off_and_only_exact_opt_in_for_chat(monkeypatch):
    monkeypatch.delenv("FIREFLY_CONVERSATION_PACING", raising=False)
    assert pacing_hint_for_turn("抱抱我") == ""
    monkeypatch.setenv("FIREFLY_CONVERSATION_PACING", "0")
    assert pacing_hint_for_turn("抱抱我") == ""
    monkeypatch.setenv("FIREFLY_CONVERSATION_PACING", "true")
    assert pacing_hint_for_turn("抱抱我") == ""
    monkeypatch.setenv("FIREFLY_CONVERSATION_PACING", "1")
    assert pacing_hint_for_turn("抱抱我") == select_pacing("抱抱我").to_prompt()
    for task in ("camera_look", "screen_look", "learning_teach", "learning_grade", "review"):
        assert pacing_hint_for_turn("抱抱我", current_task=task) == ""


class MemorySpy:
    def __init__(self):
        self.queries = []
        self.remember_attempts = []

    def search(self, query, **kwargs):
        self.queries.append(query)
        return []

    def remember_detailed(self, text, **kwargs):
        self.remember_attempts.append((text, kwargs))
        return None  # original explicit-only policy rejects this ordinary turn


class BondSpy:
    def __init__(self):
        self.reads = 0
        self.signals = []

    def read(self):
        self.reads += 1
        return None

    def apply(self, signal):
        self.signals.append(signal.type)


class StoreSpy:
    def __init__(self):
        self.loads = 0
        self.saved = []

    def load_working_window(self):
        self.loads += 1
        return []

    def append_exchange(self, user, answer):
        self.saved.append((user, answer))


class ProviderSpy:
    def __init__(self):
        self.calls = []
        self.response = {"model": "existing-model", "choices": [{"message": {
            "role": "assistant", "content": "模型原文，保持不动。"}}]}

    def chat(self, messages, model=None, temperature=.2):
        self.calls.append((deepcopy(messages), model, temperature))
        return self.response


def runtime_graph():
    character, memory, bond, store, provider = (
        CharacterLoader().load(), MemorySpy(), BondSpy(), StoreSpy(), ProviderSpy())
    runtime = CompanionRuntime(character=character, memory_service=memory,
        bond_state_engine=bond, conversation_store=store, provider_router=provider,
        config=CompanionConfig(suggestion=SuggestionSettings(enabled=False)))
    return runtime, memory, bond, store, provider


def test_off_preserves_exact_original_prompt_order_and_enabled_adds_only_one_hint(monkeypatch):
    runtime, memory, bond, _, _ = runtime_graph()
    history = [{"role": "user", "content": "上一轮。"}, {"role": "assistant", "content": "嗯。"}]
    text = "抱抱我"
    expected = runtime.character.to_system_messages() + history + [{"role": "user", "content": text}]
    monkeypatch.delenv("FIREFLY_CONVERSATION_PACING", raising=False)
    baseline = runtime.build_messages(text, history=history)
    assert baseline == expected
    monkeypatch.setenv("FIREFLY_CONVERSATION_PACING", "1")
    enabled = runtime.build_messages(text, history=history)
    hint = pacing_hint_for_turn(text, history=history)
    character_count = len(runtime.character.to_system_messages())
    assert enabled == baseline[:character_count] + [{"role": "system", "content": hint}] + baseline[character_count:]
    assert sum(m["content"] == hint for m in enabled) == 1 and len(hint) < 100
    assert not any("PERSONA CONTEXT READ LAYER" in m["content"] for m in enabled)
    assert memory.queries == [text, text] and bond.reads == 2 and bond.signals == []


@pytest.mark.parametrize("enabled", [False, True])
def test_pacing_changes_no_model_calls_reply_processing_or_owner_lifecycle(monkeypatch, enabled):
    monkeypatch.setenv("FIREFLY_CONVERSATION_PACING", "1" if enabled else "0")
    runtime, memory, bond, store, provider = runtime_graph()
    response = runtime.chat("抱抱我", model="existing-model-selection", temperature=.2)
    assert response is provider.response and len(provider.calls) == 1
    assert response["choices"][0]["message"]["content"] == "模型原文，保持不动。"
    messages, model, temperature = provider.calls[0]
    assert model == "existing-model-selection" and temperature == .2
    assert memory.queries == ["抱抱我"] and len(memory.remember_attempts) == 1
    assert memory.remember_attempts[0][1] == {"trigger": "explicit-command"}
    assert bond.reads == 1 and bond.signals == [BondSignalType.TURN_COMPLETED]
    assert store.loads == 1 and store.saved == [("抱抱我", "模型原文，保持不动。")]
    assert len(messages) == len(runtime.character.to_system_messages()) + 1 + int(enabled)


def test_pacing_selector_has_no_owner_provider_or_persistence_imports():
    import core.conversation_pacing as module
    tree = ast.parse(inspect.getsource(module))
    forbidden = ("memory", "providers", "core.ai_router", "core.bond", "core.conversation_store",
                 "core.persona", "core.relationship", "docs")
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
        elif isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
    assert not any(name.startswith(forbidden) for name in imports)
