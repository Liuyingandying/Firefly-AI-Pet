"""Offline integration tests for PageLens -> Desktop AI Router RPC."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication

from core.ai_router import ProviderRouter
from core.pagelens_bridge import PageLensBridge
from providers.base import BaseProvider, ProviderUnavailableError


MESSAGES = [{"role": "user", "content": "explain phase margin"}]


def _app() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


def _completion(provider: str, content: str) -> dict[str, Any]:
    return {
        "provider": provider,
        "choices": [{"message": {"role": "assistant", "content": content}}],
    }


class FakeProvider(BaseProvider):
    def __init__(self, name: str, *, content: str = "", error: Exception | None = None):
        self.name = name
        self.content = content
        self.error = error

    def chat(self, messages, model=None, temperature=0.2, *, timeout=None):
        if self.error is not None:
            raise self.error
        return _completion(self.name, self.content)


async def _run_request(bridge: PageLensBridge, request: dict) -> list[dict]:
    sent: list[dict] = []
    connection = object()
    bridge._writer = connection
    bridge._send_json = lambda message: sent.append(message)
    request_id, source, messages, temperature, error = bridge._validate_ai_chat_request(
        request
    )
    assert not error
    await bridge._run_ai_chat(
        request_id,
        source,
        messages,
        temperature,
        connection,
    )
    return sent


def test_response_preserves_request_id() -> None:
    _app()
    bridge = PageLensBridge(
        chat_handler=lambda messages, **kwargs: _completion("tju", "answer")
    )
    sent = asyncio.run(_run_request(bridge, {
        "type": "ai_chat_request",
        "requestId": "request-123",
        "source": "explain",
        "messages": MESSAGES,
        "temperature": 0.25,
    }))

    assert sent == [{
        "type": "ai_chat_response",
        "requestId": "request-123",
        "provider": "tju",
        "content": "answer",
    }]


def test_tju_failure_falls_back_to_glm(tmp_path: Path) -> None:
    _app()
    router = ProviderRouter(
        [
            FakeProvider("tju", error=ProviderUnavailableError("offline")),
            FakeProvider("zhipu", content="glm answer"),
        ],
        state_path=tmp_path / "provider_state.json",
    )
    bridge = PageLensBridge(chat_handler=router.chat)
    sent = asyncio.run(_run_request(bridge, {
        "type": "ai_chat_request",
        "requestId": "fallback-1",
        "source": "concept_card",
        "messages": MESSAGES,
        "temperature": 0.25,
    }))

    assert sent[0]["requestId"] == "fallback-1"
    assert sent[0]["provider"] == "zhipu"
    assert sent[0]["content"] == "glm answer"


def test_disconnect_discards_late_response() -> None:
    _app()
    entered = threading.Event()
    release = threading.Event()

    def slow_chat(messages, **kwargs):
        entered.set()
        assert release.wait(timeout=2)
        return _completion("tju", "late answer")

    bridge = PageLensBridge(chat_handler=slow_chat)
    sent: list[dict] = []
    connection = object()
    bridge._writer = connection
    bridge._send_json = lambda message: sent.append(message)

    async def scenario() -> None:
        task = asyncio.create_task(bridge._run_ai_chat(
            "late-1", "follow_up", MESSAGES, 0.5, connection
        ))
        assert await asyncio.to_thread(entered.wait, 2)
        bridge._writer = None  # WebSocket disconnected while chat was running.
        release.set()
        await task

    asyncio.run(scenario())
    assert sent == []


def test_request_validation_errors_keep_request_id() -> None:
    _app()
    bridge = PageLensBridge(chat_handler=lambda *_args, **_kwargs: {})
    sent: list[dict] = []
    bridge._writer = object()
    bridge._send_json = lambda message: sent.append(message)
    bridge._schedule_ai_chat({
        "type": "ai_chat_request",
        "requestId": "invalid-1",
        "source": "explain",
        "messages": "not-a-list",
        "temperature": 0.2,
    })
    assert sent[0]["type"] == "ai_chat_error"
    assert sent[0]["requestId"] == "invalid-1"
