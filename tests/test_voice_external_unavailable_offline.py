import urllib.error

from voice_client.client import FireflyVoiceClient
from voice_client.config import VoiceConfig


def test_enabled_external_voice_unavailable_is_graceful(monkeypatch):
    client = FireflyVoiceClient(
        VoiceConfig(enabled=True, url="http://127.0.0.1:9", read_timeout_s=0.1)
    )
    calls = 0

    def unavailable(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise urllib.error.URLError("offline test stub")

    monkeypatch.setattr("urllib.request.urlopen", unavailable)

    result = client.speak("offline voice fallback")
    chat_flow_continued = True

    assert result["ok"] is False
    assert result["reason"] == "unavailable"
    assert calls == 1
    assert chat_flow_continued is True
