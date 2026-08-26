"""Offline tests for the BondContextBuilder prompt rendering."""

from __future__ import annotations

from core.bond_context_builder import BondContextBuilder
from core.bond_rules import BondPhase
from core.bond_state import BondState


def test_empty_state_renders_no_context() -> None:
    builder = BondContextBuilder()

    context = builder.build(BondState())

    assert context.is_empty
    assert context.to_prompt() == ""


def test_non_bond_state_renders_no_context() -> None:
    builder = BondContextBuilder()

    assert builder.build(None).to_prompt() == ""
    assert builder.build(object()).to_prompt() == ""


def test_context_uses_natural_language_and_hides_internal_levels() -> None:
    builder = BondContextBuilder()
    state = BondState(
        phase=BondPhase.FAMILIAR,
        trust_level=0.5,
        familiarity_level=0.5,
        shared_milestones=("一起完成 Firefly v0.3",),
        pending_promises=("明天继续实现",),
    )

    prompt = builder.build(state).to_prompt()

    assert "BEGIN BOND CONTEXT" in prompt
    assert "彼此熟悉" in prompt
    assert "一起完成 Firefly v0.3" in prompt
    assert "明天继续实现" in prompt
    assert "trust_level" not in prompt
    assert "familiarity_level" not in prompt
    assert "0.5" not in prompt


def test_stranger_phase_with_details_still_renders_details() -> None:
    builder = BondContextBuilder()
    state = BondState(
        phase=BondPhase.STRANGER,
        trust_level=0.0,
        familiarity_level=0.0,
        shared_milestones=("第一次见面",),
    )

    prompt = builder.build(state).to_prompt()

    assert "BEGIN BOND CONTEXT" in prompt
    assert "第一次见面" in prompt
    assert "关系阶段" not in prompt
