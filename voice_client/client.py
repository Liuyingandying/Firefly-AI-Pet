# -*- coding: utf-8 -*-
"""FireflyVoiceClient — Voice Module HTTP 客户端 (永不抛异常)。"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from voice_client.config import VoiceConfig, load_voice_config

log = logging.getLogger("firefly.voice")


class FireflyVoiceClient:
    """speak(text, emotion, priority) → Voice Module /voice/speak (play=true, 边转边播)。"""

    def __init__(self, config: VoiceConfig | None = None):
        self._explicit_config = config
        if config is None:
            # 预热：触发管理链解析与一次性迁移；后续经 mtime 缓存命中
            load_voice_config()

    @property
    def config(self) -> VoiceConfig:
        """显式注入时返回注入值；否则每次重解析（mtime 缓存）。

        这让 firefly_voice 插件的开关/自动播放设置**无需重启**即可生效，
        同时保持显式构造（测试/高级用法）的原有语义。
        """
        if self._explicit_config is not None:
            return self._explicit_config
        return load_voice_config()

    # ------------------------------------------------------------------

    def speak(self, text: str, emotion: str | None = None, priority: int = 1) -> dict[str, Any]:
        """朗读一段回复。失败时返回 {"ok": False, "reason": ...}, 从不抛出。"""
        cfg = self.config
        if not cfg.enabled:
            return {"ok": False, "reason": "disabled"}
        text = (text or "").strip()
        if not text:
            return {"ok": False, "reason": "empty"}
        if len(text) > cfg.max_speak_chars:
            # 长回复不跳过, 截断朗读开头 (检索/文档类长答案只读前段, 可 config 关闭)
            log.info("回复长度 %d 超过 max_speak_chars=%d, 截断朗读", len(text), cfg.max_speak_chars)
            text = text[: cfg.max_speak_chars]

        payload: dict[str, Any] = {"text": text, "play": True, "priority": int(priority)}
        if cfg.emotion_enabled:
            # emotion=None 时由 Voice Module 关键词自动推断
            if emotion:
                payload["emotion"] = emotion
        else:
            payload["emotion"] = "neutral"

        status = self._post_json("/voice/speak", payload)
        if not status["ok"]:
            # 面向用户可见的唯一提示口径
            log.warning("voice unavailable (%s): %s", status["reason"], status.get("detail", ""))
        else:
            log.info("voice speak ok: emotion=%s segments=%s", status.get("emotion"), status.get("segments"))
        return status

    def stop(self) -> dict[str, Any]:
        """打断当前语音 (响应用户取消回复)。"""
        if not self.config.enabled:
            return {"ok": False, "reason": "disabled"}
        return self._post_json("/voice/queue/stop", {})

    # ------------------------------------------------------------------

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        cfg = self.config
        url = cfg.url + path
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            # urllib 的 timeout 是单一数值 (整体请求上限); 回环端口拒绝连接瞬时失败,
            # 该超时主要约束"服务在线但合成慢"的读等待。
            with urllib.request.urlopen(request, timeout=cfg.read_timeout_s) as resp:
                raw = resp.read()
                if resp.status != 200:
                    return {"ok": False, "reason": f"http_{resp.status}"}
        except urllib.error.HTTPError as exc:
            return {"ok": False, "reason": f"http_{exc.code}", "detail": str(exc.reason)}
        except urllib.error.URLError as exc:
            return {"ok": False, "reason": "unavailable", "detail": str(exc.reason)}
        except TimeoutError:
            return {"ok": False, "reason": "timeout"}
        except Exception as exc:  # noqa: BLE001 — 语音故障永不外溢
            return {"ok": False, "reason": "error", "detail": repr(exc)}

        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {"ok": False, "reason": "bad_response"}
        return {
            "ok": True,
            "emotion": data.get("emotion"),
            "segments": len(data.get("segments") or []),
            "total_ms": data.get("total_ms"),
            "detail": data,
        }


_shared: "FireflyVoiceClient | None" = None


def get_shared_client() -> FireflyVoiceClient:
    """进程级共享客户端 (UI 播放按钮与 Announcer 共用同一配置实例)。"""
    global _shared
    if _shared is None:
        _shared = FireflyVoiceClient()
    return _shared
