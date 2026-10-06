"""Learning Entry UI tests (Phase 2-UX).

Covers the frozen entry contract:

    welcome on entering learning mode / no-history display / never auto-activates /
    only the click activates / selecting a project opens a session /
    status card comes from LearningContext / ordinary chat untouched /
    exit restores free chat / no mastery writes / no provider calls

Pure Qt-offscreen + tmp_path SQLite; no provider, no PDF viewer.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from core.learning.controller import LearningModeController
from core.learning.curriculum import (
    ChapterConceptDraft,
    ChapterDraft,
    ConceptProposal,
    CurriculumDraft,
    CurriculumSource,
    LearningPathDraft,
    PathStepDraft,
)
from core.learning.curriculum.store import CurriculumStore
from core.learning.store import LearningStore
from core.learning.ui import (
    ACTION_CHOOSE_OTHER,
    ACTION_CONTINUE,
    ACTION_CREATE,
    build_entry_card,
    build_project_cards,
    build_status_card,
)
from ui.v2.course_picker import (
    LearningEntryCardView,
    LearningProjectPicker,
)


class _FakeSettings:
    def __init__(self) -> None:
        self.learning_last_course_id = ""

    def set_learning_last_course_id(self, course_id: str) -> None:
        self.learning_last_course_id = course_id or ""


class _FakeRunner:
    def __init__(self) -> None:
        self.agent_event = None
        self.session_video = None
        self.video_study = None
        self._screen_vision_settings = None
        self.learning_controller = None
        self.asked: list[str] = []

    def ask(self, text: str, learning_result=None) -> bool:
        self.asked.append(text)
        return True


@pytest.fixture()
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _seed_store(tmp_path) -> tuple[LearningStore, CurriculumStore]:
    store = LearningStore(tmp_path / "learning.sqlite3")
    store.initialize()
    return store, CurriculumStore(store)


def _add_course(store: LearningStore, course_id: str, name: str) -> None:
    with store.connect() as connection:
        connection.execute(
            "INSERT INTO courses (id, name, status, created_at, updated_at)"
            " VALUES (?, ?, 'active', ?, ?)",
            (course_id, name, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )


def _publish_curriculum(curricula: CurriculumStore, course_id: str) -> None:
    draft = CurriculumDraft(
        id=f"draft-{course_id}",
        course_id=course_id,
        title="自动控制原理",
        sources=(CurriculumSource(kind="manual", title="手动录入"),),
        chapters=(
            ChapterDraft(
                id="ch1", title="第一章 绪论", position=1,
                concepts=(ChapterConceptDraft(
                    proposal=ConceptProposal(proposal_id="p1", name="自动控制概述"), position=1),),
            ),
            ChapterDraft(
                id="ch2", title="第二章 数学模型", position=2,
                concepts=(
                    ChapterConceptDraft(
                        proposal=ConceptProposal(proposal_id="p2", name="传递函数"), position=1),
                    ChapterConceptDraft(
                        proposal=ConceptProposal(proposal_id="p3", name="方框图"), position=2),
                ),
            ),
        ),
        paths=(LearningPathDraft(id="path1", title="默认路线", steps=(
            PathStepDraft("s1", 1, "chapter", "ch1"),
            PathStepDraft("s2", 2, "chapter", "ch2"),
        )),),
    )
    curricula.create_draft(draft)
    curricula.confirm_draft(draft.id, confirmed_by="user")


def _make_console(qapp, store, curricula, *, last_course_id: str | None = None):
    from ui.v2.console import CompanionConsole

    settings = _FakeSettings()
    if last_course_id:
        settings.learning_last_course_id = last_course_id
    controller = LearningModeController(
        store=store, settings=settings, curriculum_store=curricula
    )
    runner = _FakeRunner()
    console = CompanionConsole(runner)
    console.learning = controller
    runner.learning_controller = controller
    return console, runner, controller, settings


def _widgets(console, cls):
    return [
        console.chat._column.itemAt(i).widget()
        for i in range(console.chat._column.count())
        if isinstance(console.chat._column.itemAt(i).widget(), cls)
    ]


def _chat_text(console) -> str:
    """Plain text of every message card in the chat flow.

    Each message lives inside a wrapper widget (the card itself is a
    QTextBrowser-based ``_MessageCard``), so the text is read from descendants.
    """
    from PySide6.QtWidgets import QLabel, QTextBrowser

    parts: list[str] = []
    for index in range(console.chat._column.count()):
        widget = console.chat._column.itemAt(index).widget()
        if widget is None:
            continue
        browsers = widget.findChildren(QTextBrowser)
        if browsers:
            parts.extend(browser.toPlainText() for browser in browsers)
        else:
            parts.extend(label.text() for label in widget.findChildren(QLabel))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 1. welcome on entering learning mode
# ---------------------------------------------------------------------------


def test_entering_learning_mode_shows_welcome(qapp, tmp_path) -> None:
    store, curricula = _seed_store(tmp_path)
    _add_course(store, "c1", "自动控制原理")
    _publish_curriculum(curricula, "c1")
    console, _, controller, settings = _make_console(
        qapp, store, curricula, last_course_id="c1"
    )
    console.learning.enter_mode()
    console._apply_learning_state()
    console._show_learning_entry()

    entries = _widgets(console, LearningEntryCardView)
    assert len(entries) == 1
    card = entries[0].card
    assert "学习模式" in "".join(card.lines())
    assert card.last_course_name == "自动控制原理"
    assert "上次学习：" in card.lines()
    assert (ACTION_CONTINUE, "继续 自动控制原理") in card.actions
    assert (ACTION_CHOOSE_OTHER, "选择其他课程") in card.actions
    assert (ACTION_CREATE, "新建学习项目") in card.actions
    console.close()


def test_welcome_greeting_only_uses_a_real_owner_name(qapp, tmp_path) -> None:
    """No invented name: the greeting is name-less when settings lack one."""
    store, curricula = _seed_store(tmp_path)
    console, _, _, _ = _make_console(qapp, store, curricula)
    entry = build_entry_card(console.learning.entry_snapshot())
    assert entry.greeting == "欢迎回来。"
    assert "傅庸" not in entry.render()
    console.close()


def test_entry_card_render_has_the_frozen_layout(qapp, tmp_path) -> None:
    store, curricula = _seed_store(tmp_path)
    _add_course(store, "c1", "自动控制原理")
    console, _, _, _ = _make_console(qapp, store, curricula, last_course_id="c1")
    rendered = build_entry_card(console.learning.entry_snapshot()).render()
    assert "📘 学习模式" in rendered
    assert "继续之前的学习吗？" in rendered
    console.close()


# ---------------------------------------------------------------------------
# 2. no-history display
# ---------------------------------------------------------------------------


def test_no_courses_shows_create_first(qapp, tmp_path) -> None:
    store, curricula = _seed_store(tmp_path)
    console, _, controller, _ = _make_console(qapp, store, curricula)
    console.learning.enter_mode()
    console._apply_learning_state()
    console._show_learning_entry()

    entries = _widgets(console, LearningEntryCardView)
    assert len(entries) == 1
    card = entries[0].card
    assert card.last_course_id is None
    assert card.course_count == 0
    assert any("还没有学习项目" in line for line in card.lines())
    assert (ACTION_CREATE, "创建第一个课程") in card.actions
    assert all(action != ACTION_CONTINUE for action, _ in card.actions)
    console.close()


def test_courses_without_history_still_offer_the_picker(qapp, tmp_path) -> None:
    store, curricula = _seed_store(tmp_path)
    _add_course(store, "c1", "自动控制原理")
    console, _, _, _ = _make_console(qapp, store, curricula)
    entry = build_entry_card(console.learning.entry_snapshot())
    assert entry.last_course_id is None
    assert entry.course_count == 1
    assert (ACTION_CHOOSE_OTHER, "选择其他课程") in entry.actions
    console.close()


# ---------------------------------------------------------------------------
# 3-4. nothing activates until the user clicks
# ---------------------------------------------------------------------------


def test_entry_never_auto_activates_or_writes(qapp, tmp_path) -> None:
    store, curricula = _seed_store(tmp_path)
    _add_course(store, "c1", "自动控制原理")
    _publish_curriculum(curricula, "c1")
    console, runner, controller, _ = _make_console(
        qapp, store, curricula, last_course_id="c1"
    )
    before_sessions = len(store.list_sessions("c1"))

    console.learning.enter_mode()
    console._apply_learning_state()
    console._show_learning_entry()
    console._apply_learning_state()
    console._show_learning_entry()  # shows the welcome card only

    assert controller.state.active_course_id is None
    assert controller.state.active_session_id is None
    assert len(store.list_sessions("c1")) == before_sessions
    assert runner.asked == []
    console.close()


def test_only_the_click_activates_the_project(qapp, tmp_path) -> None:
    store, curricula = _seed_store(tmp_path)
    _add_course(store, "c1", "自动控制原理")
    _publish_curriculum(curricula, "c1")
    console, _, controller, _ = _make_console(qapp, store, curricula, last_course_id="c1")
    console.learning.enter_mode()
    console._apply_learning_state()
    console._show_learning_entry()
    assert controller.state.active_course_id is None

    _widgets(console, LearningEntryCardView)[0].continue_requested.emit()
    assert controller.state.active_course_id == "c1"
    assert controller.state.active_course_name == "自动控制原理"
    console.close()


def test_picker_enter_button_activates(qapp, tmp_path) -> None:
    store, curricula = _seed_store(tmp_path)
    _add_course(store, "c1", "自动控制原理")
    _add_course(store, "c2", "物理光学")
    _publish_curriculum(curricula, "c1")
    console, _, controller, _ = _make_console(qapp, store, curricula)
    console.learning.enter_mode()
    console._apply_learning_state()
    console._show_learning_entry()
    console._show_project_picker()

    pickers = _widgets(console, LearningProjectPicker)
    assert len(pickers) == 1
    picker = pickers[0]
    assert {card.name for card in picker.cards} == {"自动控制原理", "物理光学"}
    assert controller.state.active_course_id is None  # still nothing active

    picker.project_chosen.emit("c2")
    assert controller.state.active_course_id == "c2"
    assert controller.state.active_course_name == "物理光学"
    console.close()


def test_project_card_shows_structure_and_recent(qapp, tmp_path) -> None:
    store, curricula = _seed_store(tmp_path)
    _add_course(store, "c1", "自动控制原理")
    _publish_curriculum(curricula, "c1")
    session = store.start_session("c1")
    concept = store.find_concept("c1", "传递函数")
    store.touch_concept(session.id, concept.id, "study")

    console, _, _, _ = _make_console(qapp, store, curricula)
    cards = {card.course_id: card for card in build_project_cards(
        console.learning.project_summaries()
    )}
    card = cards["c1"]
    assert card.curriculum_title == "自动控制原理"
    assert card.curriculum_version == 1
    assert card.structure_line == "自动控制原理 v1"
    assert card.recent == "传递函数"
    assert card.status == "继续学习"
    assert card.action_label == "进入"
    console.close()


def test_picker_without_projects_shows_create_first(qapp, tmp_path) -> None:
    store, curricula = _seed_store(tmp_path)
    console, _, _, _ = _make_console(qapp, store, curricula)
    picker = LearningProjectPicker(build_project_cards(console.learning.project_summaries()))
    assert picker.cards == ()
    console.close()


# ---------------------------------------------------------------------------
# 5. selecting a project opens a session
# ---------------------------------------------------------------------------


def test_selecting_a_project_creates_a_new_session(qapp, tmp_path) -> None:
    store, curricula = _seed_store(tmp_path)
    _add_course(store, "c1", "自动控制原理")
    _publish_curriculum(curricula, "c1")
    console, _, controller, _ = _make_console(qapp, store, curricula)

    assert store.get_active_session("c1") is None
    controller.select_course("c1")
    session = store.get_active_session("c1")
    assert session is not None
    assert controller.state.active_session_id == session.id
    console.close()


def test_entry_does_not_merge_session_history(qapp, tmp_path) -> None:
    store, curricula = _seed_store(tmp_path)
    _add_course(store, "c1", "自动控制原理")
    _publish_curriculum(curricula, "c1")
    console, _, controller, _ = _make_console(qapp, store, curricula, last_course_id="c1")

    console.learning.enter_mode()
    console._apply_learning_state()
    console._show_learning_entry()
    _widgets(console, LearningEntryCardView)[0].continue_requested.emit()
    first = controller.state.active_session_id
    controller.exit_mode()

    console2, _, controller2, _ = _make_console(
        qapp, store, curricula, last_course_id="c1"
    )
    console2.learning.enter_mode()
    console2._apply_learning_state()
    console2._show_learning_entry()
    _widgets(console2, LearningEntryCardView)[0].continue_requested.emit()
    second = controller2.state.active_session_id
    assert first != second
    assert len(store.list_sessions("c1")) == 2
    console.close()
    console2.close()


# ---------------------------------------------------------------------------
# 6. the status card comes from LearningContext
# ---------------------------------------------------------------------------


def test_status_card_maps_learning_context_only(qapp, tmp_path) -> None:
    store, curricula = _seed_store(tmp_path)
    _add_course(store, "c1", "自动控制原理")
    _publish_curriculum(curricula, "c1")
    session = store.start_session("c1")
    concept = store.find_concept("c1", "传递函数")
    store.touch_concept(session.id, concept.id, "study")

    console, _, controller, _ = _make_console(qapp, store, curricula)
    controller.state.enabled = True
    controller.state.active_course_id = "c1"
    controller.state.active_course_name = "自动控制原理"
    controller.state.active_session_id = session.id
    console._apply_learning_state()

    context = controller.learning_context()
    card = build_status_card(context)
    assert card.visible is True
    assert dict(card.rows()) == {
        "章节": "第二章 数学模型",
        "重点": "传递函数",
        "下一步": "方框图",
    }
    # the widget shows exactly the context values; the action row comes from
    # the Phase 4 decision (Phase 6.5), copied verbatim like every other row
    view = console.companion.context_status.view
    assert view.chapter == context.current_chapter
    assert view.focus == context.current_focus
    assert view.next_in_order == context.next_in_order
    assert view.decision_action == controller.learning_decision().action
    from PySide6.QtWidgets import QLabel
    visible_text = "\n".join(label.text() for label in console.companion.context_status.findChildren(QLabel))
    assert "第二章 数学模型" in visible_text and "传递函数" in visible_text
    console.close()


def test_status_card_hidden_without_curriculum(qapp, tmp_path) -> None:
    store, curricula = _seed_store(tmp_path)
    _add_course(store, "c1", "无结构课程")
    console, _, controller, _ = _make_console(qapp, store, curricula)
    controller.select_course("c1")
    console._apply_learning_state()

    assert controller.learning_context().has_curriculum is False
    assert build_status_card(controller.learning_context()).visible is False
    assert console.companion.context_status.view.chapter == ""
    console.close()


def test_status_card_is_empty_without_a_running_course(qapp, tmp_path) -> None:
    store, curricula = _seed_store(tmp_path)
    console, _, controller, _ = _make_console(qapp, store, curricula)
    assert controller.learning_context() is None
    assert build_status_card(None).visible is False
    assert build_status_card(None).rows() == ()
    console.close()


def test_entry_shows_the_resume_line_after_activation(qapp, tmp_path) -> None:
    store, curricula = _seed_store(tmp_path)
    _add_course(store, "c1", "自动控制原理")
    _publish_curriculum(curricula, "c1")
    session = store.start_session("c1")
    concept = store.find_concept("c1", "传递函数")
    store.touch_concept(session.id, concept.id, "study")

    console, _, controller, _ = _make_console(qapp, store, curricula, last_course_id="c1")
    console.learning.enter_mode()
    console._apply_learning_state()
    console._show_learning_entry()
    _widgets(console, LearningEntryCardView)[0].continue_requested.emit()

    texts = _chat_text(console)
    assert "我们继续第二章 数学模型" in texts
    assert "上次学习：传递函数" in texts
    assert "下一节是：方框图" in texts
    console.close()


# ---------------------------------------------------------------------------
# 7-8. ordinary chat and exit
# ---------------------------------------------------------------------------


def test_ordinary_chat_unaffected(qapp, tmp_path) -> None:
    store, curricula = _seed_store(tmp_path)
    console, runner, controller, _ = _make_console(qapp, store, curricula)
    console.input.setText("什么是传递函数？")
    console._send()
    assert runner.asked == ["什么是传递函数？"]
    assert controller.state.enabled is False
    console.close()


def test_natural_language_entry_matches_the_button(qapp, tmp_path) -> None:
    store, curricula = _seed_store(tmp_path)
    _add_course(store, "c1", "自动控制原理")
    console, runner, controller, _ = _make_console(qapp, store, curricula)

    for text in ("开始学习", "打开学习模式"):
        console.input.setText(text)
        console._send()
        assert controller.state.enabled is True
        assert runner.asked == []
        controller.exit_mode()

    # "继续学习" in free chat still works and activates the remembered project
    console2, runner2, controller2, _ = _make_console(
        qapp, store, curricula, last_course_id="c1"
    )
    console2.input.setText("继续学习")
    console2._send()
    assert controller2.state.active_course_id == "c1"
    assert runner2.asked == []
    console.close()
    console2.close()


def test_ordinary_questions_do_not_trigger_chapter_intents(qapp, tmp_path) -> None:
    store, curricula = _seed_store(tmp_path)
    console, runner, controller, _ = _make_console(qapp, store, curricula)
    for text in ("第二章讲了什么", "下一节是什么", "开始学习Python"):
        assert controller.handle_text(text) is None
    console.close()


def test_exit_restores_free_chat(qapp, tmp_path) -> None:
    store, curricula = _seed_store(tmp_path)
    _add_course(store, "c1", "自动控制原理")
    _publish_curriculum(curricula, "c1")
    console, runner, controller, _ = _make_console(qapp, store, curricula, last_course_id="c1")
    console.learning.enter_mode()
    console._apply_learning_state()
    console._show_learning_entry()
    _widgets(console, LearningEntryCardView)[0].continue_requested.emit()
    assert controller.state.enabled is True

    console.learning.exit_mode()
    console._apply_learning_state()  # toggle off
    assert controller.state.enabled is False
    assert controller.state.active_course_id is None
    assert console.companion.context_status.view.mode == "自由对话"
    assert console.companion.context_status.view.chapter == ""

    console.input.setText("今天天气不错")
    console._send()
    assert runner.asked == ["今天天气不错"]
    console.close()


# ---------------------------------------------------------------------------
# 9-10. no mastery writes, no provider calls
# ---------------------------------------------------------------------------


def test_entry_flow_never_writes_mastery_or_assessments(qapp, tmp_path) -> None:
    store, curricula = _seed_store(tmp_path)
    _add_course(store, "c1", "自动控制原理")
    _publish_curriculum(curricula, "c1")
    session = store.start_session("c1")
    concept = store.find_concept("c1", "传递函数")
    store.touch_concept(session.id, concept.id, "study")
    store.apply_mastery_update(concept.id, 3, "medium", "existing", source="quiz")

    before = (
        store.get_concept(concept.id).mastery_level,
        len(store.list_mastery_audit(concept.id)),
        len(store.list_assessments()),
        len(store.list_review_items()),
    )
    console, _, controller, _ = _make_console(qapp, store, curricula, last_course_id="c1")
    console.learning.enter_mode()
    console._apply_learning_state()
    console._show_learning_entry()
    _widgets(console, LearningEntryCardView)[0].continue_requested.emit()
    console._show_project_picker()
    _widgets(console, LearningProjectPicker)[0].project_chosen.emit("c1")

    after = (
        store.get_concept(concept.id).mastery_level,
        len(store.list_mastery_audit(concept.id)),
        len(store.list_assessments()),
        len(store.list_review_items()),
    )
    assert before == after
    console.close()


def test_entry_flow_never_calls_a_provider(qapp, tmp_path, monkeypatch) -> None:
    import core.ai_router

    def explode(*args, **kwargs):
        raise AssertionError("the learning entry must not call a provider")

    monkeypatch.setattr(core.ai_router, "chat", explode)
    store, curricula = _seed_store(tmp_path)
    _add_course(store, "c1", "自动控制原理")
    _publish_curriculum(curricula, "c1")
    console, runner, controller, _ = _make_console(qapp, store, curricula, last_course_id="c1")
    console.learning.enter_mode()
    console._apply_learning_state()
    console._show_learning_entry()
    _widgets(console, LearningEntryCardView)[0].continue_requested.emit()
    console._apply_learning_state()
    assert runner.asked == []
    console.close()


def test_ui_module_is_widget_and_provider_free() -> None:
    """core/learning/ui is pure: no Qt, no provider, no store."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "core" / "learning" / "ui"
    forbidden = {
        "PySide6", "PyQt5", "PyQt6", "ui", "core.ai_router", "core.providers",
        "core.pagelens_bridge", "core.screen_vision", "core.memory", "openai",
        "anthropic", "requests", "httpx", "core.learning.store",
        "core.learning.curriculum.store",
    }
    offenders: dict[str, set[str]] = {}
    for path in sorted(root.glob("*.py")):
        imports: set[str] = set()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imports.add(node.module)
        bad = {
            module for module in imports
            if module.split(".")[0] in forbidden
            or any(module == f or module.startswith(f + ".") for f in forbidden)
        }
        if bad:
            offenders[path.name] = bad
    assert not offenders, f"learning ui imports forbidden modules: {offenders}"


def test_status_card_does_not_compute_anything() -> None:
    """A card with no context stays empty — the UI never guesses structure."""

    class _FakeContext:
        course_name = "自动控制原理"
        source = "active_curriculum"

    card = build_status_card(_FakeContext())
    assert card.visible is False
    assert card.rows() == ()
    assert card.course_name == "自动控制原理"
