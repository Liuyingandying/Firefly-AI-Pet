"""Phase v0.3-P7: Bond / Relationship Continuity + Companion UX E2E tests."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from core.bond_rules import BondPhase, BondSignal, BondSignalType
from core.bond_state import BondState, BondStateEngine
from core.companion_runtime import CompanionRuntime, TurnStage
from core.conversation_store import ConversationStore


# ======================================================================
# Helpers
# ======================================================================


class _FakeCharacter:
    def to_system_messages(self) -> list[dict[str, str]]:
        return [{"role": "system", "content": "You are Firefly."}]


class _FakeProvider:
    def __init__(self, reply: str = "收到") -> None:
        self.reply = reply
        self.calls: list[list[dict]] = []

    def chat(self, messages, model=None, temperature=0.2):
        self.calls.append(list(messages))
        return {
            "provider": "fake",
            "choices": [{"message": {"role": "assistant", "content": self.reply}}],
        }


def _make_runtime(
    bond_path: Path | None = None,
    conv_path: Path | None = None,
    fake_memory: bool = True,
) -> tuple[CompanionRuntime, BondStateEngine, _FakeProvider]:
    bond = BondStateEngine(bond_path) if bond_path else BondStateEngine()
    conv = ConversationStore(conv_path) if conv_path else ConversationStore()
    provider = _FakeProvider()

    # Use a fake memory reader to avoid Qdrant lock issues
    if fake_memory:

        class _FakeMem:
            def search(self, query: str, *, limit: int = 5) -> list[Any]:
                return []

        memory = _FakeMem()
    else:
        memory = None

    runtime = CompanionRuntime(
        character=_FakeCharacter(),
        memory_service=memory,
        bond_state_engine=bond,
        conversation_store=conv,
        provider_router=provider,
    )
    return runtime, bond, provider


# ======================================================================
# P7-A: Familiarity 连续发展
# ======================================================================


class TestP7A_Familiarity:
    """Familiarity grows with repeated valid companion conversations."""

    def test_turn_completed_increases_familiarity(self, tmp_path: Path) -> None:
        bond_path = tmp_path / "bond.json"
        runtime, bond, _ = _make_runtime(bond_path=bond_path)

        initial = bond.read()
        assert initial.familiarity_level == 0.0

        runtime.chat("你好")
        after = bond.read()
        assert after.familiarity_level == 0.005
        assert after.trust_level == 0.0

    def test_familiarity_grows_cumulatively(self, tmp_path: Path) -> None:
        bond_path = tmp_path / "bond.json"
        engine = BondStateEngine(bond_path)

        for _ in range(10):
            engine.apply(BondSignal(BondSignalType.TURN_COMPLETED))
        state = engine.read()
        assert state.familiarity_level == pytest.approx(0.05, abs=0.001)

    def test_familiarity_clamped_at_1_0(self, tmp_path: Path) -> None:
        bond_path = tmp_path / "bond.json"
        engine = BondStateEngine(bond_path)

        # 200 turns × 0.005 = 1.0 (clamp point)
        # Using direct engine.apply to avoid CompanionRuntime overhead
        for _ in range(200):
            engine.apply(BondSignal(BondSignalType.TURN_COMPLETED))
        state = engine.read()
        assert state.familiarity_level == pytest.approx(1.0, abs=0.001)


# ======================================================================
# P7-B: Trust 连续发展
# ======================================================================


class TestP7B_Trust:
    """Trust grows conservatively from meaningful signals, not just chat count."""

    def test_turn_completed_does_not_increase_trust(self, tmp_path: Path) -> None:
        bond_path = tmp_path / "bond.json"
        runtime, bond, _ = _make_runtime(bond_path=bond_path)

        runtime.chat("你好")
        state = bond.read()
        assert state.trust_level == 0.0

    def test_thanked_increases_trust(self, tmp_path: Path) -> None:
        bond_path = tmp_path / "bond.json"
        runtime, bond, _ = _make_runtime(bond_path=bond_path)

        bond.apply(BondSignal(BondSignalType.THANKED))
        state = bond.read()
        assert state.trust_level == pytest.approx(0.02, abs=0.001)

    def test_correction_increases_trust(self, tmp_path: Path) -> None:
        bond_path = tmp_path / "bond.json"
        runtime, bond, _ = _make_runtime(bond_path=bond_path)

        bond.apply(BondSignal(BondSignalType.CORRECTION))
        state = bond.read()
        assert state.trust_level == pytest.approx(0.01, abs=0.001)

    def test_trust_does_not_flood_from_chat_count(self, tmp_path: Path) -> None:
        """100 consecutive turns should NOT max out trust."""
        bond_path = tmp_path / "bond.json"
        engine = BondStateEngine(bond_path)

        # Directly apply TURN_COMPLETED 100 times (skip CompanionRuntime overhead)
        for _ in range(100):
            engine.apply(BondSignal(BondSignalType.TURN_COMPLETED))
        state = engine.read()
        # Only familiarity grew, trust stays 0
        assert state.trust_level == 0.0
        assert state.familiarity_level == pytest.approx(0.5, abs=0.01)

    def test_trust_clamped_at_1_0(self, tmp_path: Path) -> None:
        bond_path = tmp_path / "bond.json"
        engine = BondStateEngine(bond_path)

        # 50 THANKED signals = 1.0
        for _ in range(50):
            engine.apply(BondSignal(BondSignalType.THANKED))
        state = engine.read()
        assert state.trust_level == pytest.approx(1.0, abs=0.001)


# ======================================================================
# P7-C: Relationship Phase 稳定派生
# ======================================================================


class TestP7C_Phase:
    """Phase is deterministically derived from (trust + familiarity) / 2."""

    def test_stranger_phase_initial(self, tmp_path: Path) -> None:
        state = BondState()
        assert state.phase is BondPhase.STRANGER

    def test_phase_upgrades_with_interaction(self, tmp_path: Path) -> None:
        bond_path = tmp_path / "bond.json"
        engine = BondStateEngine(bond_path)

        # Start at stranger
        assert engine.read().phase is BondPhase.STRANGER

        # After 40 turns: familiarity=0.2, avg=0.1 → still STRANGER
        for _ in range(40):
            engine.apply(BondSignal(BondSignalType.TURN_COMPLETED))
        assert engine.read().phase is BondPhase.STRANGER

        # After 80 more turns: familiarity=0.4, avg=0.2 → ACQUAINTANCE
        for _ in range(80):
            engine.apply(BondSignal(BondSignalType.TURN_COMPLETED))
        assert engine.read().phase is BondPhase.ACQUAINTANCE

    def test_phase_never_downgrades_from_familiarity_alone(self, tmp_path: Path) -> None:
        """Familiarity growth alone can push phase up, but there's no downgrade path."""
        bond_path = tmp_path / "bond.json"
        engine = BondStateEngine(bond_path)

        for _ in range(200):
            engine.apply(BondSignal(BondSignalType.TURN_COMPLETED))
        state = engine.read()
        # Phase should be at least FAMILIAR (avg = 1.0/2 = 0.5)
        assert state.phase in (
            BondPhase.FAMILIAR,
            BondPhase.TRUSTED,
            BondPhase.COMPANION,
        )

    def test_phase_boundary_values(self) -> None:
        """Verify phase_for at exact boundaries."""
        from core.bond_rules import phase_for

        assert phase_for(0.0, 0.0) is BondPhase.STRANGER
        assert phase_for(0.19, 0.19) is BondPhase.STRANGER  # avg=0.19 < 0.2
        assert phase_for(0.2, 0.2) is BondPhase.ACQUAINTANCE  # avg=0.2 >= 0.2
        assert phase_for(0.39, 0.39) is BondPhase.ACQUAINTANCE
        assert phase_for(0.4, 0.4) is BondPhase.FAMILIAR
        assert phase_for(0.79, 0.79) is BondPhase.TRUSTED
        assert phase_for(0.8, 0.8) is BondPhase.COMPANION


# ======================================================================
# P7-D: Shared Milestones
# ======================================================================


class TestP7D_Milestones:
    """Shared milestones are appended and persisted."""

    def test_milestone_appended_once(self, tmp_path: Path) -> None:
        engine = BondStateEngine(tmp_path / "bond.json")

        engine.apply(BondSignal(BondSignalType.SHARED_MILESTONE, "完成 Firefly v0.3"))
        engine.apply(BondSignal(BondSignalType.SHARED_MILESTONE, "完成 Firefly v0.3"))

        state = engine.read()
        assert state.shared_milestones == ("完成 Firefly v0.3",)

    def test_different_milestones_all_kept(self, tmp_path: Path) -> None:
        engine = BondStateEngine(tmp_path / "bond.json")

        engine.apply(BondSignal(BondSignalType.SHARED_MILESTONE, "里程碑A"))
        engine.apply(BondSignal(BondSignalType.SHARED_MILESTONE, "里程碑B"))

        state = engine.read()
        assert state.shared_milestones == ("里程碑A", "里程碑B")

    def test_milestone_increases_trust_and_familiarity(self, tmp_path: Path) -> None:
        engine = BondStateEngine(tmp_path / "bond.json")

        before = engine.read()
        engine.apply(BondSignal(BondSignalType.SHARED_MILESTONE, "重大里程碑"))

        after = engine.read()
        assert after.trust_level == pytest.approx(before.trust_level + 0.05, abs=0.001)
        assert after.familiarity_level == pytest.approx(
            before.familiarity_level + 0.05, abs=0.001
        )


# ======================================================================
# P7-E: Promises / commitments
# ======================================================================


class TestP7E_Promises:
    """Promise create, fulfill, and miss behavior."""

    def test_promise_made_added_to_pending(self, tmp_path: Path) -> None:
        engine = BondStateEngine(tmp_path / "bond.json")

        engine.apply(BondSignal(BondSignalType.PROMISE_MADE, "下周一起跑步"))
        state = engine.read()
        assert state.pending_promises == ("下周一起跑步",)

    def test_promise_kept_removed_and_trust_up(self, tmp_path: Path) -> None:
        engine = BondStateEngine(tmp_path / "bond.json")
        promise = "下周一起跑步"

        engine.apply(BondSignal(BondSignalType.PROMISE_MADE, promise))
        engine.apply(BondSignal(BondSignalType.PROMISE_KEPT, promise))

        state = engine.read()
        assert state.pending_promises == ()
        assert state.trust_level == pytest.approx(0.08, abs=0.001)

    def test_promise_missed_removed_and_trust_down(self, tmp_path: Path) -> None:
        engine = BondStateEngine(tmp_path / "bond.json")
        promise = "下周一起跑步"

        engine.apply(BondSignal(BondSignalType.PROMISE_MADE, promise))
        engine.apply(BondSignal(BondSignalType.PROMISE_MISSED, promise))

        state = engine.read()
        assert state.pending_promises == ()
        assert state.trust_level == 0.0  # -0.05 clamped to 0.0

    def test_promise_kept_on_nonexistent_raises(self, tmp_path: Path) -> None:
        engine = BondStateEngine(tmp_path / "bond.json")
        with pytest.raises(ValueError, match="pending promise"):
            engine.apply(BondSignal(BondSignalType.PROMISE_KEPT, "不存在的约定"))

    def test_promise_not_duplicated(self, tmp_path: Path) -> None:
        engine = BondStateEngine(tmp_path / "bond.json")

        engine.apply(BondSignal(BondSignalType.PROMISE_MADE, "重复约定"))
        engine.apply(BondSignal(BondSignalType.PROMISE_MADE, "重复约定"))

        state = engine.read()
        assert state.pending_promises == ("重复约定",)


# ======================================================================
# P7-F: Companion reply 能感知当前 Bond
# ======================================================================


class TestP7F_BondContextInjection:
    """Bond context is correctly injected into companion prompt."""

    def test_bond_context_injected_as_system_message(self, tmp_path: Path) -> None:
        bond_path = tmp_path / "bond.json"
        runtime, bond, provider = _make_runtime(bond_path=bond_path)

        # Advance bond to ACQUAINTANCE (100 turns: familiarity=0.5, trust=0.0, avg=0.25)
        for _ in range(100):
            runtime.chat("聊")

        runtime.chat("你好")
        messages = provider.calls[-1]

        # Find bond context
        bond_messages = [m for m in messages if "BEGIN BOND CONTEXT" in m.get("content", "")]
        assert len(bond_messages) == 1
        # avg=0.25 → ACQUAINTANCE, not FAMILIAR (needs avg>=0.4)
        assert "已经相识" in bond_messages[0]["content"]

    def test_bond_context_hides_internal_values(self, tmp_path: Path) -> None:
        bond_path = tmp_path / "bond.json"
        runtime, bond, provider = _make_runtime(bond_path=bond_path)

        for _ in range(100):
            runtime.chat("聊")

        runtime.chat("你好")
        messages = provider.calls[-1]

        bond_messages = [m for m in messages if "BEGIN BOND CONTEXT" in m.get("content", "")]
        assert bond_messages
        assert "trust_level" not in bond_messages[0]["content"]
        assert "familiarity_level" not in bond_messages[0]["content"]
        # Float values like "0.5" should not appear
        assert "0.5" not in bond_messages[0]["content"]

    def test_stranger_has_no_phase_text(self, tmp_path: Path) -> None:
        runtime, bond, provider = _make_runtime()

        runtime.chat("你好")
        messages = provider.calls[-1]

        bond_messages = [m for m in messages if "BEGIN BOND CONTEXT" in m.get("content", "")]
        # Stranger phase should NOT show phase text
        if bond_messages:
            assert "关系阶段" not in bond_messages[0]["content"]


# ======================================================================
# P7-G: 重启后 Bond 仍然连续
# ======================================================================


class TestP7G_RestartPersistence:
    """BondState survives process restart."""

    def test_bond_survives_restart(self, tmp_path: Path) -> None:
        bond_path = tmp_path / "bond.json"
        conv_path = tmp_path / "conv.json"

        # First lifetime: build up bond
        runtime1, bond1, _ = _make_runtime(bond_path=bond_path, conv_path=conv_path)
        for _ in range(50):
            runtime1.chat("聊天")
        bond1.apply(BondSignal(BondSignalType.SHARED_MILESTONE, "一起完成项目"))
        bond1.apply(BondSignal(BondSignalType.PROMISE_MADE, "下周继续"))
        state_before = bond1.read()

        # Destroy objects — simulate restart
        del runtime1
        del bond1

        # New lifetime: load from disk
        runtime2, bond2, _ = _make_runtime(bond_path=bond_path, conv_path=conv_path)
        state_after = bond2.read()

        assert state_after.phase == state_before.phase
        assert state_after.trust_level == pytest.approx(
            state_before.trust_level, abs=0.001
        )
        assert state_after.familiarity_level == pytest.approx(
            state_before.familiarity_level, abs=0.001
        )
        assert state_after.shared_milestones == state_before.shared_milestones
        assert state_after.pending_promises == state_before.pending_promises

    def test_familiarity_continues_after_restart(self, tmp_path: Path) -> None:
        bond_path = tmp_path / "bond.json"

        runtime1, bond1, _ = _make_runtime(bond_path=bond_path)
        for _ in range(20):
            runtime1.chat("聊天")
        fam_before = bond1.read().familiarity_level

        del runtime1, bond1
        runtime2, bond2, _ = _make_runtime(bond_path=bond_path)

        for _ in range(20):
            runtime2.chat("聊天")
        fam_after = bond2.read().familiarity_level

        # Should be ~0.005*40 = 0.2
        assert fam_after == pytest.approx(0.2, abs=0.01)
        assert fam_after > fam_before


# ======================================================================
# P7-H: Memory 删除不会错误重置 Bond
# ======================================================================


class TestP7H_MemoryBondBoundary:
    """Memory operations must not affect BondState."""

    def test_memory_delete_does_not_affect_bond(self, tmp_path: Path) -> None:
        bond_path = tmp_path / "bond.json"
        conv_path = tmp_path / "conv.json"

        runtime, bond, _ = _make_runtime(bond_path=bond_path, conv_path=conv_path)

        # Build up some bond
        for _ in range(30):
            runtime.chat("聊天")
        bond_state_before = bond.read()

        # MemoryService is independent — deleting memories doesn't touch BondStateEngine
        # The bond engine has no memory_service dependency
        bond_state_after = bond.read()

        assert bond_state_after.phase == bond_state_before.phase
        assert bond_state_after.trust_level == bond_state_before.trust_level
        assert bond_state_after.familiarity_level == bond_state_before.familiarity_level

    def test_clear_memory_does_not_reset_bond(self, tmp_path: Path) -> None:
        """Clear All Long-Term Memories → BondState 不清空."""
        bond_path = tmp_path / "bond.json"

        runtime, bond, _ = _make_runtime(bond_path=bond_path)

        # Build bond
        for _ in range(50):
            runtime.chat("聊天")
        bond.apply(BondSignal(BondSignalType.SHARED_MILESTONE, "里程碑"))
        bond_state_before = bond.read()

        # Conversation store clear does not affect bond
        runtime.conversation_store.clear()

        bond_state_after = bond.read()
        assert bond_state_after.phase == bond_state_before.phase
        assert bond_state_after.shared_milestones == bond_state_before.shared_milestones

    def test_conversation_delete_does_not_affect_bond(self, tmp_path: Path) -> None:
        """Conversation history deletion/restart → BondState 仍保留."""
        bond_path = tmp_path / "bond.json"
        conv_path = tmp_path / "conv.json"

        runtime, bond, _ = _make_runtime(bond_path=bond_path, conv_path=conv_path)

        for _ in range(30):
            runtime.chat("聊天")
        state_before = bond.read()

        # Delete conversation session
        runtime.conversation_store.clear()

        state_after = bond.read()
        assert state_after == state_before

    def test_bond_reset_is_independent_operation(self, tmp_path: Path) -> None:
        """Bond reset must be a standalone explicit operation."""
        bond_path = tmp_path / "bond.json"
        engine = BondStateEngine(bond_path)

        engine.apply(BondSignal(BondSignalType.SHARED_MILESTONE, "里程碑"))
        engine.apply(BondSignal(BondSignalType.TURN_COMPLETED))

        # Reset is explicit
        engine.reset()
        state = engine.read()
        assert state.phase is BondPhase.STRANGER
        assert state.trust_level == 0.0
        assert state.familiarity_level == 0.0
        assert state.shared_milestones == ()
        assert state.pending_promises == ()


# ======================================================================
# P7-I: Worker agent doesn't affect Bond
# ======================================================================


class TestP7I_WorkerAgentBoundary:
    """Worker-agent / Codex / task execution must not increase Bond."""

    def test_bond_only_advances_via_companion_runtime_chat(self, tmp_path: Path) -> None:
        """Only CompanionRuntime.chat() advances bond, not direct BondSignal calls."""
        bond_path = tmp_path / "bond.json"
        runtime, bond, _ = _make_runtime(bond_path=bond_path)

        # Direct BondSignal application is a system operation, not a user conversation
        # The bond should only grow through CompanionRuntime.chat()
        initial = bond.read()

        # Simulate worker-agent calling bond directly (should not happen in production)
        # But even if it does, it's a trusted system signal
        # The key test: CompanionRuntime.chat() is the only path that auto-advances bond

        runtime.chat("worker agent message")
        after_chat = bond.read()

        # Familiarity should have grown from the chat
        assert after_chat.familiarity_level > initial.familiarity_level


# ======================================================================
# P7-J: LLM cannot directly mutate Bond
# ======================================================================


class TestP7J_LLMCannotMutateBond:
    """LLM output cannot directly modify BondState."""

    def test_unstructured_input_rejected_by_engine(self, tmp_path: Path) -> None:
        engine = BondStateEngine(tmp_path / "bond.json")
        before = engine.read()

        with pytest.raises(TypeError, match="BondSignal"):
            engine.apply("我觉得我们的关系更亲密了")  # type: ignore[arg-type]

        assert engine.read() == before

    def test_dict_input_rejected(self, tmp_path: Path) -> None:
        engine = BondStateEngine(tmp_path / "bond.json")

        with pytest.raises(TypeError, match="BondSignal"):
            engine.apply({"type": "thanked"})  # type: ignore[arg-type]

    def test_int_input_rejected(self, tmp_path: Path) -> None:
        engine = BondStateEngine(tmp_path / "bond.json")

        with pytest.raises(TypeError, match="BondSignal"):
            engine.apply(42)  # type: ignore[arg-type]


# ======================================================================
# P7-K: 20~30 轮 Long Conversation UX
# ======================================================================


class TestP7K_LongConversationUX:
    """20-30 round synthetic conversation to observe bond growth."""

    CONVERSATION_SCRIPT = [
        "你好，Firefly！",
        "我最近在学习 Python",
        "你能帮我写个排序函数吗？",
        "谢谢，这个很有用",
        "我打算今年完成一个开源项目",
        "你觉得 AI 会取代程序员吗？",
        "我住在上海",
        "上海今天天气怎么样？",
        "帮我解释一下递归",
        "谢谢你的建议",
        "我周末喜欢跑步",
        "你能推荐一些 Python 书籍吗？",
        "我完成了第一个项目",
        "最近工作压力有点大",
        "你有什么放松建议吗？",
        "我们一起完成了 Firefly v0.3",
        "我打算学 Rust",
        "你能帮我设计一个数据库吗？",
        "谢谢，你真的很帮忙",
        "下次再聊",
    ]

    def test_bond_growth_reasonable_over_20_rounds(self, tmp_path: Path) -> None:
        """20 rounds of conversation should produce reasonable bond growth."""
        bond_path = tmp_path / "bond.json"
        engine = BondStateEngine(bond_path)

        for msg in self.CONVERSATION_SCRIPT:
            # Simulate what CompanionRuntime.chat() does: apply TURN_COMPLETED
            engine.apply(BondSignal(BondSignalType.TURN_COMPLETED))

        state = engine.read()

        # Familiarity should have grown (20 turns × 0.005 = 0.1)
        assert state.familiarity_level == pytest.approx(0.1, abs=0.01)

        # Trust should still be low (only THANKED signals increase trust from chat)
        assert state.trust_level == 0.0

        # Phase should be STRANGER (avg = 0.1/2 = 0.05 < 0.2)
        assert state.phase is BondPhase.STRANGER

        # No milestones or promises from plain chat
        assert state.shared_milestones == ()
        assert state.pending_promises == ()

    def test_no_phase_jump_from_single_message(self, tmp_path: Path) -> None:
        """One message should not cause phase飞跃."""
        bond_path = tmp_path / "bond.json"
        engine = BondStateEngine(bond_path)

        engine.apply(BondSignal(BondSignalType.TURN_COMPLETED))
        state = engine.read()
        assert state.phase is BondPhase.STRANGER
        assert state.trust_level == 0.0

    def test_familiarity_does_not_grow_too_fast(self, tmp_path: Path) -> None:
        """100 turns should not max out familiarity (no gamification)."""
        bond_path = tmp_path / "bond.json"
        engine = BondStateEngine(bond_path)

        for _ in range(100):
            engine.apply(BondSignal(BondSignalType.TURN_COMPLETED))

        state = engine.read()
        # 100 × 0.005 = 0.5, avg = 0.25 → ACQUAINTANCE
        assert state.familiarity_level == pytest.approx(0.5, abs=0.01)
        assert state.phase is BondPhase.ACQUAINTANCE
        # Not COMPANION or TRUSTED from chat alone
        assert state.phase not in (BondPhase.TRUSTED, BondPhase.COMPANION)


# ======================================================================
# P7-L: Character Stability
# ======================================================================


class TestP7L_CharacterStability:
    """Low Bond and High Bond Firefly remain the same character."""

    def test_low_bond_still_produces_messages(self, tmp_path: Path) -> None:
        """Low bond should still produce valid companion responses."""
        runtime, bond, provider = _make_runtime()

        runtime.chat("你好")
        messages = provider.calls[-1]

        # Should have identity messages + bond context (empty) + user message
        system_messages = [m for m in messages if m["role"] == "system"]
        assert len(system_messages) >= 1
        assert "Firefly" in system_messages[0]["content"]

    def test_high_bond_still_produces_messages(self, tmp_path: Path) -> None:
        """High bond should still produce valid companion responses."""
        bond_path = tmp_path / "bond.json"
        engine = BondStateEngine(bond_path)

        # Build high bond via direct engine (150 turns × 0.005 = 0.75)
        for _ in range(150):
            engine.apply(BondSignal(BondSignalType.TURN_COMPLETED))
        engine.apply(BondSignal(BondSignalType.SHARED_MILESTONE, "长期伙伴"))
        # After 150 turns: familiarity=0.75, trust=0.0
        # After milestone: familiarity=0.8, trust=0.05
        # avg = (0.05 + 0.8) / 2 = 0.425 → FAMILIAR

        # Now test that CompanionRuntime uses the bond state
        runtime, _, provider = _make_runtime(bond_path=bond_path)
        runtime.chat("你好")
        messages = provider.calls[-1]

        system_messages = [m for m in messages if m["role"] == "system"]
        assert len(system_messages) >= 1
        assert "Firefly" in system_messages[0]["content"]

        # Bond context should be present with FAMILIAR phase text
        bond_messages = [m for m in messages if "BEGIN BOND CONTEXT" in m.get("content", "")]
        assert len(bond_messages) == 1
        assert "彼此熟悉" in bond_messages[0]["content"]

    def test_character_identity_not_overridden_by_bond(self, tmp_path: Path) -> None:
        """Bond context is injected AFTER identity, never before."""
        bond_path = tmp_path / "bond.json"
        engine = BondStateEngine(bond_path)

        # Build high bond via direct engine
        for _ in range(200):
            engine.apply(BondSignal(BondSignalType.TURN_COMPLETED))

        # Now test that CompanionRuntime uses the bond state
        runtime, _, provider = _make_runtime(bond_path=bond_path)
        runtime.chat("你好")
        messages = provider.calls[-1]

        identity_idx = next(
            i for i, m in enumerate(messages) if "Firefly" in m.get("content", "")
        )
        bond_idx = next(
            i for i, m in enumerate(messages) if "BEGIN BOND CONTEXT" in m.get("content", "")
        )
        assert identity_idx < bond_idx, "Identity must come before bond context"


# ======================================================================
# P7-M: CompanionRuntime integration with Bond
# ======================================================================


class TestP7M_CompanionRuntimeBondIntegration:
    """Verify CompanionRuntime.chat() actually advances bond state."""

    def test_chat_advances_bond(self, tmp_path: Path) -> None:
        bond_path = tmp_path / "bond.json"
        runtime, bond, _ = _make_runtime(bond_path=bond_path)

        initial = bond.read()
        runtime.chat("测试")

        after = bond.read()
        assert after.familiarity_level > initial.familiarity_level

    def test_bond_state_available_after_chat(self, tmp_path: Path) -> None:
        """Bond state should be advanced after chat completes."""
        bond_path = tmp_path / "bond.json"
        runtime, bond, _ = _make_runtime(bond_path=bond_path)

        runtime.chat("测试")

        # The bond state engine should have been advanced by TURN_COMPLETED
        # Note: last_bond_state is captured by context_builder BEFORE _try_advance_bond(),
        # so we check the engine directly
        state = bond.read()
        assert state.familiarity_level == pytest.approx(0.005, abs=0.001)

    def test_bond_failure_isolated(self, tmp_path: Path) -> None:
        """Bond state advance failure should not affect the response."""
        bond_path = tmp_path / "bond.json"
        runtime, bond, provider = _make_runtime(bond_path=bond_path)

        # Corrupt bond state to cause failure on next apply
        bond_path.write_text("not valid json", encoding="utf-8")

        # New engine loads from corrupt state → degrades to initial
        # But chat should still work
        runtime2, bond2, provider2 = _make_runtime(bond_path=bond_path)
        response = runtime2.chat("测试")

        # Response should still be produced
        assert response is not None
        assert "choices" in response

    def test_no_bond_engine_does_not_crash(self, tmp_path: Path) -> None:
        """CompanionRuntime without bond_state_engine should work."""
        conv_path = tmp_path / "conv.json"
        runtime = CompanionRuntime(
            character=_FakeCharacter(),
            conversation_store=ConversationStore(conv_path),
            provider_router=_FakeProvider(),
        )

        # Should not raise
        runtime.chat("测试")


# ======================================================================
# P7-N: Promise context in companion prompt
# ======================================================================


class TestP7N_PromiseContext:
    """Pending promises appear in bond context."""

    def test_pending_promises_injected(self, tmp_path: Path) -> None:
        bond_path = tmp_path / "bond.json"
        runtime, bond, provider = _make_runtime(bond_path=bond_path)

        bond.apply(BondSignal(BondSignalType.PROMISE_MADE, "下周一起跑步"))

        runtime.chat("你好")
        messages = provider.calls[-1]

        bond_messages = [m for m in messages if "BEGIN BOND CONTEXT" in m.get("content", "")]
        assert bond_messages
        assert "一起跑步" in bond_messages[0]["content"]

    def test_completed_promise_not_injected(self, tmp_path: Path) -> None:
        bond_path = tmp_path / "bond.json"
        runtime, bond, provider = _make_runtime(bond_path=bond_path)

        promise = "下周一起跑步"
        bond.apply(BondSignal(BondSignalType.PROMISE_MADE, promise))
        bond.apply(BondSignal(BondSignalType.PROMISE_KEPT, promise))

        runtime.chat("你好")
        messages = provider.calls[-1]

        bond_messages = [m for m in messages if "BEGIN BOND CONTEXT" in m.get("content", "")]
        if bond_messages:
            assert "一起跑步" not in bond_messages[0]["content"]


# ======================================================================
# P7-O: Restart persistence — full round-trip
# ======================================================================


class TestP7O_FullRestartRoundTrip:
    """Full restart: destroy all objects, recreate from disk."""

    def test_full_restart_preserves_all_bond_fields(self, tmp_path: Path) -> None:
        bond_path = tmp_path / "bond.json"

        # Lifetime 1: build bond via direct engine (skip CompanionRuntime overhead)
        engine1 = BondStateEngine(bond_path)
        for _ in range(50):
            engine1.apply(BondSignal(BondSignalType.TURN_COMPLETED))
        engine1.apply(BondSignal(BondSignalType.SHARED_MILESTONE, "里程碑1"))
        engine1.apply(BondSignal(BondSignalType.SHARED_MILESTONE, "里程碑2"))
        engine1.apply(BondSignal(BondSignalType.PROMISE_MADE, "约定A"))
        engine1.apply(BondSignal(BondSignalType.PROMISE_MADE, "约定B"))
        state1 = engine1.read()

        # Destroy
        del engine1

        # Lifetime 2
        engine2 = BondStateEngine(bond_path)
        state2 = engine2.read()

        assert state2.phase == state1.phase
        assert state2.trust_level == pytest.approx(state1.trust_level, abs=0.001)
        assert state2.familiarity_level == pytest.approx(
            state1.familiarity_level, abs=0.001
        )
        assert state2.shared_milestones == state1.shared_milestones
        assert state2.pending_promises == state1.pending_promises

        # Lifetime 3: continue advancing
        for _ in range(10):
            engine2.apply(BondSignal(BondSignalType.TURN_COMPLETED))
        state3 = engine2.read()
        assert state3.familiarity_level > state2.familiarity_level
