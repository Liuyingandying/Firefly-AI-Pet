"""Lightweight Tianjin University ``tju-llm`` availability probe.

This module answers exactly one question: *can Firefly's configured Tianjin
University OpenAI-compatible API (model ``tju-llm``) be called right now?*

It deliberately has NOTHING to do with the Qwen CLI — the dot next to the
Qwen launcher is not about whether ``qwen`` is installed, can start, is
running, or works locally. Those facts never influence the status: a TJU 500
is RED even when the Qwen CLI opens fine, and a healthy ``tju-llm`` response
is GREEN even while no Qwen process exists.

Reuse, not reinvention: the probe goes through the existing
``providers.tju_qwen.TJUQwenProvider`` (``TJULLM_API_KEY`` /
``TJULLM_BASE_URL`` / ``TJULLM_MODEL`` resolution from env then .env, endpoint
normalization) and its stdlib OpenAI-compatible transport, which already
classifies HTTP errors / timeouts / transport failures. No new secret is
created anywhere. The failure policy (consecutive failures open a cooldown
window, then one half-open probe) mirrors
``core.screen_vision.circuit_breaker`` with a small local copy so the dock
never drags in the heavy vision import chain.

Cost: one tiny non-streaming chat request, prompt ``ping``, ``max_tokens=1``.
No screenshot, no memory, no conversation history, no workspace content, no
role card, no file contents, and the API key is never written to logs or UI.

Threading: probing runs on a daemon worker thread with a per-start stop event;
results are published to the Qt main thread through the ``status_changed``
signal (auto-queued), so the UI thread is never blocked and there is no
QThread lifecycle coupling.
"""

from __future__ import annotations

import logging
import threading
import time

from PySide6.QtCore import QObject, Signal

from providers.base import (
    ProviderError,
    ProviderHTTPError,
    ProviderProtocolError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    default_transport,
)
from providers.tju_qwen import DEFAULT_MODEL, TJUQwenProvider

logger = logging.getLogger(__name__)


class TjuApiStatus:
    """Four-state availability of the configured TJU ``tju-llm`` API.

    ``NOT_CONFIGURED`` = no key/endpoint/model set (a configuration state,
    not an outage) — the dock hides the dot for this state.
    """

    UNKNOWN = "unknown"
    NOT_CONFIGURED = "not_configured"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


PROBE_PROMPT = "ping"
PROBE_MAX_TOKENS = 1
PROBE_TIMEOUT_SECONDS = 6.0
DEFAULT_INITIAL_DELAY_SECONDS = 5.0
DEFAULT_INTERVAL_SECONDS = 90.0

# Policy mirrors core/screen_vision/circuit_breaker.py (kept local so the dock
# does not import the vision package; nothing TJU-specific existed to reuse).
BREAKER_FAILURE_THRESHOLD = 2
BREAKER_COOLDOWN_SECONDS = 60.0


class _Breaker:
    """Consecutive-failure cooldown gate.

    After ``threshold`` consecutive failures the breaker opens for
    ``cooldown_seconds``; while open, callers skip probes entirely. After the
    cooldown a single probe is allowed — success closes the breaker, failure
    re-opens it.
    """

    def __init__(self, threshold: int, cooldown_seconds: float, clock=time.monotonic):
        self._threshold = threshold
        self._cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._opened_at = None

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._opened_at is not None and (
                self._clock() - self._opened_at < self._cooldown_seconds
            )

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._threshold:
                self._opened_at = self._clock()


def probe_tju_llm(provider, *, transport=None, timeout=PROBE_TIMEOUT_SECONDS):
    """Send one minimal ``tju-llm`` chat probe and return the raw response.

    Raises :class:`providers.base.ProviderError` (or a transport OSError) on
    failure; callers map exceptions to a status. The API key is used only in
    the request ``Authorization`` header and never logged or returned.
    """
    if not provider.api_key:
        raise ProviderUnavailableError("tju API key is not configured")
    if not provider.endpoint:
        raise ProviderUnavailableError("tju endpoint is not configured")
    if not provider.default_model:
        raise ProviderUnavailableError("tju model is not configured")

    send = transport or getattr(provider, "_transport", None) or default_transport
    payload = {
        "model": provider.default_model,
        "messages": [{"role": "user", "content": PROBE_PROMPT}],
        "max_tokens": PROBE_MAX_TOKENS,
        "temperature": 0.0,
        "stream": False,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {provider.api_key}",
    }
    return send(provider.endpoint, payload, headers, timeout)


def classify_probe_error(exc: Exception) -> str:
    """Map a probe failure to a status.

    Every failure family the dock must show as unavailable — HTTP 429, 5xx,
    401/403, timeout, network failure, malformed response, missing config —
    maps to ``unavailable``. Only a valid completion produces ``available``,
    so anything else is treated as clearly not callable rather than unknown.
    """
    if isinstance(
        exc,
        (
            ProviderHTTPError,
            ProviderTimeoutError,
            ProviderUnavailableError,
            ProviderProtocolError,
            ProviderError,
            OSError,
            TimeoutError,
        ),
    ):
        return TjuApiStatus.UNAVAILABLE
    return TjuApiStatus.UNAVAILABLE


# 模块级最近探测结论：checker 实例归 dock 所有，而控制台状态行也需要
# 读取；跨实例共享一份最近结论即可（只在 _set_status 中写入）。
_LAST_PROBE_STATUS = ""


def last_probe_status() -> str:
    """最近一次 TJU 健康探测结论（TjuApiStatus 值；尚未探测过为空串）。"""
    return _LAST_PROBE_STATUS


class TjuLlmHealthChecker(QObject):
    """Background TJU ``tju-llm`` availability probe with a thread-safe status.

    Initial state is ``unknown``. ``start()`` waits a few seconds for the
    first probe, then probes roughly every ``interval`` seconds; a probe is
    skipped while the breaker is open. ``stop()`` signals the worker to exit
    and joins it briefly (daemon fallback keeps shutdown unblocked).
    """

    status_changed = Signal(str)

    def __init__(
        self,
        *,
        provider=None,
        provider_factory: Callable[[], Any] | None = None,
        transport=None,
        initial_delay: float = DEFAULT_INITIAL_DELAY_SECONDS,
        interval: float = DEFAULT_INTERVAL_SECONDS,
        timeout: float = PROBE_TIMEOUT_SECONDS,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._provider = provider if provider is not None else TJUQwenProvider()
        # 每次探测重建 Provider：凭据在构造时解析，旧实例感知不到
        # Provider Manager 后来保存的密钥（红点不熄灭的根因）。
        self._provider_factory = provider_factory
        self._transport = transport
        self._timeout = float(timeout)
        self._initial_delay = float(initial_delay)
        self._interval = max(10.0, float(interval))
        self._breaker = _Breaker(BREAKER_FAILURE_THRESHOLD, BREAKER_COOLDOWN_SECONDS)
        self._status = TjuApiStatus.UNKNOWN
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None

    def _current_provider(self):
        if self._provider_factory is not None:
            try:
                return self._provider_factory()
            except Exception:
                return self._provider
        return self._provider

    @property
    def status(self) -> str:
        with self._lock:
            return self._status

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            stop_event = threading.Event()
            self._stop_event = stop_event
            self._thread = threading.Thread(
                target=self._run_loop,
                args=(stop_event,),
                name="firefly-tju-llm-health",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            stop_event = self._stop_event
            thread = self._thread
            self._thread = None
        if stop_event is not None:
            stop_event.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def _run_loop(self, stop_event: threading.Event) -> None:
        stop_event.wait(self._initial_delay)
        while not stop_event.is_set():
            self._probe_once()
            stop_event.wait(self._interval)

    def _probe_once(self) -> None:
        if self._breaker.is_open:
            return  # breaker open: reuse its state, send no extra probe
        provider = self._current_provider()
        # 未配置密钥/端点/模型 = 配置状态而非故障：不进熔断器，
        # 点直接隐藏（红点只保留给"配置了但探测失败"）。
        if not provider.api_key or not provider.endpoint or not provider.default_model:
            self._set_status(TjuApiStatus.NOT_CONFIGURED)
            return
        try:
            probe_tju_llm(
                provider,
                transport=self._transport,
                timeout=self._timeout,
            )
        except Exception as exc:
            self._breaker.record_failure()
            self._set_status(classify_probe_error(exc))
            logger.debug("tju/tju-llm probe unavailable: %s", _log_label(exc))
        else:
            self._breaker.record_success()
            self._set_status(TjuApiStatus.AVAILABLE)

    def probe_now(self) -> None:
        """事件驱动的立即探测：重置熔断器后在后台线程执行一次。

        供凭据变更（providers.updated）等场景调用——保存密钥后 1~2 秒内
        状态点即可更新，无需等下一轮 90 秒周期。
        """
        with self._lock:
            self._breaker.record_success()  # 重置熔断，允许立即探测
        threading.Thread(
            target=self._probe_once, daemon=True, name="firefly-tju-probe-now"
        ).start()

    def _set_status(self, status: str) -> None:
        global _LAST_PROBE_STATUS
        with self._lock:
            changed = status != self._status
            self._status = status
            _LAST_PROBE_STATUS = status
        if changed:
            # Emitting from the worker thread is safe: the receiver (dock)
            # lives on the main thread, so Qt auto-queues delivery there.
            self.status_changed.emit(status)


def _log_label(exc: Exception) -> str:
    """Secret-free label for log lines (status code only, never the payload)."""
    if isinstance(exc, ProviderHTTPError):
        return f"ProviderHTTPError(status={exc.status_code})"
    return type(exc).__name__
