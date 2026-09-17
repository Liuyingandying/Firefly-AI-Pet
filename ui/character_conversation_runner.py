"""Qt-safe bridge from the Firefly bubble to ConversationRuntime."""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Any, NamedTuple

from PySide6.QtCore import QObject, QThread, Signal

from core.agent_events import (
    STATUS_READING,
    STATUS_THINKING,
    AgentEvent,
    AgentEventType,
    ErrorCategory,
)
from core.runtime_bus import CameraObservedEvent, RuntimeEvent
from core.conversation_runtime import ConversationRuntime
from core.conversation_store import ConversationStore
from core.screen_vision.trigger import (
    format_screen_vision_context,
    is_camera_vision_request,
    is_explicit_screen_vision_request,
    is_look_command,
    resolve_capture_target,
    screen_vision_question,
)
from core.screen_vision.vision.deepseek_vision import (
    DEFAULT_DIRECT_STYLE_CONTEXT as DEFAULT_ATTACHMENT_STYLE_CONTEXT,
)
from ui.companion_attachment import (
    DEFAULT_ATTACHMENT_QUESTION,
    DEFAULT_DOCUMENT_QUESTION,
    HISTORY_DOCUMENT_PLACEHOLDER,
    HISTORY_IMAGE_PLACEHOLDER,
    AttachmentError,
    AttachmentImage,
    DocumentAttachment,
    attachment_to_frame,
)
from core.document_attachment import (
    DocumentContext,
    DocumentParseError,
    EncryptedPdfError,
    build_qa_messages,
    chunk_document,
    direct_section_lookup,
    format_excerpts,
    is_summary_request,
    retrieve_chunks,
    summarize_document,
)
from core.document_vision import (
    DOCUMENT_VISION_STYLE_CONTEXT,
    PageOutOfRangeError,
    build_document_vision_question,
    is_document_vision_request,
    resolve_document_location,
)
from core.document_router import (
    DocumentAction,
    DocumentRoute,
    route_document_question,
)
from core.lazy_pdf_ocr import (
    BATCH_LIMIT_NOTE,
    LOW_COVERAGE_REPLY,
    MAX_LAZY_OCR_PAGES_PER_TURN,
    MAX_PROGRESSIVE_OCR_PAGES_PER_TURN,
    MAX_SUMMARY_SEED_OCR_PAGES,
    PAGE_OCR_FAILED_REPLY,
    SUMMARY_COVERAGE_THRESHOLD,
    LazyPdfOcrState,
    is_continue_scan_request,
    is_full_ocr_request,
    run_ocr_batch,
)
from core.pdf_processor import PdfPageStatus
from core.video_frame_vision import analyze_video_frame
from core.video_reader import (
    SessionVideoContext,
    analyze_video_message,
    detect_bilibili_reference,
    is_video_followup,
    video_reading_failure_reply,
)
from core.video_time_parser import (
    format_timestamp,
    parse_video_time_expression,
)
from core.video_study import (
    STUDY_ENTRY_REPLY,
    VideoStudyContext,
    handle_study_turn,
    is_quiz_request,
    is_study_entry,
    is_study_exit,
    study_failure_reply,
)


logger = logging.getLogger(__name__)

# P0.2 diagnostic tracing start anchor (env-gated, behavior-neutral).
_TRACE_START = time.monotonic()


# M1.5: the LLM prompt only ever sees the most recent WORKING_WINDOW_MESSAGES
# messages of conversation history (bounded Working Memory). The full session
# history remains in ConversationStore. Keep this distinct from the store's
# own persistence ring (`max_messages`) -- they may differ by configuration.
WORKING_WINDOW_MESSAGES = 40


class WorkingContextStats(NamedTuple):
    """Bounded working-memory diagnostics (M1.5; no tokenizer dependency).

    ``turns`` = chat messages currently in the working window;
    ``chars`` = total content characters across those messages.
    """

    turns: int
    chars: int

SCREEN_VISION_FAILURE_REPLY = (
    "我这边的视觉模块这次没能看成屏幕（{reason}）。"
    "你可以稍后再试一次，或者直接把内容粘贴给我。"
)
SCREEN_VISION_UNAVAILABLE_REPLY = (
    "我这次暂时没能看清屏幕，视觉服务好像出了点问题。"
    "稍后再叫我看一次吧；也可以直接把内容粘贴给我。"
)
SCREEN_VISION_REASONING_UNAVAILABLE_REPLY = (
    "我已经看到了，但这次分析服务好像有些不稳定，稍后再问我一次吧。"
)
CAMERA_VISION_DISABLED_REPLY = "Camera Vision 当前已关闭，可以在 Quick Tools 中开启。"
CAMERA_OBSERVATION_DISABLED_REPLY = (
    "Camera Vision 当前已关闭，我不会继续使用之前的摄像头观察结果。"
)
CAMERA_VISION_UNAVAILABLE_REPLY = (
    "Camera Vision 当前不可用：未检测到可用摄像头，或摄像头没有返回画面。"
)
VIDEO_ANALYSIS_DISABLED_REPLY = (
    "Video Analysis 当前已关闭。可以在 Quick Tools 中开启后再让我分析这个视频。"
)
TJU_RETRIEVAL_DISABLED_REPLY = (
    "TJU Info Retrieval 当前已关闭，可以在 Quick Tools 中开启后再使用信息检索。"
)
TJU_RETRIEVAL_AUTH_REQUIRED_REPLY = (
    "TJU Info Retrieval 的天津大学登录状态已过期。请重新登录后再试。\n"
    "回复「用 TJU 信息检索重新登录」即可打开受控浏览器完成登录。"
)
TJU_RETRIEVAL_RELOGIN_DONE_REPLY = (
    "已打开受控浏览器，请在天津大学登录页完成登录。登录完成后可以重新发送检索指令。"
)

# 保守触发：只有显式的「用 TJU 信息检索/用信息检索系统」才算；绝不因「天津大学」
# 字样自动启动插件。普通聊天不受影响。
_TJU_TRIGGERS = (
    "用TJU信息检索查",
    "用TJU信息检索搜索",
    "用信息检索系统搜索",
    "用信息检索系统查",
    "用TJU检索",
)


def _tju_query(text: str) -> str | None:
    """Extract the query after an explicit TJU-retrieval trigger; None otherwise."""
    compact = re.sub(r"\s+", "", text or "")
    if not compact:
        return None
    for prefix in _TJU_TRIGGERS:
        if compact.startswith(prefix):
            query = compact[len(prefix):].strip(" ：:，,。.!！?？")
            return query or None
    return None


def _tju_relogin_request(text: str) -> bool:
    """True only for the explicit re-login trigger (opens the controlled Edge)."""
    compact = re.sub(r"\s+", "", text or "")
    return any(
        marker in compact
        for marker in ("用TJU信息检索重新登录", "TJU重新登录", "重新登录TJU")
    )


def _tju_open_request(text: str) -> bool:
    """True only for the explicit「打开 TJU 信息检索系统」trigger (open_ui).

    Deliberately disjoint from the search triggers ("用 TJU 信息检索搜索 xxx"):
    "打开 TJU 信息检索" never runs a search, and a search request never opens
    the GUI.
    """
    compact = re.sub(r"\s+", "", text or "")
    return any(
        marker in compact
        for marker in ("打开TJU信息检索", "打开信息检索系统", "打开科研检索")
    )


def _format_tju_results(query: str, results) -> str:
    """Render search results in the chat area (plain text, no new window)."""
    if not results:
        return f"TJU Info Retrieval\n\n没有找到「{query}」的相关结果。"
    lines = ["TJU Info Retrieval", f"找到 {len(results)} 条结果：", ""]
    for index, item in enumerate(results, start=1):
        title = getattr(item, "title", "") or ""
        snippet = getattr(item, "snippet", "") or ""
        lines.append(f"{index}. {title}")
        if snippet:
            lines.append(f"   {snippet[:160]}")
        source = getattr(item, "source", "") or ""
        meta = dict(getattr(item, "metadata", {}) or {})
        year = meta.get("year")
        url = meta.get("detail_url")
        detail = " · ".join(
            part for part in (str(source), str(year) if year else "", str(url) if url else "") if part
        )
        if detail:
            lines.append(f"   {detail}")
        lines.append("")
    return "\n".join(lines).rstrip()
CAMERA_VISION_FAILURE_REPLY = (
    "我的视觉分析服务暂时没有响应，可以稍后再让我看看。"
)
CAMERA_VISION_REASONING_UNAVAILABLE_REPLY = (
    "Camera Vision 已取得画面，但这次分析服务不稳定，请稍后再试。"
)

# Image-attachment turns (v1). The dot/capability is completely independent of
# the screen: an attached image is answered from the image itself, never from a
# screenshot.
ATTACHMENT_VISION_FAILURE_REPLY = (
    "这张图片我这次没能看清（{reason}）。你可以稍后再试一次。"
)

# Document-attachment turns. Documents are parsed locally; only question-
# relevant excerpts reach the text provider (core.ai_router) — never Memory.
DOCUMENT_QA_FAILURE_REPLY = (
    "这次没能读取这个文档（{reason}）。你可以稍后再试一次。"
)
DOCUMENT_UNREADABLE_REPLY = "这个文件似乎无法读取。"
DOCUMENT_ENCRYPTED_REPLY = "这个 PDF 需要密码，目前无法直接读取。"
DOCUMENT_PARSING_REPLY = "文档还在解析中，请稍候再问。"
DOCUMENT_PARTIAL_OCR_NOTE = "\n\n（注：部分页面没有成功识别。）"

# Document Vision turns: render the requested page/slide and answer with one
# direct vision call (page image + page text + question). Only explicit visual
# triggers plus a resolvable page/slide location engage this path.
DOCUMENT_VISION_FAILURE_REPLY = (
    "这一页的图片我这次没能看清（{reason}）。你可以稍后再试一次。"
)
DOCUMENT_VISION_UNRESOLVED_REPLY = "请告诉我具体页码，我才能查看对应页面。"
DOCUMENT_VISION_OUT_OF_RANGE_REPLY = "这份文档没有这一页，请告诉我一个有效的页码。"
DOCUMENT_VISION_NO_SOURCE_REPLY = "这份文档暂时无法查看页面图片，请重新上传后再试。"
DOCUMENT_VISION_RENDER_FAILED_REPLY = (
    "这一页暂时没能渲染成图片（{reason}），请稍后再试。"
)
DOCUMENT_VISION_UNSUPPORTED_REPLY = "这种文档类型暂时不支持查看页面图片。"

# Explicit "ignore the document" phrasing falls through to a normal turn.
_DOCUMENT_NEGATION_MARKERS = (
    "先不看这个文件", "先不管这个文件", "别看这个文件", "不要看这个文件",
    "不看这个文件", "忽略这个文件", "先不看文件", "先不管文件",
    "先不看这份文件", "先不管这份文件",
)

# Explicit "ignore the image" phrasing falls through to a normal turn (which
# may then legitimately use screen vision). No LLM intent classifier in v1.
_ATTACHMENT_NEGATION_MARKERS = (
    "不要看这张图", "不要看这张图片", "别看这张图", "别看这张图片",
    "不看这张图", "不看这张图片", "不要看图片", "别看图片",
)


# ---------------------------------------------------------------------------
# Vision-1C: recent camera observation ("刚看到你") follow-up context.
# Session-local: only the answer text is injected through the existing
# turn_context channel, within a freshness window; nothing is persisted and
# nothing but text ever enters the model context.
# ---------------------------------------------------------------------------

_RECENT_VISUAL_FOLLOWUPS = (
    "刚才看到我了吗",
    "我刚才什么样",
    "你刚刚看到什么",
    "刚才我的状态",
    "你看到我了吗",
    "你刚才看到我什么",
    "刚看到我什么",
    "你刚才看到什么",
    "刚才看到我",
)
_RECENT_VISUAL_WINDOW_S = 300.0  # 5 minutes


def _is_recent_visual_followup(text: str) -> bool:
    """True only for an explicit follow-up about the last camera look.

    Deliberately disjoint from the video follow-up markers ("刚才" alone is
    NOT enough), so a video reference never becomes a camera reference and
    vice versa.
    """
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    return any(phrase in normalized for phrase in _RECENT_VISUAL_FOLLOWUPS)


def _recent_visual_window_valid(observed_at: float | None, now: float | None = None) -> bool:
    if observed_at is None:
        return False
    return (now if now is not None else time.time()) - observed_at <= _RECENT_VISUAL_WINDOW_S


def _camera_note_context_block(note: str) -> str:
    """Render the recent-camera-observation turn_context block (text only)."""
    return (
        "[Recent Visual Context]\n"
        "本会话最近一次通过摄像头看到你时的描述：\n"
        f"{note}\n"
        "基于这次摄像头观察回答用户；画面之外或看不清的内容不要编造。\n"
        "[End Recent Visual Context]"
    )


class CharacterConversationRunner(QObject):
    """Run non-streaming character turns without blocking the Qt UI thread."""

    _CAMERA_TRACE = logging.getLogger("firefly.camera_trace")

    def _camera_trace(self, event: str, extra: str = "") -> None:
        """P0.2 diagnostic tracing: env-gated (FIREFLY_CAMERA_TRACE=1),
        behavior-neutral, thread-identified."""
        if not os.environ.get("FIREFLY_CAMERA_TRACE"):
            return
        try:
            current = QThread.currentThread()
            qname = current.objectName() or type(current).__name__
        except Exception:  # noqa: BLE001
            qname = "?"
        self._CAMERA_TRACE.info(
            "CAMTRACE %-24s dt=%7.1fms py_tid=%s py_name=%s qt=%s %s",
            event,
            (time.monotonic() - _TRACE_START) * 1000.0,
            threading.get_ident(),
            (threading.current_thread().name or "?"),
            qname,
            extra,
        )

    AGENT_ID = "firefly"

    agent_event = Signal(object)
    ocr_progress = Signal(object, int, int)   # (attachment, completed, total)
    ocr_page = Signal(object, int)            # (attachment, page) on-demand OCR started

    def __init__(
        self,
        runtime: ConversationRuntime | None = None,
        parent: QObject | None = None,
        screen_vision_service: Any | None = None,
        screen_vision_settings: Any | None = None,
        attachment_vision_provider: Any | None = None,
        document_chat_handler: Any | None = None,
        document_vision_renderer: Any | None = None,
        runtime_bus: Any | None = None,
        camera_vision_enabled: Any | None = None,
        learning_controller: Any | None = None,
        video_analysis_enabled: Any | None = None,
        tju_retrieval_enabled: Any | None = None,
        tju_retrieval_search: Any | None = None,
        tju_retrieval_open_login: Any | None = None,
        tju_retrieval_open_ui: Any | None = None,
    ) -> None:
        super().__init__(parent)
        self.runtime = runtime or ConversationRuntime(
            conversation_store=ConversationStore()
        )
        self._screen_vision_service = screen_vision_service
        self._screen_vision_settings = screen_vision_settings
        # Phase 1B: learning-mode controller (optional). When present, its
        # context_block() is injected through the existing turn_context
        # channel on ordinary chat turns while learning mode is enabled.
        # It never changes routing, providers or history persistence.
        self.learning_controller = learning_controller
        # Phase 9B-1: the outcome of the last response-contract enforcement
        # ({"met": bool, "missing": (...), "action": str}) — observable for
        # tests and diagnostics. None when no contract applied.
        self.last_contract_state = None
        # Vision-1B: RuntimeBus for session events (camera.observed). The bus
        # is created after this runner in app.py, so it is attached later via
        # set_runtime_bus(); None keeps the runner fully standalone.
        self._runtime_bus = runtime_bus
        # Managed Camera Vision gate. Standalone/test runners keep the legacy
        # enabled default; production injects PluginLoader's persisted state.
        self._camera_vision_enabled = camera_vision_enabled or (lambda: True)
        # Managed Video Analysis gate (Quick Tools「Video Analysis」). Read LIVE
        # on every request (never cached) so toggling takes effect immediately.
        # Standalone/test runners keep the legacy allow default; production
        # injects PluginLoader.is_plugin_enabled("firefly-video").
        self._video_analysis_enabled = video_analysis_enabled or (lambda: True)
        # TJU Info Retrieval capability gate (Quick Tools「TJU Info Retrieval」).
        # Read LIVE on every request; production injects
        # PluginLoader.is_plugin_enabled("tju-info-retrieval").
        self._tju_retrieval_enabled = tju_retrieval_enabled or (lambda: True)
        # The thin adapter callable (plugin.search) wired by app.py; None means
        # the capability is not wired (trigger degrades with a friendly note).
        self._tju_retrieval_search = tju_retrieval_search
        # Re-login action (plugin.open_login) — opens the controlled Edge for a
        # MANUAL TJU login. Never touches credentials.
        self._tju_retrieval_open_login = tju_retrieval_open_login
        # Open-GUI action (plugin.open_ui) — launches the original TJU desktop
        # app. An explicit user action, independent of the search capability
        # gate (Firefly's own search stays gated).
        self._tju_retrieval_open_ui = tju_retrieval_open_ui
        # Direct vision provider for image-attachment turns (injectable for
        # tests; defaults to the shared FAST TJU-Qwen vision provider).
        self._attachment_vision_provider = attachment_vision_provider
        # Text chat handler for document-attachment turns (injectable for
        # tests; defaults to core.ai_router.chat — no Memory/Bond/suggestion).
        self._document_chat_handler = document_chat_handler
        # Document Vision page/slide renderer (injectable for tests; defaults
        # to the real PyMuPDF / PowerPoint COM renderer).
        self._document_vision_renderer = document_vision_renderer
        self.last_screen_vision_timings: dict[str, float] | None = None
        self.last_screen_vision_meta: dict[str, Any] | None = None
        # Vision-1B: most recent explicit camera observation answer text.
        # Session-local, in-RAM only: never persisted, cleared at exit.
        self.last_camera_observation: str | None = None
        # Vision-1C: monotonic time of that observation (freshness window).
        self.last_camera_observation_at: float | None = None
        self.last_attachment_timings: dict[str, Any] | None = None
        self.last_attachment_meta: dict[str, Any] | None = None
        self.last_document_timings: dict[str, Any] | None = None
        self.last_document_meta: dict[str, Any] | None = None
        self.last_document_vision_timings: dict[str, Any] | None = None
        self.last_document_vision_meta: dict[str, Any] | None = None
        # Router explainability for the current document turn (debug only;
        # never shown to the user).
        self._active_document_action: str | None = None
        self._active_document_route_reason: str | None = None
        self._busy = False
        self._cancel_event: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._history = self._load_persisted_history()
        # Session video context ("刚才那个视频…" follow-ups). In-RAM only:
        # never persisted, cleared when the process ends.
        self._session_video: SessionVideoContext | None = None
        # Study companion mode over the session video ("陪我学习"/"考考我").
        self._video_study: VideoStudyContext | None = None
        self._lock = threading.RLock()

    def set_runtime_bus(self, runtime_bus: Any | None) -> None:
        """Attach the RuntimeBus after construction (app.py creates the bus
        after this runner). Passing None detaches and keeps the runner
        standalone; event publishing is optional at every call site."""
        self._runtime_bus = runtime_bus

    def _get_screen_vision_service(self) -> Any:
        """Lazily build the real service on first explicit request.

        Construction loads provider config from the environment; a missing
        configuration must not break Firefly startup, only the look request.
        """
        if self._screen_vision_service is None:
            from core.screen_vision.service import ScreenVisionService

            self._screen_vision_service = ScreenVisionService(
                settings=self._screen_vision_settings
            )
        # The Settings toggle applies immediately (no restart needed).
        sync = getattr(self._screen_vision_service, "sync_routing_mode_from_settings", None)
        if sync is not None:
            sync()
        return self._screen_vision_service

    def _working_window_limit(self) -> int:
        """M1.5: bounded working window = recent WORKING_WINDOW_MESSAGES
        messages, never exceeding the store's own persistence ring."""
        store = getattr(self.runtime, "conversation_store", None)
        ring = getattr(store, "max_messages", None)
        if isinstance(ring, int) and ring > 0:
            return min(WORKING_WINDOW_MESSAGES, ring)
        return WORKING_WINDOW_MESSAGES

    def _append_history(self, messages: list[dict[str, str]]) -> None:
        """M1.5: the only append path for in-RAM working memory.

        ``_history`` represents the CURRENT WORKING WINDOW (the recent
        conversation the LLM request needs), never the full history: after
        every append it is deterministically trimmed to
        ``ConversationStore.max_messages`` messages -- the same bound the
        store itself applies. Full history stays in ConversationStore only.
        """
        with self._lock:
            self._history.extend(messages)
            limit = self._working_window_limit()
            excess = len(self._history) - limit
            if excess > 0:
                del self._history[:excess]

    def working_context_stats(self) -> WorkingContextStats:
        """Lightweight prompt-side diagnostics (turns/chars, no tokenizer)."""
        with self._lock:
            return WorkingContextStats(
                turns=len(self._history),
                chars=sum(len(m.get("content", "")) for m in self._history),
            )

    def _load_persisted_history(self) -> list[dict[str, str]]:
        """Seed the working window from ConversationStore (M1.5: bounded).

        Reloads never pull the full session -- only the recent working window
        (``WORKING_WINDOW_MESSAGES``), so a restart cannot dump an unbounded
        history into the next prompt.
        """
        store = getattr(self.runtime, "conversation_store", None)
        loader = getattr(store, "load_working_window", None)
        if not callable(loader):
            return []
        limit = self._working_window_limit()
        try:
            return [turn.to_chat_message() for turn in loader(limit=limit)]
        except TypeError:
            # Legacy store signature without ``limit`` support.
            try:
                return [turn.to_chat_message() for turn in loader()]
            except Exception:
                return []
        except Exception:
            return []

    def open_tju_info_retrieval(self) -> str:
        """Explicit user action: launch the original TJU GUI (shared with Quick
        Tools「打开」/ 科研助手 / chat command). Returns a user-facing message.
        Independent of the search capability gate."""
        if self._tju_retrieval_open_ui is None:
            return "（TJU Info Retrieval 打开动作未接线。）"
        return self._tju_retrieval_open_ui()

    def reload_history(self) -> None:
        """Re-seed the in-memory history from the store's current session.

        Called by the console after switching the conversation session so the
        next turn uses the right context. No LLM call, no re-save.
        """
        with self._lock:
            self._history = self._load_persisted_history()

    @property
    def session_video(self) -> SessionVideoContext | None:
        """Read-only session video context (UI V2 console consumption)."""
        return self._session_video

    @property
    def video_study(self) -> VideoStudyContext | None:
        """Read-only study state over the session video (UI V2 console)."""
        return self._video_study

    @property
    def running(self) -> bool:
        with self._lock:
            return self._busy

    @property
    def has_history(self) -> bool:
        with self._lock:
            return bool(self._history)

    @property
    def history(self) -> list[dict[str, str]]:
        """Return a detached copy of the restored/in-process chat history."""
        with self._lock:
            return [dict(message) for message in self._history]



    @property
    def memory_service(self) -> Any:
        """Expose the underlying MemoryService for UI panels (read + write)."""
        runtime = getattr(self, "runtime", None)
        if runtime is None:
            return None
        _runtime = getattr(runtime, "_runtime", None)
        if _runtime is None:
            return None
        return getattr(_runtime, "memory_service", None)

    @property
    def suggestion_service(self) -> Any:
        """Expose the underlying SuggestionService for UI panels."""
        runtime = getattr(self, "runtime", None)
        if runtime is None:
            return None
        _runtime = getattr(runtime, "_runtime", None)
        if _runtime is None:
            return None
        return getattr(_runtime, "suggestion_service", None)
    def ask(self, prompt: str, learning_result: Any | None = None) -> bool:
        """Start a character turn and return False if the request is invalid.

        ``learning_result`` (Phase 9B-1): an already-computed
        :class:`LearningLoopResult` from the orchestrator — its contexts and
        decision drive the response contract directly, with NO recomputation.
        """
        text = (prompt or "").strip()
        with self._lock:
            if self._busy or not text:
                return False
            self._busy = True
            self._cancel_event = threading.Event()
            cancel_event = self._cancel_event

        self.agent_event.emit(
            AgentEvent.make(self.AGENT_ID, AgentEventType.STARTED)
        )
        self.agent_event.emit(
            AgentEvent.make(
                self.AGENT_ID,
                AgentEventType.STATUS,
                status=STATUS_THINKING,
            )
        )
        thread = threading.Thread(
            target=self._run,
            args=(text, cancel_event, learning_result),
            daemon=True,
            name="FireflyCharacterConversation",
        )
        with self._lock:
            self._thread = thread
        thread.start()
        return True

    def stop(self) -> None:
        """Logically cancel an in-flight request and discard its late reply."""
        with self._lock:
            if self._cancel_event is not None:
                self._cancel_event.set()

    def ask_with_image(self, question: str, attachment: AttachmentImage) -> bool:
        """Start a turn with an image attachment (never a screen capture).

        ``question`` may be empty; the runner substitutes an internal default
        question. Returns False when the runner is busy or attachment is None.
        """
        if attachment is None:
            return self.ask(question)
        with self._lock:
            if self._busy:
                return False
            self._busy = True
            self._cancel_event = threading.Event()
            cancel_event = self._cancel_event

        self.agent_event.emit(
            AgentEvent.make(self.AGENT_ID, AgentEventType.STARTED)
        )
        self.agent_event.emit(
            AgentEvent.make(
                self.AGENT_ID,
                AgentEventType.STATUS,
                status=STATUS_THINKING,
            )
        )
        thread = threading.Thread(
            target=self._run_with_image,
            args=(question, attachment, cancel_event),
            daemon=True,
            name="FireflyImageAttachmentTurn",
        )
        with self._lock:
            self._thread = thread
        thread.start()
        return True

    def perform_with_image(
        self,
        question: str,
        attachment: AttachmentImage,
        cancel_event: threading.Event | None = None,
    ) -> tuple[list[AgentEvent], str]:
        """Synchronously execute one image-attachment turn.

        With an attachment the turn always answers from the attached image —
        even when the text mentions looking at the screen — unless the text
        explicitly says to ignore the image (then it falls through to the
        normal turn, which may legitimately use screen vision). Exactly one
        direct vision remote call, no reasoning, no screen capture.
        """
        text = (question or "").strip()
        if attachment is None:
            return self.perform(text, cancel_event)
        if _attachment_negated(text):
            return self.perform(text, cancel_event)
        event = cancel_event or threading.Event()
        if event.is_set():
            return [self._cancelled_event()], ""

        effective_question = text or DEFAULT_ATTACHMENT_QUESTION
        prep_started = time.perf_counter()
        try:
            frame = attachment_to_frame(attachment)
        except Exception as exc:
            message = str(exc) if isinstance(exc, AttachmentError) else type(exc).__name__
            return [self._error_event(message, ErrorCategory.PROTOCOL)], ""
        prep_ms = (time.perf_counter() - prep_started) * 1000

        provider = self._get_attachment_vision_provider()
        vision_started = time.perf_counter()
        try:
            answer = provider.answer_direct(
                frame,
                effective_question,
                style_context=DEFAULT_ATTACHMENT_STYLE_CONTEXT,
            )
        except Exception as exc:
            vision_ms = (time.perf_counter() - vision_started) * 1000
            reason = _attachment_failure_label(exc)
            logger.info(
                "image attachment vision failed: %s (%s ms)",
                reason,
                round(vision_ms, 1),
            )
            return [self._error_event(
                ATTACHMENT_VISION_FAILURE_REPLY.format(reason=reason),
                ErrorCategory.PROVIDER,
            )], ""
        vision_ms = (time.perf_counter() - vision_started) * 1000
        if not isinstance(answer, str) or not answer.strip():
            return [self._error_event(
                ATTACHMENT_VISION_FAILURE_REPLY.format(reason="empty_response"),
                ErrorCategory.PROVIDER,
            )], ""
        answer = answer.strip()
        if event.is_set():
            return [self._cancelled_event()], ""

        history_user = HISTORY_IMAGE_PLACEHOLDER if not text else (
            f"{HISTORY_IMAGE_PLACEHOLDER} {text}"
        )
        with self._lock:
            self._append_history(
                [
                    {"role": "user", "content": history_user},
                    {"role": "assistant", "content": answer},
                ]
            )
        total_ms = (time.perf_counter() - prep_started) * 1000
        self.last_attachment_timings = {
            "attachment_prep_ms": round(prep_ms, 1),
            "vision_ms": round(vision_ms, 1),
            "total_ms": round(total_ms, 1),
            "response_length": len(answer),
        }
        self.last_attachment_meta = {
            "remote_calls": 1,
            "reasoning_calls": 0,
            "screen_capture_calls": 0,
            "source": "image_attachment",
            "direct_one_shot": True,
            "vision_provider": getattr(provider, "name", "deepseek-vision"),
            "vision_model": getattr(provider, "model", "unknown"),
        }
        return [
            AgentEvent.make(
                self.AGENT_ID,
                AgentEventType.FINAL,
                text=answer,
            )
        ], answer

    def _run_with_image(
        self,
        question: str,
        attachment: AttachmentImage,
        cancel_event: threading.Event,
    ) -> None:
        events, _ = self.perform_with_image(question, attachment, cancel_event)
        with self._lock:
            self._busy = False
            self._cancel_event = None
            self._thread = None
        for event in events:
            self.agent_event.emit(event)

    def _get_attachment_vision_provider(self) -> Any:
        """The shared FAST one-shot vision provider — TJU-Qwen (lazily;
        injectable)."""
        if self._attachment_vision_provider is None:
            from core.screen_vision.config import get_fast_direct_provider

            self._attachment_vision_provider = get_fast_direct_provider()
        return self._attachment_vision_provider

    def ask_with_document(self, question: str, attachment: DocumentAttachment) -> bool:
        """Start a document turn (local parsing + text QA, no screen/vision)."""
        if attachment is None:
            return self.ask(question)
        with self._lock:
            if self._busy:
                return False
            self._busy = True
            self._cancel_event = threading.Event()
            cancel_event = self._cancel_event

        self.agent_event.emit(
            AgentEvent.make(self.AGENT_ID, AgentEventType.STARTED)
        )
        self.agent_event.emit(
            AgentEvent.make(
                self.AGENT_ID,
                AgentEventType.STATUS,
                status=STATUS_THINKING,
            )
        )
        thread = threading.Thread(
            target=self._run_with_document,
            args=(question, attachment, cancel_event),
            daemon=True,
            name="FireflyDocumentAttachmentTurn",
        )
        with self._lock:
            self._thread = thread
        thread.start()
        return True

    def perform_with_document(
        self,
        question: str,
        attachment: DocumentAttachment,
        cancel_event: threading.Event | None = None,
    ) -> tuple[list[AgentEvent], str]:
        """Synchronously execute one document turn.

        With a document attachment the question defaults to being about the
        document (even for ordinary questions) unless the text explicitly says
        to ignore the file. Documents are parsed locally; only question-
        relevant excerpts go to the text provider (never Memory, never vision,
        never a screen capture).
        """
        text = (question or "").strip()
        if attachment is None:
            return self.perform(text, cancel_event)
        if _document_negated(text):
            return self.perform(text, cancel_event)
        event = cancel_event or threading.Event()
        if event.is_set():
            return [self._cancelled_event()], ""

        total_started = time.perf_counter()
        if not self._wait_for_document_parse(attachment):
            return [self._error_event(DOCUMENT_PARSING_REPLY, ErrorCategory.PROTOCOL)], ""
        parse_ms = 0.0
        context = attachment.context
        if context is None or attachment.parse_state == "error":
            message = _document_error_message(attachment.parse_error)
            return [self._error_event(message, ErrorCategory.PROTOCOL)], ""

        lazy = getattr(attachment, "lazy_state", None)
        effective_question = text or DEFAULT_DOCUMENT_QUESTION
        # Rule-based router decides between text QA / page lookup / vision /
        # scan OCR / summary. No model call, no second parser.
        route = route_document_question(effective_question, context, lazy_state=lazy)
        self._active_document_action = route.action.value
        self._active_document_route_reason = route.reason

        if route.action == DocumentAction.DOCUMENT_VISION:
            return self.perform_with_document_vision(text, attachment, cancel_event)

        if lazy is not None:
            return self._perform_lazy_pdf(text, attachment, lazy, event, total_started)

        provider = self._get_document_chat()
        retrieval_started = time.perf_counter()
        summary_stats: dict[str, Any] | None = None
        try:
            # Page/slide/sheet direct lookup wins over fuzzy retrieval AND
            # over summary intent ("第 6 页讲了什么" is page-specific).
            direct = direct_section_lookup(effective_question, context)
            if direct:
                excerpts = format_excerpts(direct)
                chunks_sent = len(direct)
                messages = build_qa_messages(context.filename, excerpts, effective_question)
                answer = _chat_answer(provider(messages, temperature=0.2))
                remote_calls = 1
            elif route.action == DocumentAction.SUMMARY:
                answer, remote_calls, summary_stats = summarize_document(
                    context, provider, cache=attachment.summary_cache
                )
                chunks_sent = int(summary_stats.get("groups", 1))
            else:
                top = retrieve_chunks(effective_question, chunk_document(context))
                excerpts = format_excerpts(top)
                chunks_sent = len(top)
                messages = build_qa_messages(context.filename, excerpts, effective_question)
                answer = _chat_answer(provider(messages, temperature=0.2))
                remote_calls = 1
        except Exception as exc:
            reason = _attachment_failure_label(exc)
            logger.info("document attachment QA failed: %s", reason)
            return [self._error_event(
                DOCUMENT_QA_FAILURE_REPLY.format(reason=reason),
                ErrorCategory.PROVIDER,
            )], ""
        retrieval_ms = (time.perf_counter() - retrieval_started) * 1000

        if not isinstance(answer, str) or not answer.strip():
            return [self._error_event(
                DOCUMENT_QA_FAILURE_REPLY.format(reason="empty_response"),
                ErrorCategory.PROVIDER,
            )], ""
        answer = answer.strip()
        if _has_ocr_failures(context):
            answer = answer + DOCUMENT_PARTIAL_OCR_NOTE
        if event.is_set():
            return [self._cancelled_event()], ""

        history_user = HISTORY_DOCUMENT_PLACEHOLDER.format(
            name=attachment.display_name
        )
        if text:
            history_user = f"{history_user} {text}"
        with self._lock:
            self._append_history(
                [
                    {"role": "user", "content": history_user},
                    {"role": "assistant", "content": answer},
                ]
            )
        total_ms = (time.perf_counter() - total_started) * 1000
        model_ms = max(total_ms - parse_ms - retrieval_ms, 0.0)
        timings = {
            "parse_ms": round(parse_ms, 1),
            "retrieval_ms": round(retrieval_ms, 1),
            "model_ms": round(model_ms, 1),
            "total_ms": round(total_ms, 1),
            "chunks_sent": int(chunks_sent),
        }
        if summary_stats is not None:
            local_prepare_ms = float(summary_stats.get("local_prepare_ms", 0.0))
            timings["local_prepare_ms"] = round(local_prepare_ms, 1)
            timings["model_ms"] = round(max(retrieval_ms - local_prepare_ms, 0.0), 1)
            timings["summary_input_chars"] = int(
                summary_stats.get("summary_input_chars", 0)
            )
        self.last_document_timings = timings
        self.last_document_meta = {
            "remote_calls": int(remote_calls),
            "reasoning_calls": 0,
            "screen_capture_calls": 0,
            "vision_calls": 0,
            "chunks_sent": int(chunks_sent),
            "source": "document_attachment",
            "kind": context.kind,
            "filename": context.filename,
            "document_action": self._active_document_action,
            "document_route_reason": self._active_document_route_reason,
        }
        if summary_stats is not None:
            self.last_document_meta["summary_level"] = summary_stats.get("level", "small")
            self.last_document_meta["summary_groups"] = int(summary_stats.get("groups", 1))
            self.last_document_meta["summary_input_chars"] = int(
                summary_stats.get("summary_input_chars", 0)
            )
            self.last_document_meta["references_downgraded"] = int(
                summary_stats.get("references_downgraded", 0)
            )
        return [
            AgentEvent.make(
                self.AGENT_ID,
                AgentEventType.FINAL,
                text=answer,
            )
        ], answer

    def perform_with_document_vision(
        self,
        question: str,
        attachment: DocumentAttachment,
        cancel_event: threading.Event | None = None,
    ) -> tuple[list[AgentEvent], str]:
        """Synchronously execute one document turn (router-driven entry).

        The router picks the handler: DOCUMENT_VISION renders the requested
        page/slide and answers with exactly one direct vision call (page image
        + page text + question); every other action falls through to the
        normal document QA. Never touches OCR, Memory, History or a screen
        capture outside the chosen handler.
        """
        text = (question or "").strip()
        if attachment is None:
            return self.perform(text, cancel_event)
        if _document_negated(text):
            return self.perform(text, cancel_event)
        event = cancel_event or threading.Event()
        if event.is_set():
            return [self._cancelled_event()], ""

        if not self._wait_for_document_parse(attachment):
            return [self._error_event(DOCUMENT_PARSING_REPLY, ErrorCategory.PROTOCOL)], ""
        context = attachment.context
        if context is None or attachment.parse_state == "error":
            message = _document_error_message(attachment.parse_error)
            return [self._error_event(message, ErrorCategory.PROTOCOL)], ""

        route = route_document_question(
            text, context, lazy_state=getattr(attachment, "lazy_state", None)
        )
        if route.action != DocumentAction.DOCUMENT_VISION:
            return self.perform_with_document(text, attachment, cancel_event)
        self._active_document_action = route.action.value
        self._active_document_route_reason = route.reason

        return self._run_document_vision_turn(text, attachment, context, event)

    def _run_document_vision_turn(
        self,
        text: str,
        attachment: DocumentAttachment,
        context: DocumentContext,
        event: threading.Event,
    ) -> tuple[list[AgentEvent], str]:
        """Render the requested page/slide and answer with one direct vision call.

        Shared by both document entries; only reached after the router decided
        DOCUMENT_VISION (or the explicit vision entry confirmed it).
        """
        location = resolve_document_location(text, context)
        if location is None:
            return [self._error_event(
                DOCUMENT_VISION_UNRESOLVED_REPLY, ErrorCategory.PROTOCOL
            )], ""
        total = context.page_count if context.kind == "pdf" else context.slide_count
        if total is None or location < 1 or location > total:
            return [self._error_event(
                DOCUMENT_VISION_OUT_OF_RANGE_REPLY, ErrorCategory.PROTOCOL
            )], ""

        source = attachment.take_source_bytes()
        if not source:
            return [self._error_event(
                DOCUMENT_VISION_NO_SOURCE_REPLY, ErrorCategory.PROTOCOL
            )], ""

        total_started = time.perf_counter()
        renderer = self._get_document_vision_renderer()
        render_started = time.perf_counter()
        try:
            if context.kind == "pdf":
                frame = renderer.render_pdf_page(source, location)
            elif context.kind == "pptx":
                frame = renderer.render_pptx_slide(source, location)
            else:
                return [self._error_event(
                    DOCUMENT_VISION_UNSUPPORTED_REPLY, ErrorCategory.PROTOCOL
                )], ""
        except PageOutOfRangeError:
            return [self._error_event(
                DOCUMENT_VISION_OUT_OF_RANGE_REPLY, ErrorCategory.PROTOCOL
            )], ""
        except Exception as exc:
            reason = _attachment_failure_label(exc)
            logger.info("document vision render failed: %s", reason)
            return [self._error_event(
                DOCUMENT_VISION_RENDER_FAILED_REPLY.format(reason=reason),
                ErrorCategory.PROVIDER,
            )], ""
        render_ms = (time.perf_counter() - render_started) * 1000

        location_label = f"Page {location}" if context.kind == "pdf" else f"Slide {location}"
        section = next(
            (s for s in context.sections if s.label == location_label), None
        )
        page_text = (section.text if section is not None else "") or ""

        provider = self._get_attachment_vision_provider()
        vision_started = time.perf_counter()
        try:
            answer = provider.answer_direct(
                frame,
                build_document_vision_question(text, location_label, page_text),
                style_context=DOCUMENT_VISION_STYLE_CONTEXT,
            )
        except Exception as exc:
            vision_ms = (time.perf_counter() - vision_started) * 1000
            reason = _attachment_failure_label(exc)
            logger.info(
                "document vision failed: %s (%s ms)",
                reason,
                round(vision_ms, 1),
            )
            return [self._error_event(
                DOCUMENT_VISION_FAILURE_REPLY.format(reason=reason),
                ErrorCategory.PROVIDER,
            )], ""
        vision_ms = (time.perf_counter() - vision_started) * 1000

        if not isinstance(answer, str) or not answer.strip():
            return [self._error_event(
                DOCUMENT_VISION_FAILURE_REPLY.format(reason="empty_response"),
                ErrorCategory.PROVIDER,
            )], ""
        answer = answer.strip()
        if event.is_set():
            return [self._cancelled_event()], ""

        history_user = HISTORY_DOCUMENT_PLACEHOLDER.format(
            name=attachment.display_name
        )
        if text:
            history_user = f"{history_user} {text}"
        with self._lock:
            self._append_history(
                [
                    {"role": "user", "content": history_user},
                    {"role": "assistant", "content": answer},
                ]
            )
        total_ms = (time.perf_counter() - total_started) * 1000
        self.last_document_vision_timings = {
            "render_ms": round(render_ms, 1),
            "vision_ms": round(vision_ms, 1),
            "total_ms": round(total_ms, 1),
            "response_length": len(answer),
            "location": int(location),
        }
        self.last_document_vision_meta = {
            "remote_calls": 1,
            "reasoning_calls": 0,
            "screen_capture_calls": 0,
            "vision_calls": 1,
            "source": "document_vision",
            "kind": context.kind,
            "filename": context.filename,
            "location": int(location),
            "page_text_chars": len(page_text),
            "document_action": self._active_document_action,
            "document_route_reason": self._active_document_route_reason,
            "vision_provider": getattr(provider, "name", "deepseek-vision"),
            "vision_model": getattr(provider, "model", "unknown"),
        }
        return [
            AgentEvent.make(
                self.AGENT_ID,
                AgentEventType.FINAL,
                text=answer,
            )
        ], answer

    def _run_with_document(
        self,
        question: str,
        attachment: DocumentAttachment,
        cancel_event: threading.Event,
    ) -> None:
        events, _ = self.perform_with_document(question, attachment, cancel_event)
        with self._lock:
            self._busy = False
            self._cancel_event = None
            self._thread = None
        for event in events:
            self.agent_event.emit(event)

    def _get_document_chat(self) -> Any:
        """The text chat handler for document QA (lazily; injectable).

        Uses ``core.ai_router.chat`` directly: the router performs no memory
        retrieval, no bond update and no suggestion extraction.
        """
        if self._document_chat_handler is None:
            from core.ai_router import chat

            self._document_chat_handler = chat
        return self._document_chat_handler

    def _get_document_vision_renderer(self) -> Any:
        """The Document Vision page/slide renderer (lazily; injectable)."""
        if self._document_vision_renderer is None:
            from core.document_vision import DocumentVisionRenderer

            self._document_vision_renderer = DocumentVisionRenderer()
        return self._document_vision_renderer

    def _wait_for_document_parse(
        self, attachment: DocumentAttachment, timeout: float = 60.0
    ) -> bool:
        """Block (worker thread only) until background parsing settles."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = attachment.parse_state
            if state in ("ready", "error"):
                return True
            time.sleep(0.05)
        return False

    # ------------------------------------------------ lazy scanned PDF turns

    def _perform_lazy_pdf(
        self,
        question: str,
        attachment: DocumentAttachment,
        lazy: LazyPdfOcrState,
        event: threading.Event,
        total_started: float,
    ) -> tuple[list[AgentEvent], str]:
        """One turn against a scanned PDF with lazy on-demand OCR."""
        text = (question or "").strip()
        effective = text or DEFAULT_DOCUMENT_QUESTION
        provider = self._get_document_chat()
        ocr_fn = getattr(attachment, "lazy_ocr_fn", None) or self._get_lazy_ocr_fn()
        source = attachment.take_source_bytes()

        if is_full_ocr_request(effective):
            if not self._full_ocr_active(attachment):
                self.start_full_pdf_ocr(attachment)
            answer = "已开始后台全文识别，可以随时继续提问；识别进度会显示在附件上。"
            return self._finish_lazy_turn(
                answer, attachment, lazy, [], 0, 0.0,
                ocr_mode="full_background", total_started=total_started,
            )
        if is_continue_scan_request(effective):
            batch = lazy.pick_next_batch()
            if batch:
                self._run_ocr_batch(attachment, lazy, batch, ocr_fn, source)
                answer = f"已继续识别第 {batch[0]}–{batch[-1]} 页。"
            else:
                answer = "所有页面都已识别完成。"
            return self._finish_lazy_turn(
                answer, attachment, lazy, list(batch), 0, 0.0,
                ocr_mode="progressive", total_started=total_started,
            )

        pages = lazy.requested_pages(effective)
        if pages is not None:
            return self._answer_lazy_pages(
                effective, attachment, lazy, pages, ocr_fn, source, provider,
                event, total_started,
            )
        if is_summary_request(effective):
            return self._answer_lazy_summary(
                attachment, lazy, ocr_fn, source, provider, event, total_started,
            )
        return self._answer_lazy_generic(
            effective, attachment, lazy, ocr_fn, source, provider,
            event, total_started,
        )

    def _answer_lazy_pages(
        self,
        question: str,
        attachment: DocumentAttachment,
        lazy: LazyPdfOcrState,
        pages: list[int],
        ocr_fn,
        source: bytes,
        provider,
        event: threading.Event,
        total_started: float,
    ) -> tuple[list[AgentEvent], str]:
        capped = pages[:MAX_LAZY_OCR_PAGES_PER_TURN]
        cache_hits = 0
        ocr_started = time.perf_counter()
        needed = []
        for page in capped:
            state = lazy.page_state(page)
            if state in (PdfPageStatus.OCR_DONE, PdfPageStatus.NATIVE):
                cache_hits += 1
            else:
                needed.append(page)
        if needed:
            self._run_ocr_batch(attachment, lazy, needed, ocr_fn, source)
        ocr_ms = (time.perf_counter() - ocr_started) * 1000

        usable = [(p, lazy.page_text(p)) for p in capped if lazy.page_text(p).strip()]
        if not usable:
            # OCR got nothing (formula / figure pages often do). Give the
            # vision model the rendered page instead of a dead "无法识别".
            answer = self._try_lazy_vision_fallback(
                question, attachment, source, capped[0], event
            )
            if answer is None:
                answer = PAGE_OCR_FAILED_REPLY
            return self._finish_lazy_turn(
                answer, attachment, lazy, pages, cache_hits, ocr_ms,
                ocr_mode="lazy", total_started=total_started,
            )
        excerpts = "\n\n".join(f"[Page {p}]\n{t}" for p, t in usable)
        if len(pages) > MAX_LAZY_OCR_PAGES_PER_TURN:
            excerpts = excerpts + "\n\n" + BATCH_LIMIT_NOTE
        messages = build_qa_messages(attachment.display_name, excerpts, question)
        answer = _chat_answer(provider(messages, temperature=0.2))
        if event.is_set():
            return [self._cancelled_event()], ""
        return self._finish_lazy_turn(
            answer, attachment, lazy, pages, cache_hits, ocr_ms,
            ocr_mode="lazy", total_started=total_started,
        )

    def _try_lazy_vision_fallback(
        self,
        question: str,
        attachment: DocumentAttachment,
        source: bytes,
        page: int,
        event: threading.Event,
    ) -> str | None:
        """When a page's OCR is empty, answer from the rendered page image.

        Reuses the exact DOCUMENT_VISION pipeline (renderer + attachment
        vision provider + document vision question); returns None so callers
        can fall back to the existing OCR-failure reply.
        """
        if not source or event.is_set():
            return None
        try:
            renderer = self._get_document_vision_renderer()
            frame = renderer.render_pdf_page(source, page)
        except Exception as exc:
            logger.info(
                "lazy vision fallback render failed (page %s): %s", page,
                _attachment_failure_label(exc),
            )
            return None
        try:
            provider = self._get_attachment_vision_provider()
            answer = provider.answer_direct(
                frame,
                build_document_vision_question(question, f"Page {page}", ""),
                style_context=DOCUMENT_VISION_STYLE_CONTEXT,
            )
        except Exception as exc:
            logger.info(
                "lazy vision fallback failed (page %s): %s", page,
                _attachment_failure_label(exc),
            )
            return None
        if not isinstance(answer, str) or not answer.strip():
            return None
        return answer.strip()

    def _answer_lazy_summary(
        self,
        attachment: DocumentAttachment,
        lazy: LazyPdfOcrState,
        ocr_fn,
        source: bytes,
        provider,
        event: threading.Event,
        total_started: float,
    ) -> tuple[list[AgentEvent], str]:
        coverage = lazy.coverage_ratio()
        ocr_ms = 0.0
        cache_hits = 0
        ocr_started = time.perf_counter()
        if coverage < SUMMARY_COVERAGE_THRESHOLD and lazy.ocr_pages_pending():
            seed = lazy.pick_summary_seed_pages(MAX_SUMMARY_SEED_OCR_PAGES)
            if seed:
                self._run_ocr_batch(attachment, lazy, seed, ocr_fn, source)
        ocr_ms = (time.perf_counter() - ocr_started) * 1000
        context = lazy.build_context()
        answer, calls, stats = summarize_document(
            context, provider, cache=attachment.summary_cache
        )
        preliminary = coverage < SUMMARY_COVERAGE_THRESHOLD
        if preliminary:
            answer = (
                "基于目前已识别的代表性页面，先给你一个初步概览"
                "（这是部分页面的初步总结，不是完整全文总结）：\n\n" + answer
            )
        if event.is_set():
            return [self._cancelled_event()], ""
        return self._finish_lazy_turn(
            answer, attachment, lazy, [], cache_hits, ocr_ms,
            ocr_mode="progressive" if preliminary else "native",
            total_started=total_started, remote_calls=calls,
            summary_input_chars=stats.get("summary_input_chars"),
        )

    def _answer_lazy_generic(
        self,
        question: str,
        attachment: DocumentAttachment,
        lazy: LazyPdfOcrState,
        ocr_fn,
        source: bytes,
        provider,
        event: threading.Event,
        total_started: float,
    ) -> tuple[list[AgentEvent], str]:
        context = lazy.build_context()
        top = retrieve_chunks(question, chunk_document(context))
        has_evidence = any(_chunk_matches(chunk, question) for chunk in top)
        ocr_ms = 0.0
        cache_hits = 0
        progressive_used = False
        if not has_evidence and lazy.ocr_pages_pending():
            ocr_started = time.perf_counter()
            batch = lazy.pick_progressive_pages(question, MAX_PROGRESSIVE_OCR_PAGES_PER_TURN)
            if batch:
                self._run_ocr_batch(attachment, lazy, batch, ocr_fn, source)
                progressive_used = True
                context = lazy.build_context()
                top = retrieve_chunks(question, chunk_document(context))
                has_evidence = any(_chunk_matches(chunk, question) for chunk in top)
            ocr_ms = (time.perf_counter() - ocr_started) * 1000
        if not has_evidence:
            return self._finish_lazy_turn(
                LOW_COVERAGE_REPLY, attachment, lazy, [], cache_hits, ocr_ms,
                ocr_mode="progressive" if progressive_used else "lazy",
                total_started=total_started,
            )
        excerpts = format_excerpts(top)
        messages = build_qa_messages(attachment.display_name, excerpts, question)
        answer = _chat_answer(provider(messages, temperature=0.2))
        if event.is_set():
            return [self._cancelled_event()], ""
        return self._finish_lazy_turn(
            answer, attachment, lazy, [], cache_hits, ocr_ms,
            ocr_mode="progressive" if progressive_used else "lazy",
            total_started=total_started,
        )

    def _run_ocr_batch(
        self, attachment: DocumentAttachment, lazy: LazyPdfOcrState,
        pages: list[int], ocr_fn, source: bytes,
    ) -> None:
        for page in pages:
            if lazy.cancel_event.is_set():
                return
            self.ocr_page.emit(attachment, page)
        run_ocr_batch(lazy, pages, ocr_fn, source)
        # New OCR pages invalidate the derived retrieval/summary caches.
        attachment.clear_summary_cache()

    def _get_lazy_ocr_fn(self):
        from core.pdf_processor import ocr_pdf_pages

        return ocr_pdf_pages

    def _full_ocr_active(self, attachment: DocumentAttachment) -> bool:
        lazy = getattr(attachment, "lazy_state", None)
        return bool(getattr(lazy, "full_job_active", False))

    def start_full_pdf_ocr(self, attachment: DocumentAttachment) -> None:
        """Start a single-worker background full-OCR job for a lazy PDF.

        Cancellable and generation-guarded: the job only ever writes to the
        lazy state it was started with, so a newer attachment can never
        receive stale results.
        """
        lazy = getattr(attachment, "lazy_state", None)
        if lazy is None or getattr(lazy, "full_job_active", False):
            return
        lazy.full_job_active = True
        ocr_fn = getattr(attachment, "lazy_ocr_fn", None) or self._get_lazy_ocr_fn()

        def work() -> None:
            try:
                source = attachment.take_source_bytes()
                while not lazy.cancel_event.is_set():
                    batch = lazy.pick_next_batch(1)  # exactly one worker/page
                    if not batch:
                        break
                    self._run_ocr_batch(attachment, lazy, batch, ocr_fn, source)
                    self.ocr_progress.emit(
                        attachment, lazy.ocr_pages_completed(), lazy.page_count
                    )
                self.ocr_progress.emit(
                    attachment, lazy.ocr_pages_completed(), lazy.page_count
                )
            finally:
                lazy.full_job_active = False

        threading.Thread(
            target=work, daemon=True, name="FireflyFullPdfOcr"
        ).start()

    def _finish_lazy_turn(
        self,
        answer: str,
        attachment: DocumentAttachment,
        lazy: LazyPdfOcrState,
        requested_pages: list[int],
        cache_hits: int,
        ocr_ms: float,
        *,
        ocr_mode: str,
        total_started: float,
        remote_calls: int = 1,
        summary_input_chars: int | None = None,
    ) -> tuple[list[AgentEvent], str]:
        if not isinstance(answer, str) or not answer.strip():
            return [self._error_event(
                DOCUMENT_QA_FAILURE_REPLY.format(reason="empty_response"),
                ErrorCategory.PROVIDER,
            )], ""
        answer = answer.strip()
        history_user = HISTORY_DOCUMENT_PLACEHOLDER.format(
            name=attachment.display_name
        )
        with self._lock:
            self._append_history(
                [
                    {"role": "user", "content": history_user},
                    {"role": "assistant", "content": answer},
                ]
            )
        total_ms = (time.perf_counter() - total_started) * 1000
        self.last_document_timings = {
            "total_ms": round(total_ms, 1),
            "ocr_ms": round(ocr_ms, 1),
            "response_length": len(answer),
        }
        index_ms = getattr(attachment, "lazy_index_ms", 0.0)
        self.last_document_meta = {
            "remote_calls": int(remote_calls),
            "reasoning_calls": 0,
            "screen_capture_calls": 0,
            "vision_calls": 0,
            "source": "document_attachment",
            "kind": "pdf",
            "filename": attachment.display_name,
            "document_action": self._active_document_action,
            "document_route_reason": self._active_document_route_reason,
            "ocr_mode": ocr_mode,
            "ocr_pages_requested": int(len(requested_pages)),
            "ocr_pages_completed": int(lazy.ocr_pages_completed()),
            "ocr_cache_hits": int(cache_hits),
            "ocr_ms": round(ocr_ms, 1),
            "ocr_coverage_ratio": round(lazy.coverage_ratio(), 3),
            "pdf_initial_index_ms": round(index_ms, 1),
        }
        if summary_input_chars is not None:
            self.last_document_meta["summary_input_chars"] = int(summary_input_chars)
        return [self._final_event(answer)], answer

    def _final_event(self, text: str) -> AgentEvent:
        return AgentEvent.make(self.AGENT_ID, AgentEventType.FINAL, text=text)

    def stop(self) -> None:
        """Logically cancel an in-flight request and discard its late reply."""
        with self._lock:
            if self._cancel_event is not None:
                self._cancel_event.set()

    def clear_history(self) -> None:
        """Clear only the in-process conversation history, never long-term memory."""
        with self._lock:
            self._history.clear()

    def perform(
        self,
        prompt: str,
        cancel_event: threading.Event | None = None,
        learning_result: Any | None = None,
    ) -> tuple[list[AgentEvent], str]:
        """Synchronously execute one turn for deterministic offline testing."""
        text = (prompt or "").strip()
        if not text:
            return [self._error_event("empty user message", ErrorCategory.PROTOCOL)], ""
        event = cancel_event or threading.Event()
        if event.is_set():
            return [self._cancelled_event()], ""
        with self._lock:
            history = [dict(message) for message in self._history]

        video_bvid = detect_bilibili_reference(text)
        if video_bvid and not self._video_analysis_enabled():
            # Capability gate: refuse BEFORE any video data is read (no
            # download, no transcript fetch, no FFmpeg, no provider call).
            answer = VIDEO_ANALYSIS_DISABLED_REPLY
            with self._lock:
                self._append_history(
                    [
                        {"role": "user", "content": text},
                        {"role": "assistant", "content": answer},
                    ]
                )
            return [
                AgentEvent.make(
                    self.AGENT_ID,
                    AgentEventType.FINAL,
                    text=answer,
                )
            ], answer
        if video_bvid:
            # Minimal video reading route: a Bilibili URL/BV id in the message
            # is answered by the BiliInsight pipeline (metadata + transcript +
            # AI Router summary) instead of the ordinary companion chat.
            self.agent_event.emit(
                AgentEvent.make(
                    self.AGENT_ID,
                    AgentEventType.STATUS,
                    status=STATUS_READING,
                )
            )
            try:
                result = analyze_video_message(text)
                answer = result.to_answer()
            except Exception as exc:  # video failure must not crash the turn
                answer = video_reading_failure_reply(exc)
            else:
                self._session_video = SessionVideoContext.from_result(result)
                answer = result.to_answer()
                if is_study_entry(text) and self._video_analysis_enabled():
                    # "陪我学习这个视频 BVxxx": analyze, then enter study mode.
                    self._video_study = VideoStudyContext.from_session(
                        self._session_video)
                    answer += f"\n\n{STUDY_ENTRY_REPLY}"
            if event.is_set():
                return [self._cancelled_event()], ""
            with self._lock:
                self._append_history(
                    [
                        {"role": "user", "content": text},
                        {"role": "assistant", "content": answer},
                    ]
                )
            return [
                AgentEvent.make(
                    self.AGENT_ID,
                    AgentEventType.FINAL,
                    text=answer,
                )
            ], answer

        # TJU Info Retrieval: explicit「打开 TJU 信息检索系统」(open_ui).
        # Deliberately earlier than the search trigger and disjoint from it.
        if _tju_open_request(text):
            if self._tju_retrieval_open_ui is None:
                answer = "（TJU Info Retrieval 打开动作未接线。）"
            else:
                try:
                    answer = self._tju_retrieval_open_ui()
                except Exception as exc:  # noqa: BLE001 - never leak internals
                    safe = str(exc).strip().replace("\n", " ")
                    answer = f"（无法打开 TJU Info Retrieval：{safe[:160]}）"
            with self._lock:
                self._append_history(
                    [
                        {"role": "user", "content": text},
                        {"role": "assistant", "content": answer},
                    ]
                )
            return [
                AgentEvent.make(
                    self.AGENT_ID,
                    AgentEventType.FINAL,
                    text=answer,
                )
            ], answer

        # TJU Info Retrieval: explicit re-login trigger — opens the SAME
        # controlled Edge (via the plugin) for a MANUAL TJU login. Never
        # touches credentials; no auto-resume of the previous query.
        if _tju_relogin_request(text):
            if not self._tju_retrieval_enabled():
                answer = TJU_RETRIEVAL_DISABLED_REPLY
            elif self._tju_retrieval_open_login is None:
                answer = "（TJU 信息检索重新登录不可用。）"
            else:
                try:
                    answer = self._tju_retrieval_open_login()
                except Exception as exc:  # noqa: BLE001 - never leak internals
                    safe = str(exc).strip().replace("\n", " ")
                    answer = f"（无法打开受控浏览器：{safe[:160]}）"
            with self._lock:
                self._append_history(
                    [
                        {"role": "user", "content": text},
                        {"role": "assistant", "content": answer},
                    ]
                )
            return [
                AgentEvent.make(
                    self.AGENT_ID,
                    AgentEventType.FINAL,
                    text=answer,
                )
            ], answer

        # TJU Info Retrieval: explicit trigger ("用 TJU 信息检索查…"). Gated
        # BEFORE any subprocess/API call — Off means the retrieval system is
        # never invoked, with a user-readable reply.
        tju_query = _tju_query(text)
        if tju_query:
            if not self._tju_retrieval_enabled():
                answer = TJU_RETRIEVAL_DISABLED_REPLY
            elif self._tju_retrieval_search is None:
                answer = "（TJU 信息检索适配器未接线。）"
            else:
                try:
                    results = self._tju_retrieval_search(tju_query, top_k=5)
                    answer = _format_tju_results(tju_query, results)
                except Exception as exc:  # noqa: BLE001 - never leak internals
                    if type(exc).__name__ == "TjuAuthRequiredError":
                        # 登录过期 ≠ 普通错误：给用户可操作的重新登录提示。
                        answer = TJU_RETRIEVAL_AUTH_REQUIRED_REPLY
                    else:
                        safe = str(exc).strip().replace("\n", " ")
                        answer = f"（TJU 信息检索暂时不可用：{safe[:160]}）"
            with self._lock:
                self._append_history(
                    [
                        {"role": "user", "content": text},
                        {"role": "assistant", "content": answer},
                    ]
                )
            return [
                AgentEvent.make(
                    self.AGENT_ID,
                    AgentEventType.FINAL,
                    text=answer,
                )
            ], answer

        # Session video timestamp follow-up ("刚才2分钟那里是什么？"): only
        # when a video was read this session AND the message carries a time
        # expression within its duration. Otherwise the ordinary paths run.
        frame_seconds = None
        if self._session_video is not None:
            frame_seconds = parse_video_time_expression(
                text, duration_hint=self._session_video.duration_s or None)
            duration_s = self._session_video.duration_s or 0.0
            if frame_seconds is not None and duration_s > 0 and frame_seconds >= duration_s:
                frame_seconds = None  # "花30分钟装环境" is not a video timestamp
        if frame_seconds is not None:
            self.agent_event.emit(
                AgentEvent.make(
                    self.AGENT_ID,
                    AgentEventType.STATUS,
                    status=STATUS_READING,
                )
            )
            try:
                # Tell the vision model where this frame sits in the video, so
                # it answers the question instead of doubting the timestamp.
                framed_question = (
                    f"（这是视频进行到 {format_timestamp(frame_seconds)} 时的画面帧）{text}"
                )
                frame_analysis = analyze_video_frame(
                    self._session_video.url, frame_seconds, question=framed_question)
                answer = (
                    f"我看了一下，{format_timestamp(frame_seconds)}那里："
                    f"{frame_analysis.description}"
                )
            except Exception as exc:  # frame failure must not crash the turn
                answer = (
                    f"抱歉，{format_timestamp(frame_seconds)}那里的画面我没能看到。"
                    f"（{video_reading_failure_reply(exc)}）"
                )
            if event.is_set():
                return [self._cancelled_event()], ""
            with self._lock:
                self._append_history(
                    [
                        {"role": "user", "content": text},
                        {"role": "assistant", "content": answer},
                    ]
                )
            return [
                AgentEvent.make(
                    self.AGENT_ID,
                    AgentEventType.FINAL,
                    text=answer,
                )
            ], answer

        # Study companion mode (Phase 8): entry / quiz / grading / exit.
        # In the "watching" stage only explicit study markers are intercepted —
        # other messages keep the ordinary chat path. While "quizzing", the
        # next message counts as the learner's answer.
        study_intercepts = (
            self._video_study is not None
            and self._video_analysis_enabled()
            and (self._video_study.stage == "quizzing"
                 or is_study_exit(text) or is_quiz_request(text) or is_study_entry(text))
        ) or (
            self._video_study is None
            and self._session_video is not None
            and is_study_entry(text)
            and self._video_analysis_enabled()
        )
        if study_intercepts and self._session_video is not None:
            self.agent_event.emit(
                AgentEvent.make(
                    self.AGENT_ID,
                    AgentEventType.STATUS,
                    status=STATUS_THINKING,
                )
            )
            try:
                answer, self._video_study = handle_study_turn(
                    text, self._video_study, self._session_video)
            except Exception as exc:  # study failure must not crash the turn
                answer = study_failure_reply(exc)
            if event.is_set():
                return [self._cancelled_event()], ""
            with self._lock:
                self._append_history(
                    [
                        {"role": "user", "content": text},
                        {"role": "assistant", "content": answer},
                    ]
                )
            return [
                AgentEvent.make(
                    self.AGENT_ID,
                    AgentEventType.FINAL,
                    text=answer,
                )
            ], answer

        turn_context = None
        camera_request = is_camera_vision_request(text)
        if camera_request:
            self._camera_trace("T0_user_trigger_camera")
        if camera_request and not bool(self._camera_vision_enabled()):
            answer = CAMERA_VISION_DISABLED_REPLY
            with self._lock:
                self._append_history(
                    [
                        {"role": "user", "content": text},
                        {"role": "assistant", "content": answer},
                    ]
                )
            return [AgentEvent.make(
                self.AGENT_ID,
                AgentEventType.FINAL,
                text=answer,
            )], answer
        if (is_look_command(text) or is_explicit_screen_vision_request(text)
                or camera_request):
            self.agent_event.emit(
                AgentEvent.make(
                    self.AGENT_ID,
                    AgentEventType.STATUS,
                    status=STATUS_READING,
                )
            )
            capture_mode = resolve_capture_target(screen_vision_question(text))
            is_camera = capture_mode == "camera"
            try:
                self._camera_trace("T1_worker_entering_vision_look", f"mode={capture_mode}")
                result = self._get_screen_vision_service().look(
                    screen_vision_question(text),
                    capture_mode=capture_mode,
                )
            except Exception as exc:  # vision failure must not crash the turn
                from core.screen_vision.provider_errors import (
                    ReasoningTemporarilyUnavailable,
                    VisionTemporarilyUnavailable,
                )

                from core.screen_vision.safety import sanitize_error_text

                logger.warning(
                    "%s vision look failed: %s: %s",
                    "Camera" if is_camera else "Screen",
                    type(exc).__name__,
                    sanitize_error_text(str(exc)),
                )
                if is_camera:
                    from core.screen_vision.screen.camera import CameraUnavailableError

                    if isinstance(exc, CameraUnavailableError):
                        answer = CAMERA_VISION_UNAVAILABLE_REPLY
                    elif isinstance(exc, ReasoningTemporarilyUnavailable):
                        answer = CAMERA_VISION_REASONING_UNAVAILABLE_REPLY
                    else:
                        # User-facing copy: never expose the internal
                        # exception type/HTTP/billing details; the full error
                        # stays in the logger line above.
                        answer = CAMERA_VISION_FAILURE_REPLY
                elif isinstance(exc, VisionTemporarilyUnavailable):
                    answer = SCREEN_VISION_UNAVAILABLE_REPLY
                elif isinstance(exc, ReasoningTemporarilyUnavailable):
                    answer = SCREEN_VISION_REASONING_UNAVAILABLE_REPLY
                else:
                    answer = SCREEN_VISION_FAILURE_REPLY.format(
                        reason=type(exc).__name__
                    )
                with self._lock:
                    self._append_history(
                        [
                            {"role": "user", "content": text},
                            {"role": "assistant", "content": answer},
                        ]
                    )
                return [AgentEvent.make(
                    self.AGENT_ID,
                    AgentEventType.FINAL,
                    text=answer,
                )], answer
            self._pending_vision_timings = dict(result.timings)
            self._pending_vision_meta = dict(result.meta)
            if result.meta.get("capture_mode") == "camera":
                # Vision-1B: record the most recent camera observation at
                # session level and notify the companion UI via the bus.
                # Text only, never pixels. Publishing must never break the
                # visual answer that is about to be returned.
                self.last_camera_observation = result.answer
                self.last_camera_observation_at = time.time()
                if self._runtime_bus is not None and result.answer:
                    try:
                        self._runtime_bus.publish_event(
                            RuntimeEvent(
                                kind="camera.observed",
                                source="character_conversation_runner",
                                timestamp=int(time.time() * 1000),
                                payload=CameraObservedEvent(text=result.answer),
                            )
                        )
                    except Exception as exc:  # noqa: BLE001 - event must not fail the turn
                        logger.warning(
                            "camera.observed publish failed: %s",
                            type(exc).__name__,
                        )
            if result.meta.get("direct_one_shot") is True:
                # FAST already produced the final natural-language companion
                # answer. Do not send it, the screenshot question, or chat
                # history through a second reasoning/chat provider.
                answer = result.answer
                self._camera_trace("T10_caller_received_answer", f"mode={capture_mode}")
                if event.is_set():
                    return [self._cancelled_event()], ""
                self.last_screen_vision_timings = dict(result.timings)
                self.last_screen_vision_meta = dict(result.meta)
                logger.info(
                    "Screen vision turn timings: %s",
                    self.last_screen_vision_timings,
                )
                with self._lock:
                    self._append_history(
                        [
                            {"role": "user", "content": text},
                            {"role": "assistant", "content": answer},
                        ]
                    )
                return [AgentEvent.make(
                    self.AGENT_ID,
                    AgentEventType.FINAL,
                    text=answer,
                )], answer
            turn_context = format_screen_vision_context(result)

        if (turn_context is None and self._session_video is not None
                and is_video_followup(text)
                and self._video_analysis_enabled()):
            # "刚才那个视频里面…" — inject the last read video (summary +
            # transcript excerpt) through the existing single-turn
            # turn_context channel. Gated by the Video Analysis capability:
            # when it is off, video-derived context must not be injected (the
            # assistant cannot answer "from the video"). Never persisted;
            # ordinary messages keep turn_context None.
            turn_context = self._session_video.to_context_block()

        camera_reuse_blocked = (
            self.last_camera_observation is not None
            and _recent_visual_window_valid(self.last_camera_observation_at)
            and _is_recent_visual_followup(text)
            and not self._camera_vision_enabled()
        )
        if camera_reuse_blocked:
            # Camera Vision off: never reuse a stale camera observation in a
            # NEW turn. Refuse before any observation text is injected; no
            # camera open, no provider call, no old data leak.
            answer = CAMERA_OBSERVATION_DISABLED_REPLY
            with self._lock:
                self._append_history(
                    [
                        {"role": "user", "content": text},
                        {"role": "assistant", "content": answer},
                    ]
                )
            return [
                AgentEvent.make(
                    self.AGENT_ID,
                    AgentEventType.FINAL,
                    text=answer,
                )
            ], answer

        if (turn_context is None
                and self.last_camera_observation is not None
                and _recent_visual_window_valid(self.last_camera_observation_at)
                and _is_recent_visual_followup(text)
                and self._camera_vision_enabled()):
            # Vision-1C: explicit "刚看到你" follow-up — inject the most
            # recent camera observation (text only) through the same
            # turn_context channel. Window-limited (5 min), never persisted,
            # never pixel data; ordinary and video messages stay untouched.
            turn_context = _camera_note_context_block(self.last_camera_observation)

        contract = None
        self.last_contract_state = None
        if learning_result is not None:
            # Phase 9B-1: the orchestrator already computed the four contexts;
            # the runner uses them directly — NO recomputation. The response
            # contract (course / chapter / next step / required elements)
            # constrains the final answer.
            from core.learning.orchestrator import build_response_contract

            contract = build_response_contract(learning_result)
            blocks = []
            result_block = getattr(learning_result, "context_block", None)
            if result_block:
                blocks.append(result_block)
            contract_block = contract.prompt_block() if contract is not None else None
            if contract_block:
                blocks.append(contract_block)
            if blocks:
                turn_context = "\n\n".join(blocks)
        elif (turn_context is None
                and self.learning_controller is not None):
            # Phase 1B: while learning mode is enabled, inject the light
            # learning context (mode + course + teaching principles) through
            # the same channel. Never persisted, never affects routing.
            # Phase 3/4/5: teaching posture, next-action decision and prepared
            # action guidance are appended by the same read-only composition.
            # Phase 6: the composition itself moved into the orchestrator
            # (learning_loop_block) — one frozen flow order, still read-only
            # (it never calls handle_text, so it cannot double-record or
            # double-start an assessment). The runner decides nothing.
            loop_block = getattr(self.learning_controller, "learning_loop_block", None)
            if callable(loop_block):
                turn_context = loop_block()

        companion_started = time.perf_counter()
        try:
            response = self.runtime.chat(text, history=history, turn_context=turn_context)
            answer = extract_assistant_text(response)
            if contract is not None:
                # Phase 9B-1: the contract is ENFORCED on the final answer —
                # one deterministic retry when the anchors are missing. The
                # check itself is pure (no LLM judge).
                missing = contract.missing_anchors(answer)
                if missing:
                    retry_context = "\n\n".join([
                        turn_context or "",
                        "你上一次的回答没有覆盖学习契约中的要素，"
                        "请重新回答并覆盖：课程定位、第一学习任务、下一步交互。",
                    ])
                    retry = self.runtime.chat(
                        text, history=history, turn_context=retry_context)
                    answer2 = extract_assistant_text(retry)
                    if not contract.missing_anchors(answer2):
                        answer = answer2
                        missing = ()
                self.last_contract_state = {
                    "met": not missing,
                    "missing": missing,
                    "action": contract.action,
                }
        except Exception as exc:  # provider/runtime failures must not crash Qt
            return [self._error_event(str(exc), ErrorCategory.PROVIDER)], ""
        companion_ms = (time.perf_counter() - companion_started) * 1000
        if event.is_set():
            return [self._cancelled_event()], ""
        if turn_context is not None:
            vision_timings = getattr(self, "_pending_vision_timings", {}) or {}
            self.last_screen_vision_meta = dict(
                getattr(self, "_pending_vision_meta", {}) or {}
            )
            total_ms = vision_timings.get("total_ms", 0.0) + companion_ms
            self.last_screen_vision_timings = {
                **vision_timings,
                "companion_ms": round(companion_ms, 1),
                "total_ms": round(total_ms, 1),
            }
            logger.info(
                "Screen vision turn timings: %s",
                self.last_screen_vision_timings,
            )
        with self._lock:
            self._append_history(
                [
                    {"role": "user", "content": text},
                    {"role": "assistant", "content": answer},
                ]
            )
        return [
            AgentEvent.make(
                self.AGENT_ID,
                AgentEventType.FINAL,
                text=answer,
            )
        ], answer

    def _run(self, prompt: str, cancel_event: threading.Event,
             learning_result: Any | None = None) -> None:
        events, _ = self.perform(prompt, cancel_event,
                                 learning_result=learning_result)
        with self._lock:
            self._busy = False
            self._cancel_event = None
            self._thread = None
        for event in events:
            self.agent_event.emit(event)

    def _error_event(self, message: str, category: ErrorCategory) -> AgentEvent:
        safe_message = (message or "character conversation failed").strip()
        return AgentEvent.make(
            self.AGENT_ID,
            AgentEventType.ERROR,
            text=safe_message,
            error_code=category,
        )

    def _cancelled_event(self) -> AgentEvent:
        return AgentEvent.make(
            self.AGENT_ID,
            AgentEventType.CANCELLED,
            error_code=ErrorCategory.CANCELLED,
        )


def _attachment_negated(text: str) -> bool:
    """True when the user explicitly asked to ignore the attached image."""
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _ATTACHMENT_NEGATION_MARKERS)


def _chunk_matches(chunk: Any, question: str) -> bool:
    """Cheap evidence check: does any question term appear in the chunk?"""
    from core.lazy_pdf_ocr import _question_terms

    lowered = (chunk.text or "").lower()
    return any(term in lowered for term in _question_terms(question))


def _document_negated(text: str) -> bool:
    """True when the user explicitly asked to ignore the attached document."""
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _DOCUMENT_NEGATION_MARKERS)


def _document_error_message(parse_error: str | None) -> str:
    """Map a document parse failure to a friendly user-facing message."""
    error_text = (parse_error or "").strip()
    if "密码" in error_text:
        return DOCUMENT_ENCRYPTED_REPLY
    if "OCR" in error_text or "无法读取" in error_text or error_text:
        return DOCUMENT_UNREADABLE_REPLY
    return DOCUMENT_UNREADABLE_REPLY


def _has_ocr_failures(context: Any) -> bool:
    warnings = getattr(context, "parse_warnings", None) or []
    return any("OCR" in warning for warning in warnings)


def _chat_answer(response: Any) -> str:
    """Extract the assistant text from an OpenAI-compatible completion dict."""
    if not isinstance(response, dict):
        raise ValueError("document QA response must be an object")
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("document QA response has no choices")
    choice = choices[0]
    message = choice.get("message") if isinstance(choice, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise ValueError("document QA response has no assistant text")
    return content.strip()


def _attachment_failure_label(exc: Exception) -> str:
    """Secret-free failure label for the user-facing attachment error."""
    try:
        from core.screen_vision.provider_errors import classify_provider_exception

        classified = classify_provider_exception(exc)
        return getattr(classified, "failure_type", type(exc).__name__)
    except Exception:  # classification itself must never break the turn
        return type(exc).__name__


def extract_assistant_text(response: Any) -> str:
    """Extract one non-empty assistant reply from a chat completion."""
    if not isinstance(response, dict):
        raise ValueError("conversation response must be an object")
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("conversation response has no choices")
    choice = choices[0]
    message = choice.get("message") if isinstance(choice, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise ValueError("conversation response has no assistant text")
    return content.strip()
