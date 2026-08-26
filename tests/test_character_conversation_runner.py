"""Offline UI-runtime integration tests for Phase 2-P3."""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app import VisualShell
from core.agent_events import AgentEvent, AgentEventType
from core.conversation_runtime import ConversationRuntime
from memory.memory_manager import MemoryManager
from memory.repository import JsonMemoryRepository
from ui.character_conversation_runner import CharacterConversationRunner
from ui.short_ask import ShortAskPanel, ShortTalkState


class FakeMemoryClient:
    def __init__(self) -> None:
        self.memories: list[dict[str, Any]] = []
        self.add_calls = 0

    def add(
        self, content: str, metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.add_calls += 1
        item = {"memory": content, "metadata": dict(metadata or {})}
        self.memories.append(item)
        return {"results": [item]}

    def search(self, query: str) -> list[dict[str, Any]]:
        return list(self.memories)


class CharacterAwareProvider:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        self.calls.append(messages)
        user_text = messages[-1]["content"]
        if user_text == "你是谁？":
            answer = "我是流萤，Firefly AI Pet 的桌面 AI 伙伴。"
        else:
            answer = "你最近在开发 Firefly AI Pet。"
        return {
            "provider": "fake",
            "choices": [{"message": {"role": "assistant", "content": answer}}],
        }


def _runner(tmp_path=None) -> tuple[
    CharacterConversationRunner,
    MemoryManager,
    FakeMemoryClient,
    CharacterAwareProvider,
]:
    client = FakeMemoryClient()
    repository = (
        JsonMemoryRepository(tmp_path / "records.json")
        if tmp_path is not None
        else None
    )
    manager = MemoryManager(client=client, repository=repository)
    provider = CharacterAwareProvider()
    runtime = ConversationRuntime(manager, provider)
    return CharacterConversationRunner(runtime), manager, client, provider


def test_who_are_you_uses_character_prompt_and_returns_firefly_reply() -> None:
    runner, _, client, provider = _runner()

    events, text = runner.perform("你是谁？")

    assert events[-1].type == AgentEventType.FINAL
    assert text == "我是流萤，Firefly AI Pet 的桌面 AI 伙伴。"
    assert "CHARACTER IDENTITY" in provider.calls[0][0]["content"]
    assert "流萤" in provider.calls[0][0]["content"]
    assert provider.calls[0][-1] == {"role": "user", "content": "你是谁？"}
    assert client.add_calls == 0


def test_recent_project_reads_manual_memory_into_independent_context(tmp_path) -> None:
    runner, manager, client, provider = _runner(tmp_path)
    memory = "我正在开发 Firefly AI Pet，希望它成为长期 AI Companion。"
    manager.add_memory(memory, {"category": "project", "source": "manual"})

    events, text = runner.perform("我最近在做什么？")

    assert events[-1].type == AgentEventType.FINAL
    assert text == "你最近在开发 Firefly AI Pet。"
    messages = provider.calls[0]
    assert [message["role"] for message in messages] == [
        "system",
        "system",
        "system",
        "user",
    ]
    assert "BEGIN MEMORY CONTEXT" in messages[2]["content"]
    assert memory in messages[2]["content"]
    assert memory not in messages[0]["content"]
    assert memory not in messages[1]["content"]
    assert client.add_calls == 1


def test_empty_memory_still_returns_through_existing_event_contract() -> None:
    runner, _, client, _ = _runner()

    events, text = runner.perform("你是谁？")

    assert [event.type for event in events] == [AgentEventType.FINAL]
    assert text
    assert client.add_calls == 0


def test_firefly_short_ask_bypasses_agent_router() -> None:
    prompts: list[str] = []
    shell = SimpleNamespace(
        short_ask=SimpleNamespace(running=False, agent="firefly"),
        _do_character_ask=prompts.append,
        agent_router=SimpleNamespace(
            recommend=lambda _request: (_ for _ in ()).throw(
                AssertionError("AgentRouter must not receive character chat")
            )
        ),
    )

    VisualShell._on_short_ask_send(shell, "你是谁？")

    assert prompts == ["你是谁？"]


def test_bubble_enters_running_state_before_background_request_starts() -> None:
    order: list[str] = []

    class FakePanel:
        def set_running(self, _message: str) -> None:
            order.append("ui-running")

        def reset_with_note(self, _message: str) -> None:
            order.append("ui-reset")

    class FakeConversation:
        def ask(self, _prompt: str) -> bool:
            order.append("runtime-ask")
            return True

    shell = SimpleNamespace(
        short_ask=FakePanel(),
        character_conversation=FakeConversation(),
        _last_short_ask_prompt="",
    )

    VisualShell._do_character_ask(shell, "你是谁？")

    assert order == ["ui-running", "runtime-ask"]


def test_existing_short_ask_bubble_displays_firefly_reply() -> None:
    application = QApplication.instance() or QApplication([])
    panel = ShortAskPanel()
    panel.show_input("firefly")
    panel.set_running("Thinking…")

    panel.on_agent_event(
        AgentEvent.make(
            "firefly",
            AgentEventType.FINAL,
            text="我是流萤，Firefly AI Pet 的桌面 AI 伙伴。",
        )
    )
    application.processEvents()

    assert panel.state == ShortTalkState.COMPLETE
    assert panel.full_answer() == "我是流萤，Firefly AI Pet 的桌面 AI 伙伴。"
    assert panel._title.text() == "Ask Firefly"
    panel.close()
