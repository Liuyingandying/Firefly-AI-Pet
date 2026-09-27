# -*- coding: utf-8 -*-
"""PlayVoiceButton: 聊天消息上的"🔊 播放"按钮 (v1.3)。

状态机: 🔊 播放 → ⏳ 播放中…(禁用) → ⏹ 停止 → 🔊 播放
- 点击播放: 文本经 remove_action_text 清洗后调 VoiceClient.speak (daemon 线程)
- 再次点击: 调 /voice/queue/stop 打断当前语音
- 线程结果经 Signal 回 UI 线程更新按钮, 任何失败静默回退并写入 tooltip
"""

from __future__ import annotations

import threading
from typing import Callable

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QPushButton

from voice_client.client import FireflyVoiceClient, get_shared_client
from voice_client.text_cleaner import remove_action_text

_IDLE = "🔊 播放"
_BUSY = "⏳ 播放中…"
_PLAYING = "⏹ 停止"


class _Relay(QObject):
    done = Signal(object)


class PlayVoiceButton(QPushButton):
    def __init__(self, get_text: Callable[[], str], client: FireflyVoiceClient | None = None, parent=None):
        super().__init__(_IDLE, parent)
        self._get_text = get_text
        self._client = client or get_shared_client()
        self._state = "idle"
        self._op: str | None = None        # 当前在途操作: "speak" | "stop"
        self._gen = 0                      # v1.3.1: 操作代际, 晚到的旧结果直接丢弃
        self._relay = _Relay()
        self._relay.done.connect(self._on_done)
        self.setToolTip("朗读这条回复的对白 (自动跳过动作括号)")
        self.setStyleSheet(
            "QPushButton { padding: 2px 10px; border: none; background: transparent;"
            " color: rgba(122,92,255,220); font-size: 12pt; }"
            "QPushButton:hover { background: rgba(122,92,255,30); border-radius: 8px; }"
            "QPushButton:disabled { color: rgba(150,140,170,160); }"
        )
        self.clicked.connect(self._on_clicked)

    # ------------------------------------------------------------------

    def _set(self, state: str, label: str, enabled: bool = True, tip: str = "") -> None:
        self._state = state
        self.setText(label)
        self.setEnabled(enabled)
        if tip:
            self.setToolTip(tip)

    def _on_clicked(self) -> None:
        if self._state == "busy":
            # v1.3.1: 合成等待期再次点击 = 请求停止 (取消会话), 不必等合成结束
            if self._op == "speak" and not getattr(self, "_stop_sent", False):
                self._stop_sent = True
                self._op = "stop"
                self._gen += 1                 # 使在途 speak 结果失效
                gen = self._gen
                self.setText("⏳ 停止中…")
                self.setEnabled(False)
                self._run(gen, self._client.stop)
            return
        self._gen += 1
        gen = self._gen
        self._stop_sent = False
        if self._state == "playing":
            self._set("busy", _BUSY, enabled=False)
            self._op = "stop"
            self._run(gen, self._client.stop)
            return
        speakable = remove_action_text(self._get_text() or "")
        if not speakable:
            self._set("idle", _IDLE, tip="这条回复没有可朗读的对白")
            return
        self._set("busy", _BUSY, enabled=True, tip="合成中, 再次点击可停止")   # 保持可点: 合成中也能请求停止
        self._op = "speak"
        self._run(gen, lambda: self._client.speak(speakable))

    def _run(self, gen: int, fn) -> None:
        def _work():
            result = {"ok": False, "reason": "error"}
            try:
                result = fn()
            except Exception as exc:  # noqa: BLE001 — client 已不抛, 双保险
                result = {"ok": False, "reason": "error", "detail": repr(exc)}
            self._relay.done.emit((gen, result))

        threading.Thread(target=_work, name="firefly-play-button", daemon=True).start()

    def _on_done(self, payload: object) -> None:
        gen, result = payload if isinstance(payload, tuple) else (self._gen, payload)
        if gen != self._gen:
            return  # v1.3.1: 旧操作(如停止前的 speak)晚到结果 — 丢弃, 不翻转 UI
        op, self._op = self._op, None
        ok = bool(isinstance(result, dict) and result.get("ok"))
        if op == "stop":
            self._set("idle", _IDLE)
            return
        if ok:
            self._set("playing", _PLAYING)
        else:
            reason = result.get("reason", "error") if isinstance(result, dict) else "error"
            self._set("idle", _IDLE, tip=f"语音不可用 ({reason})")
