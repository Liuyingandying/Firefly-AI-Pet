"""PageLens Bridge — Desktop WebSocket server.

Phase 9B: localhost WebSocket server that connects Firefly AI Pet's PageLens
panel to the Firefly PageLens browser extension.

Architecture:
  - asyncio-based WebSocket server running in a daemon thread
  - Receives browser events (concept cards, question deltas, etc.)
  - Sends desktop actions (open_concept, open_related, open_question, back)
  - Emits Qt signals for UI thread consumption
  - Strict JSON protocol with type whitelist
  - Only listens on 127.0.0.1:17321

Security:
  - Loopback only (127.0.0.1)
  - Strict JSON type whitelist
  - No eval/exec/shell/file-path actions
  - No API keys in messages
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
import threading
import time
from typing import Any

import websockets
from PySide6.QtCore import QObject, QThread, QTimer, Signal

from core.ai_router import chat

log = logging.getLogger("firefly.pagelens.bridge")

# ---------------------------------------------------------------------------
# Bridge constants
# ---------------------------------------------------------------------------
BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 17321
BRIDGE_KEEPALIVE_INTERVAL = 20  # seconds
BRIDGE_RECONNECT_BASE = 1  # seconds (for browser client)
BRIDGE_RECONNECT_MAX = 5  # seconds (for browser client)

_AI_CHAT_SOURCES = frozenset({
    "explain",
    "semantic_concept",
    "concept_card",
    "follow_up",
})

# ---------------------------------------------------------------------------
# Incoming message types (Browser → Desktop)
# ---------------------------------------------------------------------------
_INCOMING_TYPES = frozenset({
    "bridge_hello",
    "bridge_ping",
    "page_context",
    "concepts",
    "concept_loading",
    "concept_card",
    "concept_error",
    "question_loading",
    "question_delta",
    "question_done",
    "question_error",
    "view_state",
    "pagelens-desktop-sync-request",
    "ai_chat_request",
})

# ---------------------------------------------------------------------------
# Outgoing action types (Desktop → Browser)
# ---------------------------------------------------------------------------
_OUTGOING_TYPES = frozenset({
    "open_concept",
    "open_related",
    "open_question",
    "back",
    "sync_request",
})


class PageLensBridge(QObject):
    """Qt-safe PageLens WebSocket bridge.

    All network I/O happens in a background asyncio thread.
    UI updates happen via Qt signals emitted on the main thread.

    Public signals:
        bridge_connected: emitted when browser extension connects
        bridge_disconnected: emitted when browser extension disconnects
        concept_loading(str): term being loaded
        concept_card(dict): full concept card payload
        concept_error(str): error message
        question_loading(dict): {parent_term, question}
        question_delta(str): SSE delta chunk
        question_done: question streaming complete
        question_error(str): question error
        concepts(list): top concepts list for "本页概念" area
        view_state(dict): browser view state mirror
        page_context(dict): page metadata (title, url)
    """

    # Signals
    bridge_connected = Signal(bool)
    bridge_disconnected = Signal(bool)
    concept_loading = Signal(str)
    concept_card = Signal(dict)
    concept_error = Signal(str)
    question_loading = Signal(dict)
    question_delta = Signal(str)
    question_done = Signal()
    question_error = Signal(str)
    concepts = Signal(list)
    view_state = Signal(dict)
    page_context = Signal(dict)

    # Actions that the UI can trigger (emitted from PageLensPanel → bridge)
    action_open_concept = Signal(str)
    action_open_related = Signal(str)
    action_open_question = Signal(str)
    action_back = Signal()

    def __init__(self, parent: QObject | None = None, *, chat_handler=None):
        super().__init__(parent)
        self._thread: QThread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.AbstractServer | None = None
        self._client: asyncio.StreamReaderWriter | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected = False
        self._active_tab_id: int | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._reconnect_timer: asyncio.TimerHandle | None = None
        self._pending_actions: list[dict] = []  # queue actions while disconnected
        self._chat_handler = chat_handler or chat
        self._ai_chat_tasks: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the WebSocket server in a background daemon thread.

        Uses a plain threading.Thread (not QThread) because the asyncio
        event loop must call run_forever() directly — there is no way to
        make call_soon_threadsafe() execute a callback on a loop that is
        not yet running (chicken-and-egg).
        """
        if self._connected:
            return
        t = threading.Thread(target=self._run_loop, daemon=True, name="PageLensBridgeThread")
        self._thread = t  # keep reference so stop() can join it
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        t.start()
        log.info("[PageLens Bridge] starting on %s:%d", BRIDGE_HOST, BRIDGE_PORT)

    def stop(self) -> None:
        """Stop the WebSocket server and clean up."""
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            if isinstance(self._thread, threading.Thread):
                self._thread.join(timeout=5)
            elif hasattr(self._thread, "quit"):
                self._thread.quit()
                self._thread.wait()
            self._thread = None
        if self._loop is not None:
            # The loop may still be running after call_soon_threadsafe + join,
            # because the stop() callback might not have executed yet.
            # Wait up to 2s for the loop to stop.
            for _ in range(20):
                if not self._loop.is_running():
                    break
                time.sleep(0.1)
            try:
                self._loop.close()
            except RuntimeError:
                pass  # already closed or cannot close a running loop
            self._loop = None
        log.info("[PageLens Bridge] stopped")

    @property
    def connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # Outgoing actions (called from UI thread via signals)
    # ------------------------------------------------------------------

    def send_action(self, action_type: str, payload: dict | None = None) -> None:
        """Send an action to the browser. Thread-safe."""
        if action_type not in _OUTGOING_TYPES:
            log.warning("[PageLens Bridge] unknown action type: %s", action_type)
            return
        msg = {"type": action_type, "payload": payload or {}}
        if self._active_tab_id is not None:
            msg["tabId"] = self._active_tab_id
        self._loop.call_soon_threadsafe(self._send_json, msg)

    def send_open_concept(self, term: str) -> None:
        self.send_action("open_concept", {"term": term})

    def send_open_related(self, term: str) -> None:
        self.send_action("open_related", {"term": term})

    def send_open_question(self, question: str) -> None:
        self.send_action("open_question", {"question": question})

    def send_back(self) -> None:
        self.send_action("back", {})

    def send_sync_request(self) -> None:
        self.send_action("sync_request", {})

    # ------------------------------------------------------------------
    # Internal asyncio methods (run in event loop thread)
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        """Entry point for the background event loop.

        Wraps everything in try/except so a server crash doesn't silently
        disappear — the exception is logged and the loop exits cleanly.
        """
        try:
            self._start_server()
            self._loop.run_forever()
        except Exception:
            log.exception("[PageLens Bridge] event loop crashed")
        finally:
            self._loop.stop()
            self._loop.close()
            self._loop = None

    def _start_server(self) -> None:
        try:
            asyncio.ensure_future(self._run_server(), loop=self._loop)
        except Exception:
            log.exception("[PageLens Bridge] failed to start server coroutine")

    def _stop_server(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
        if self._writer is not None:
            try:
                # websockets Connection: close the connection
                import asyncio as _asyncio
                _asyncio.run_coroutine_threadsafe(self._writer.close(), self._loop)
            except Exception:
                pass
            self._writer = None
        self._connected = False
        self._active_tab_id = None
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            self._keepalive_task = None
        if self._reconnect_timer is not None:
            self._reconnect_timer.cancel()
            self._reconnect_timer = None

    async def _run_server(self) -> None:
        try:
            self._server = await websockets.serve(
                self._handle_client, BRIDGE_HOST, BRIDGE_PORT,
                process_request=self._process_request,
            )
            log.info("[PageLens Bridge] WebSocket listening %s:%d", BRIDGE_HOST, BRIDGE_PORT)
        except OSError as exc:
            log.error("[PageLens Bridge] bind failed: %s", exc)
            return

        try:
            await self._server.wait_closed()
        except asyncio.CancelledError:
            pass
        finally:
            try:
                self._server.close()
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None

    async def _process_request(self, path, request):
        """Validate incoming WebSocket connections before upgrade.

        Logs the Origin header for debugging. Accepts all origins in dev mode
        (chrome-extension:// IDs change on each reload).
        """
        origin = request.headers.get("Origin", "unknown")
        log.info("[PageLens Bridge] incoming connection from %s (Origin: %s)", request.headers.get("Host"), origin)
        # Accept all origins — Chrome extension IDs change on reload,
        # and we only listen on 127.0.0.1 anyway.
        return None  # None means "proceed with handshake"

    async def _handle_client(self, websocket) -> None:
        """Handle a single browser extension WebSocket connection."""
        self._writer = websocket
        peer = websocket.remote_address
        log.info("[PageLens Bridge] browser connected from %s", peer)

        try:
            async for raw in websocket:
                if not isinstance(raw, str):
                    log.warning("[PageLens Bridge] non-text frame received, skipping")
                    continue
                raw = raw.strip()
                if not raw:
                    continue
                self._handle_incoming(raw)
        except websockets.ConnectionClosed:
            log.info("[PageLens Bridge] browser disconnected (closed)")
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("[PageLens Bridge] client error")
        finally:
            # A superseded connection must not tear down the newer writer or
            # cause its in-flight RPC results to be treated as disconnected.
            if websocket is self._writer:
                self._connected = False
                self._active_tab_id = None
                if self._keepalive_task is not None:
                    self._keepalive_task.cancel()
                    self._keepalive_task = None
                self._writer = None
                self.bridge_disconnected.emit(False)
                log.info("[PageLens Bridge] disconnected")

    def _handle_incoming(self, raw: str) -> None:
        """Parse and dispatch an incoming JSON message."""
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            log.warning("[PageLens Bridge] invalid JSON: %s", raw[:200])
            return

        msg_type = msg.get("type", "")
        if msg_type not in _INCOMING_TYPES:
            log.warning("[PageLens Bridge] unknown type: %s", msg_type)
            return

        payload = msg.get("payload", {})
        tab_id = msg.get("tabId")

        # Track active tab
        if tab_id is not None:
            self._active_tab_id = tab_id

        if msg_type == "bridge_hello":
            self._connected = True
            self.bridge_connected.emit(True)
            log.info("[PageLens Bridge] browser connected")
            # Flush pending actions
            for action in self._pending_actions:
                self._send_json(action)
            self._pending_actions.clear()
            # A real WebSocket client has a writer. Direct protocol unit tests
            # intentionally do not, so they must not leave keepalive tasks.
            if self._writer is not None:
                self._start_keepalive()

        elif msg_type == "bridge_ping":
            # Browser-side keepalive. Acknowledge without changing bridge or
            # panel state; WebSocket OPEN/CLOSE remains authoritative.
            self._send_json({"type": "bridge_pong"})

        elif msg_type == "page_context":
            self.page_context.emit(payload)

        elif msg_type == "concepts":
            items = payload.get("items", [])
            self.concepts.emit(items)

        elif msg_type == "concept_loading":
            term = payload.get("term", "")
            self.concept_loading.emit(term)

        elif msg_type == "concept_card":
            self.concept_card.emit(payload)
            log.info("[PageLens Bridge] concept_card: %s", payload.get("term", ""))

        elif msg_type == "concept_error":
            self.concept_error.emit(payload.get("message", "Unknown error"))

        elif msg_type == "question_loading":
            self.question_loading.emit(payload)

        elif msg_type == "question_delta":
            self.question_delta.emit(payload.get("delta", ""))

        elif msg_type == "question_done":
            self.question_done.emit()

        elif msg_type == "question_error":
            self.question_error.emit(payload.get("message", "Unknown error"))

        elif msg_type == "view_state":
            self.view_state.emit(payload)

        elif msg_type == "pagelens-desktop-sync-request":
            # Browser is asking Desktop to replay current state.
            # Desktop doesn't have browser state, so we just acknowledge.
            # The real sync happens via browser sending pagelens-mirror messages.
            log.info("[PageLens Bridge] sync-request received, browser will send state")

        elif msg_type == "ai_chat_request":
            self._schedule_ai_chat(msg)

    def _schedule_ai_chat(self, request: dict) -> None:
        """Validate and schedule one non-blocking AI Router request."""
        request_id, source, messages, temperature, error = self._validate_ai_chat_request(
            request
        )
        if error:
            self._send_ai_chat_error(request_id, error)
            return
        if request_id in self._ai_chat_tasks:
            self._send_ai_chat_error(request_id, "duplicate requestId")
            return
        if self._loop is None or not self._loop.is_running():
            self._send_ai_chat_error(request_id, "Desktop AI bridge is unavailable")
            return

        connection = self._writer
        task = asyncio.ensure_future(
            self._run_ai_chat(
                request_id,
                source,
                messages,
                temperature,
                connection,
            ),
            loop=self._loop,
        )
        self._ai_chat_tasks[request_id] = task

        def _forget(completed: asyncio.Task) -> None:
            if self._ai_chat_tasks.get(request_id) is completed:
                self._ai_chat_tasks.pop(request_id, None)

        task.add_done_callback(_forget)

    @staticmethod
    def _validate_ai_chat_request(request: dict) -> tuple[str, str, list, float, str]:
        request_id = request.get("requestId")
        safe_request_id = request_id.strip() if isinstance(request_id, str) else ""
        if not safe_request_id:
            return "", "", [], 0.0, "requestId is required"
        if len(safe_request_id) > 128:
            return safe_request_id[:128], "", [], 0.0, "requestId is too long"

        source = request.get("source")
        if not isinstance(source, str) or not source.strip():
            return safe_request_id, "", [], 0.0, "source is required"
        source = source.strip()
        if source not in _AI_CHAT_SOURCES:
            return safe_request_id, source, [], 0.0, "unsupported source"

        messages = request.get("messages")
        if not isinstance(messages, list):
            return safe_request_id, source, [], 0.0, "messages must be a list"

        temperature = request.get("temperature")
        if (
            isinstance(temperature, bool)
            or not isinstance(temperature, (int, float))
            or not math.isfinite(float(temperature))
        ):
            return safe_request_id, source, messages, 0.0, "temperature must be a number"
        return safe_request_id, source, messages, float(temperature), ""

    async def _run_ai_chat(
        self,
        request_id: str,
        source: str,
        messages: list,
        temperature: float,
        connection,
    ) -> None:
        """Run synchronous ``core.ai_router.chat`` outside the WebSocket loop."""
        try:
            response = await asyncio.to_thread(
                self._chat_handler,
                messages,
                temperature=temperature,
            )
            provider = response.get("provider") if isinstance(response, dict) else None
            choices = response.get("choices") if isinstance(response, dict) else None
            choice = choices[0] if isinstance(choices, list) and choices else None
            message = choice.get("message") if isinstance(choice, dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(provider, str) or not provider:
                raise ValueError("AI Router response has no provider")
            if not isinstance(content, str):
                raise ValueError("AI Router response has no content")
        except Exception as exc:  # isolate provider and response failures
            if connection is self._writer and connection is not None:
                self._send_ai_chat_error(request_id, str(exc)[:500])
            return

        # A request that finishes after its WebSocket disconnected must never
        # leak into a later connection, even if that connection reuses a tab.
        if connection is not self._writer or connection is None:
            log.info("[PageLens Bridge] discarded stale AI response: %s", request_id)
            return
        self._send_json({
            "type": "ai_chat_response",
            "requestId": request_id,
            "provider": provider,
            "content": content,
        })
        log.info(
            "[PageLens Bridge] AI response: request=%s source=%s provider=%s",
            request_id,
            source,
            provider,
        )

    def _send_ai_chat_error(self, request_id: str, error: str) -> None:
        self._send_json({
            "type": "ai_chat_error",
            "requestId": request_id,
            "error": error or "AI Router request failed",
        })

    def _send_json(self, msg: dict) -> None:
        """Send JSON to the browser client. Thread-safe."""
        if self._writer is not None:
            try:
                result = self._writer.send(json.dumps(msg, ensure_ascii=False))
                if inspect.isawaitable(result):
                    if self._loop is None or not self._loop.is_running():
                        result.close()
                        raise RuntimeError("WebSocket event loop is not running")
                    future = asyncio.run_coroutine_threadsafe(result, self._loop)

                    def _report_send_failure(completed) -> None:
                        try:
                            completed.result()
                        except Exception:
                            log.exception("[PageLens Bridge] async send error")

                    future.add_done_callback(_report_send_failure)
            except Exception:
                log.exception("[PageLens Bridge] send error")
        elif self._connected:
            # Should not happen, but queue just in case
            log.warning("[PageLens Bridge] no writer while connected, dropping: %s", msg.get("type"))

    def _start_keepalive(self) -> None:
        """Send periodic ping to keep the connection alive."""
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()

        async def _keepalive_loop() -> None:
            while True:
                await asyncio.sleep(BRIDGE_KEEPALIVE_INTERVAL)
                if self._writer is not None:
                    try:
                        await self._writer.send('{"type":"bridge_ping"}')
                    except Exception:
                        break

        self._keepalive_task = asyncio.ensure_future(_keepalive_loop(), loop=self._loop)
