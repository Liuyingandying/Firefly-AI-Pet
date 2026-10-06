"""Original Character ownership through actual chat and Camera FAST seams.

Offline synthetic records, injected transport and frame; no live store or camera.
"""
from datetime import datetime
from types import SimpleNamespace
import threading

from character.character_loader import CharacterLoader
from core.companion_config import CompanionConfig
from core.companion_runtime import CompanionRuntime
from core.screen_vision.config import VisionConfig
from core.screen_vision.models import ScreenFrame
from core.screen_vision.service import ScreenVisionService
from core.screen_vision.vision.qwen_vision import QwenVisionProvider
from memory.records import MemoryRecord
from memory.repository import JsonMemoryRepository
from memory.service import MemoryService
from ui.character_conversation_runner import CharacterConversationRunner

RETIRED = ("BEGIN PERSONA", "Evidence Plan", "RELATIONAL_INTERACTION",
           "PersonaBoundary", "Grounded Interaction", "Affordance")


def test_provider_ready_chat_owns_four_character_layers_and_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREFLY_CONVERSATION_PACING", "0")
    character = CharacterLoader().load()
    repository = JsonMemoryRepository(tmp_path / "records.json")
    record = MemoryRecord.create(category="preference", content="请称呼我测试星",
                                 trigger="explicit-command", identity_kind="preferred_name")
    repository.add(record)
    memory = MemoryService.local(repository)
    captured = []
    def chat(messages, **kwargs):
        captured.append(messages)
        return {"choices": [{"message": {"content": "synthetic response"}}]}
    runtime = CompanionRuntime(character=character, memory_service=memory,
                               conversation_store=None, bond_state_engine=None,
                               provider_router=SimpleNamespace(chat=chat),
                               config=CompanionConfig())
    runtime.chat("我是谁呀", history=[{"role": "user", "content": "synthetic prior turn"}])
    messages = captured[0]
    layers = character.to_system_messages()
    assert messages[:len(layers)] == layers
    combined = "\n".join(m["content"] for m in messages[:len(layers)])
    for field in ("identity_prompt", "personality_prompt", "dialogue_policy_prompt", "relationship_policy_prompt"):
        assert getattr(character, field) in combined
    memory_blocks = [m["content"] for m in messages if "<long_term_memory>" in m["content"]]
    assert len(memory_blocks) == 1 and "测试星" in memory_blocks[0]
    assert messages[-2:] == [{"role": "user", "content": "synthetic prior turn"},
                             {"role": "user", "content": "我是谁呀"}]
    assert not any(marker in m["content"] for m in messages for marker in RETIRED)


def test_camera_fast_runner_to_qwen_full_character_system(monkeypatch):
    from core.screen_vision.vision import qwen_vision
    character = CharacterLoader().load()
    captured = []
    def post(url, **kwargs):
        captured.append(kwargs["json"])
        return SimpleNamespace(status_code=200, json=lambda: {
            "choices": [{"message": {"content": "synthetic image description"}}]})
    monkeypatch.setattr(qwen_vision.requests, "post", post)
    monkeypatch.setattr(qwen_vision, "routed_http_call", lambda call, url, task, provider, **kw: call(url, **kw))
    provider = QwenVisionProvider(VisionConfig("https://example.invalid/v1", "test-vision", "fixture"))
    frame = ScreenFrame(8, 8, "image/jpeg", b"synthetic-frame", datetime.now())
    capture = SimpleNamespace(capture_camera=lambda: frame, last_capture_info={})
    service = ScreenVisionService(vision_provider=provider, reasoning_provider=object(),
                                  capture_service=capture, fast_mode=True,
                                  direct_vision_provider=provider)
    runner = CharacterConversationRunner(
        runtime=SimpleNamespace(character=character, conversation_store=None),
        screen_vision_service=service)
    events, _ = runner.perform("看看我", threading.Event())
    assert events and len(captured) == 1
    system = captured[0]["messages"][0]
    assert system == {"role": "system", "content": "\n".join(m["content"] for m in character.to_system_messages())}
    assert not any(marker in system["content"] for marker in RETIRED)


def test_original_four_yaml_bytes():
    from pathlib import Path
    import hashlib
    root = Path(__file__).resolve().parents[1] / "character" / "firefly"
    expected = {
        "identity": "555d50934d6258f11b8d46bec3615ca9e46ee9aa510c2f0059a84201f918878e",
        "personality": "a2a5d8f4cd039a1bc325d0efbd0f828a4e19dd7b1dfc6edd1a5cb558fa67a2b0",
        "dialogue_policy": "30b0201231ddbc1acdb09603bac79dde48aeba30b7b0e9e412070a3ca745cac9",
        "relationship_policy": "055acccec53badafb8a8099b120c8224399df7f904dd69e4d79ad17983769d6a",
    }
    for name, digest in expected.items():
        # Git may check text out as CRLF on Windows; the committed contract is LF.
        data = (root / (name + ".yaml")).read_bytes().replace(b"\r\n", b"\n")
        assert hashlib.sha256(data).hexdigest() == digest
