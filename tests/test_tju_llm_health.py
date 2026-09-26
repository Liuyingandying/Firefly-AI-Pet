"""TJU 健康探测测试：未配置状态识别、立即探测、熔断交互。

全程使用 stub transport，不发真实网络请求。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from providers.base import ProviderUnavailableError
from ui.tju_llm_health import (
    TjuApiStatus,
    TjuLlmHealthChecker,
    probe_tju_llm,
)


class _Provider:
    """可配置的假 Provider：api_key/endpoint/model 可控。"""

    def __init__(self, api_key="", endpoint="https://x/v1", model="m"):
        self.api_key = api_key
        self.endpoint = endpoint
        self.default_model = model


class _Transport:
    """假传输：返回最小合法 completion。"""

    @staticmethod
    def ok(endpoint, payload, headers, timeout):
        return {"choices": [{"message": {"content": "pong"}}]}

    @staticmethod
    def fail(endpoint, payload, headers, timeout):
        raise OSError("network down")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_probe_raises_when_not_configured():
    with pytest.raises(ProviderUnavailableError):
        probe_tju_llm(_Provider(api_key="", endpoint="https://x", model="m"))


def test_not_configured_status_hides_dot(qapp):
    """未配置密钥 → NOT_CONFIGURED（点隐藏），且不进入熔断器。"""
    checker = TjuLlmHealthChecker(provider=_Provider(api_key=""), initial_delay=0)
    seen = []
    checker.status_changed.connect(lambda s: seen.append(s))
    checker._probe_once()
    assert checker.status == TjuApiStatus.NOT_CONFIGURED
    assert "not_configured" in seen
    checker._probe_once()
    assert checker.status == TjuApiStatus.NOT_CONFIGURED
    checker.stop()


def test_available_status_with_transport(qapp):
    checker = TjuLlmHealthChecker(
        provider=_Provider(api_key="k", endpoint="https://x", model="m"),
        transport=_Transport.ok,
        initial_delay=0,
    )
    checker._probe_once()
    assert checker.status == TjuApiStatus.AVAILABLE
    checker.stop()


def test_unavailable_status_on_transport_failure(qapp):
    checker = TjuLlmHealthChecker(
        provider=_Provider(api_key="k", endpoint="https://x", model="m"),
        transport=_Transport.fail,
        initial_delay=0,
    )
    checker._probe_once()
    assert checker.status == TjuApiStatus.UNAVAILABLE
    checker.stop()


def test_probe_now_forces_immediate_probe(qapp):
    """probe_now：绕过熔断与周期，立即探测一次（事件驱动路径）。"""
    calls = []
    provider = _Provider(api_key="k", endpoint="https://x", model="m")
    checker = TjuLlmHealthChecker(provider=provider, initial_delay=0)
    checker._breaker.record_failure()
    checker._breaker.record_failure()
    assert checker._breaker.is_open  # 已熔断

    # 用假 transport 记录探测发生
    checker._transport = lambda *a, **k: calls.append(1) or {
        "choices": [{"message": {"content": "pong"}}]
    }
    checker.probe_now()  # → _probe_once_forced: reset breaker + probe
    QTest.qWait(300)
    assert checker.status == TjuApiStatus.AVAILABLE
    assert len(calls) == 1  # 熔断已重置，探测真的发生了
    checker.stop()


from PySide6.QtTest import QTest  # noqa: E402  (probe_now 后的等待用)


def test_last_probe_status_module_cache(qapp):
    """_set_status 同步更新模块级缓存，供控制台状态行跨实例读取。"""
    from ui.tju_llm_health import last_probe_status

    checker = TjuLlmHealthChecker(provider=_Provider(api_key=""), initial_delay=0)
    checker._probe_once()
    assert last_probe_status() == TjuApiStatus.NOT_CONFIGURED
    checker.stop()
