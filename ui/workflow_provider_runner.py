"""Direct Anthropic-compatible provider runner for Workflow Plan/Review (9D.6-H4).

A :class:`QObject` transport that POSTs a non-streaming Anthropic-compatible
Messages request straight to the provider endpoint, off the GUI thread, and
re-emits the outcome as the same neutral :class:`AgentEvent` stream the rest of
the app already consumes (STARTED / STATUS / FINAL / ERROR / CANCELLED).

It never touches the Claude Code CLI, :class:`QuickAskRunner`, or
``run_cli.ps1``, and it never creates a :class:`SessionManager` session — so
Workflow Plan/Review are fully isolated from Short Talk and the ordinary Claude
lifecycle.

Cancellation is logical: ``stop()`` sets a flag and the in-flight request is
ignored when it returns. The stdlib urllib transport cannot be aborted
mid-flight from another thread, so the network request may finish in the
background after a cancel; the runner never emits a FINAL or produces an
artifact for a cancelled request.
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request

from PySide6.QtCore import QObject, Signal

from core.agent_events import (
    STATUS_THINKING,
    AgentEvent,
    AgentEventType,
    ErrorCategory,
)
from core.provider_client import (
    ProviderHttpError,
    ProviderProtocolError,
    ProviderTimeoutError,
    ProviderTransportError,
    WorkflowProviderConfig,
    build_messages_headers,
    build_messages_payload,
    extract_text,
    load_workflow_provider_config,
)


def default_transport(
    endpoint: str,
    payload: dict,
    headers: dict,
    timeout: float,
    cancel_event: threading.Event,
) -> dict:
    """Perform one non-streaming POST and return the parsed JSON body.

    ``cancel_event`` is checked cooperatively while reading the response body;
    urllib cannot be force-aborted from another thread, so a cancelled request
    may still return and is discarded by the caller.
    """
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = _read_response(resp, cancel_event)
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ProviderProtocolError("malformed JSON response") from exc
            return parsed
    except urllib.error.HTTPError as exc:
        raise ProviderHttpError(exc.code, _safe_error_body(exc)) from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
        if _is_timeout(exc):
            raise ProviderTimeoutError(str(exc)) from exc
        raise ProviderTransportError(str(exc)) from exc
    except OSError as exc:
        raise ProviderTransportError(str(exc)) from exc


def _read_response(resp, cancel_event: threading.Event) -> str:
    chunks: list[bytes] = []
    while True:
        chunk = resp.read(64 * 1024)
        if not chunk:
            break
        chunks.append(chunk)
        if cancel_event.is_set():
            # Cooperatively stop reading; the caller will discard this result.
            break
    return b"".join(chunks).decode("utf-8", "replace")


def _is_timeout(exc) -> bool:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    if isinstance(exc, urllib.error.URLError):
        return isinstance(exc.reason, (TimeoutError, socket.timeout))
    return False


def _safe_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", "replace").strip()
    except OSError:
        body = ""
    if not body:
        return f"HTTP {exc.code} ({exc.reason})"
    return f"HTTP {exc.code} ({exc.reason}): {body[:800]}"


class DirectProviderRunner(QObject):
    """Run Workflow Plan/Review against a direct provider, off the GUI thread.

    ``config`` and ``transport`` are injectable for tests; in production
    ``config`` is resolved lazily from the active provider env and ``transport``
    is the stdlib urllib call. The runner is provider-neutral: it knows only an
    Anthropic-compatible endpoint, a request model, and an in-memory auth token.
    """

    AGENT_ID = "claude"

    agent_event = Signal(object)  # passthrough of the managed AgentEvents
    finished = Signal(str, int)  # (text, exit_code): 0 ok, non-zero error/cancel
    failed = Signal(str)

    def __init__(
        self,
        config: WorkflowProviderConfig | None = None,
        transport=None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._transport = transport if transport is not None else default_transport
        self._busy = False
        self._cancelled = False
        self._cancel_event: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._generation = 0

    @property
    def running(self) -> bool:
        return self._busy

    @property
    def config(self) -> WorkflowProviderConfig | None:
        return self._config

    # -- public -----------------------------------------------------------

    def ask(self, prompt: str) -> bool:
        """Start one non-streaming provider request; returns False on local reject."""
        if self._busy:
            self.failed.emit("a direct provider request is already running")
            return False
        prompt = (prompt or "").strip()
        if not prompt:
            self.failed.emit("empty prompt")
            return False

        try:
            config = self._config if self._config is not None else load_workflow_provider_config()
        except Exception as exc:  # noqa: BLE001 - config error must not crash the app
            self.agent_event.emit(
                AgentEvent.make(
                    self.AGENT_ID,
                    AgentEventType.ERROR,
                    text=str(exc),
                    error_code=ErrorCategory.CONFIGURATION,
                )
            )
            self.finished.emit("", 1)
            return True

        self._busy = True
        self._cancelled = False
        self._cancel_event = threading.Event()
        self._generation += 1
        generation = self._generation

        self.agent_event.emit(AgentEvent.make(self.AGENT_ID, AgentEventType.STARTED))
        self.agent_event.emit(
            AgentEvent.make(self.AGENT_ID, AgentEventType.STATUS, status=STATUS_THINKING)
        )

        self._thread = threading.Thread(
            target=self._run, args=(prompt, config, generation), daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        """Logical cancel: mark the in-flight request to be discarded on return."""
        if not self._busy:
            return
        self._cancelled = True
        if self._cancel_event is not None:
            self._cancel_event.set()

    # -- internals --------------------------------------------------------

    def _run(self, prompt: str, config: WorkflowProviderConfig, generation: int) -> None:
        try:
            events, text = self._perform(prompt, config, self._cancel_event)
            if generation != self._generation:
                return  # a newer ask superseded this one
            for ev in events:
                self.agent_event.emit(ev)
            if events and events[-1].type == AgentEventType.FINAL:
                self.finished.emit(text, 0)
            else:
                self.finished.emit("", 1)
        finally:
            self._busy = False
            self._cancel_event = None
            self._thread = None

    def _perform(
        self,
        prompt: str,
        config: WorkflowProviderConfig,
        cancel_event: threading.Event,
    ) -> tuple[list[AgentEvent], str]:
        """Synchronously perform one request. Returns (events, final_text).

        This is the thread-free core, kept separate so tests can exercise the
        transport, taxonomy, and cancellation deterministically without a real
        thread or event loop.
        """
        if cancel_event is not None and cancel_event.is_set():
            return [
                AgentEvent.make(
                    self.AGENT_ID, AgentEventType.CANCELLED, error_code=ErrorCategory.CANCELLED
                )
            ], ""

        try:
            payload = build_messages_payload(prompt, config)
            headers = build_messages_headers(config)
            response_json = self._transport(
                config.endpoint, payload, headers, config.timeout_seconds, cancel_event
            )
        except Exception as exc:  # noqa: BLE001 - classify every provider failure
            category, message = self._classify(exc)
            return [
                AgentEvent.make(
                    self.AGENT_ID, AgentEventType.ERROR, text=message, error_code=category
                )
            ], ""

        if cancel_event is not None and cancel_event.is_set():
            # Cancelled while the request was in flight: discard the late response.
            return [
                AgentEvent.make(
                    self.AGENT_ID, AgentEventType.CANCELLED, error_code=ErrorCategory.CANCELLED
                )
            ], ""

        text = extract_text(response_json)
        return [AgentEvent.make(self.AGENT_ID, AgentEventType.FINAL, text=text)], text

    @staticmethod
    def _classify(exc: Exception) -> tuple[ErrorCategory, str]:
        if isinstance(exc, ProviderHttpError):
            if exc.status in (401, 403):
                return ErrorCategory.AUTH, f"provider auth failed (HTTP {exc.status})"
            return ErrorCategory.PROVIDER, f"provider error (HTTP {exc.status})"
        if isinstance(exc, ProviderTimeoutError):
            return ErrorCategory.TIMEOUT, "provider request timed out"
        if isinstance(exc, ProviderTransportError):
            return ErrorCategory.TRANSPORT, f"transport error: {exc}"
        if isinstance(exc, ProviderProtocolError):
            return ErrorCategory.PROTOCOL, f"protocol error: {exc}"
        return ErrorCategory.UNKNOWN, f"provider error: {exc}"
