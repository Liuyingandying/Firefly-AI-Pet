# -*- coding: utf-8 -*-
"""VoiceAnnouncer: 监听 Agent FINAL 事件 → 后台线程朗读。

挂在 app.py 的 character_conversation.agent_event 信号上 (主线程收到事件),
HTTP 全部在 daemon 线程发出, 任何失败只留日志, UI 零感知。
"""

from __future__ import annotations

import logging
import threading

from core.agent_events import AgentEvent, AgentEventType
from voice_client.client import FireflyVoiceClient
from voice_client.text_cleaner import remove_action_text

log = logging.getLogger("firefly.voice")


class VoiceAnnouncer:
    def __init__(self, client: FireflyVoiceClient | None = None):
        self.client = client or FireflyVoiceClient()
        self._spinning = set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------

    def attach(self, runner) -> None:
        """连接 CharacterConversationRunner.agent_event 信号 (PySide6 可连任意 callable)。"""
        runner.agent_event.connect(self.on_agent_event)
        log.info("voice announcer attached (enabled=%s url=%s)", self.client.config.enabled, self.client.config.url)

    def on_agent_event(self, event: AgentEvent) -> None:
        et = getattr(event, "type", None)
        if et == AgentEventType.FINAL:
            if not self.client.config.auto_play:
                return  # v1.3: 默认只显示播放按钮, 由 UI 点击触发
            text = (getattr(event, "text", "") or "").strip()
            speakable = remove_action_text(text)  # 只朗读对白
            if speakable:
                self._spawn("speak", lambda: self.client.speak(speakable))
        elif et == AgentEventType.CANCELLED:
            self._spawn("stop", self.client.stop)

    # ------------------------------------------------------------------

    def _spawn(self, tag: str, fn) -> None:
        def _run():
            with self._lock:
                self._spinning.add(tag)
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 — 双保险, client 已不抛
                log.warning("voice %s failed: %s", tag, exc)
            finally:
                with self._lock:
                    self._spinning.discard(tag)

        threading.Thread(target=_run, name=f"firefly-voice-{tag}", daemon=True).start()
