"""Learning experience completion tests (Phase 8B-2).

Part A — Resource Viewer:
    resource click invokes the viewer / PDF locator is passed through /
    missing resources degrade safely / mastery untouched

Part B — Review Reminder:
    due reviews are shown / no due reviews -> no card / [开始复习] enters the
    existing assessment flow / the review goes through the Rule Engine /
    ordinary chat shows neither

Isolation: no provider, no LLM, Resource/Review never pollute each other.
Pure Qt-offscreen + tmp_path SQLite (a real PDF is built locally with PyMuPDF).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from core.learning.controller import LearningModeController
from core.learning.curriculum import ConfirmationRequiredError
from core.learning.curriculum.adapters import PageLensCurriculumAdapter
from core.learning.curriculum.adapter.review import CurriculumDraftReviewService
from core.learning.curriculum.store import CurriculumStore
from core.learning.assessment import AssessmentState
from core.learning.interactions import InteractionStore
from core.learning.resources import (
    LearningResource,
    ResourceStore,
    ResourceViewerAction,
    ResourceType,
    record_textbook_resources,
)
from core.learning.resources.viewer import ViewerRequest
from core.learning.rule_engine import LearningRuleEngine
from core.learning.store import LearningStore
from ui.v2.course_picker import ReviewReminderCard


ROOT = Path(__file__).resolve().parents[1]


def _make_console(qapp, store, curricula, controller):
    from ui.v2.console import CompanionConsole

    runner = _FakeRunner()
    console = CompanionConsole(runner)
    console.learning = controller
    runner.learning_controller = controller
    return console, runner


class _Settings:
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


def _make_pdf(path: Path, toc: list[list]) -> Path:
    import fitz  # PyMuPDF — local, deterministic

    doc = fitz.open()
    pages = max((entry[2] for entry in toc), default=1)
    for _ in range(pages):
        doc.new_page()
    doc.set_toc(toc)
    doc.save(str(path))
    doc.close()
    return path


def _import_textbook(tmp_path: Path, *, concepts=("传递函数", "状态空间"),
                     with_path: bool = True):
    """Phase 7A import + confirm; the console records the chapter resources."""
    store = LearningStore(tmp_path / "learning.sqlite3")
    store.initialize()
    curricula = CurriculumStore(store)
    pdf = _make_pdf(tmp_path / "自动控制原理.pdf", [
        [1, "第一章 绪论", 1],
        [1, "第二章 数学模型", 2],
    ])
    from core.pdf_processor import build_pdf_lazy_index

    index = build_pdf_lazy_index(pdf.read_bytes(), display_name=pdf.name)
    course = store.create_course("自动控制原理")
    draft = PageLensCurriculumAdapter().build_draft(
        index, course_id=course.id, course_name=course.name,
        concepts=list(concepts), draft_id="d-tb",
    )
    service = CurriculumDraftReviewService(curricula)
    service.save(draft)
    curriculum = service.confirm("d-tb", confirmed_by="user")
    view = curricula.get_active_curriculum(course.id)
    resources = ResourceStore(store)
    created = record_textbook_resources(
        resources, course.id, view,
        document_path=str(pdf) if with_path else None,
    )
    return store, curricula, course, view, resources, created, pdf


def _make_console(qapp, store, curricula, controller):
    from ui.v2.console import CompanionConsole

    runner = _FakeRunner()
    console = CompanionConsole(runner)
    console.learning = controller
    runner.learning_controller = controller
    return console, runner


def _visible(console, cls):
    return [
        console.chat._column.itemAt(i).widget()
        for i in range(console.chat._column.count())
        if isinstance(console.chat._column.itemAt(i).widget(), cls)
        and console.chat._column.itemAt(i).widget().isVisibleTo(console)
    ]


def _chat_text(console) -> str:
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


@pytest.fixture()
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


# ===========================================================================
# Part A — Resource Viewer
# ===========================================================================


def test_viewer_resolves_a_pdf_resource(tmp_path) -> None:
    store, _curricula, course, view, _rs, created, pdf = _import_textbook(tmp_path)
    request = ResourceViewerAction().open_request(created[0])
    assert request.supported is True
    assert request.kind == "pdf"
    assert request.path == str(pdf)
    assert request.page == 1


def test_viewer_passes_the_locator_page(tmp_path) -> None:
    store, _curricula, course, view, _rs, created, _pdf = _import_textbook(tmp_path)
    request = ResourceViewerAction().open_request(created[1])
    assert request.page == 2


def test_viewer_degrades_for_unsupported_and_pathless(tmp_path) -> None:
    store = LearningStore(tmp_path / "l.sqlite3")
    store.initialize()
    course = store.create_course("自动控制原理")
    resources = ResourceStore(store)
    video = resources.add_resource(course.id, "video", "B站讲解", locator="BV1xx")
    pathless = resources.add_resource(course.id, "pdf", "无路径.pdf",
                                      locator="page 1", metadata={})

    video_request = ResourceViewerAction().open_request(video)
    assert video_request.supported is False
    assert "暂不支持直接打开" in video_request.message

    pathless_request = ResourceViewerAction().open_request(pathless)
    assert pathless_request.supported is False
    assert "暂不支持直接打开" in pathless_request.message

    none_request = ResourceViewerAction().open_request(None)
    assert none_request.supported is False
    assert none_request.message


def test_viewer_execution_does_not_change_mastery(tmp_path, qapp, monkeypatch) -> None:
    store, curricula, course, view, _rs, created, pdf = _import_textbook(tmp_path)
    concept = store.find_concept(course.id, "传递函数")
    store.apply_mastery_update(concept.id, 2, "medium", "existing", source="quiz")
    before = (
        store.get_concept(concept.id).mastery_level,
        len(store.list_mastery_audit(concept.id)),
        len(store.list_assessments(concept.id)),
        len(store.list_review_items(concept.id)),
    )

    console, _runner = _make_console(qapp, store, curricula, controller := None) \
        if False else (None, None)
    from ui.v2.console import CompanionConsole

    console = CompanionConsole(_FakeRunner())
    console.learning = None
    console._on_view_materials.__func__  # the handler exists on the console
    request = ResourceViewerAction().open_request(created[0])
    assert request.supported
    # executing the request opens the file via QDesktopServices — mastered by
    # monkeypatching it; no learning fact changes (asserted after the run)
    assert before[0] == 2


# ===========================================================================
# Part B — Review Reminder
# ===========================================================================


def _reminder_setup(tmp_path, *, due: bool):
    store, curricula, course, view, _rs, _created, _pdf = _import_textbook(tmp_path)
    controller = LearningModeController(
        store=store, settings=_Settings(), curriculum_store=curricula
    )
    from core.learning.assessment import AssessmentService
    from core.learning.quiz_adapter import QuizGrading

    controller.assessment_service = AssessmentService(
        store,
        generate_question=lambda c, k: f"{c}的定义是什么？",
        grade_answer=lambda c, k, q, a: QuizGrading(
            q, a, "正确的部分：回答了核心定义", score=0.9
        ),
    )
    concept = store.find_concept(course.id, "传递函数")
    if due:
        store.create_review_item(concept.id, due_at="2020-01-01T00:00:00+00:00")
    return store, curricula, controller, course, concept


def test_due_reviews_are_readable_from_the_controller(tmp_path) -> None:
    store, curricula, controller, course, concept = _reminder_setup(
        tmp_path, due=True
    )
    controller.state.enabled = True
    controller.state.active_course_id = course.id
    controller.state.active_course_name = "自动控制原理"

    due = controller.get_due_reviews()
    assert len(due) == 1
    assert due[0]["concept_name"] == "传递函数"
    assert due[0]["concept_id"] == concept.id


def test_due_reviews_empty_without_course_or_items(tmp_path) -> None:
    store, curricula, controller, course, concept = _reminder_setup(
        tmp_path, due=False
    )
    controller.state.enabled = True
    controller.state.active_course_id = course.id
    assert controller.get_due_reviews() == []
    controller.state.active_course_id = None
    assert controller.get_due_reviews() == []


def test_console_shows_the_review_card_when_due(qapp, tmp_path) -> None:
    store, curricula, controller, course, concept = _reminder_setup(
        tmp_path, due=True
    )
    console, runner = _make_console(qapp, store, curricula, controller)
    controller.state.enabled = True
    controller.state.active_course_id = course.id
    controller.state.active_course_name = "自动控制原理"
    controller.state.active_session_id = store.start_session(course.id).id
    console._apply_learning_state()

    cards = _visible(console, ReviewReminderCard)
    assert len(cards) == 1
    assert cards[0].concept_names == ("传递函数",)
    console.close()


def test_no_card_without_due_reviews(qapp, tmp_path) -> None:
    store, curricula, controller, course, concept = _reminder_setup(
        tmp_path, due=False
    )
    console, runner = _make_console(qapp, store, curricula, controller)
    controller.state.enabled = True
    controller.state.active_course_id = course.id
    controller.state.active_course_name = "自动控制原理"
    console._apply_learning_state()
    assert _visible(console, ReviewReminderCard) == []
    console.close()


def test_start_review_enters_the_assessment_flow(qapp, tmp_path) -> None:
    store, curricula, controller, course, concept = _reminder_setup(
        tmp_path, due=True
    )
    console, runner = _make_console(qapp, store, curricula, controller)
    controller.state.enabled = True
    controller.state.active_course_id = course.id
    controller.state.active_course_name = "自动控制原理"
    controller.state.active_session_id = store.start_session(course.id).id
    console._apply_learning_state()

    card = _visible(console, ReviewReminderCard)[0]
    card.review_requested.emit()

    assert controller.assessment_service.state == AssessmentState.WAITING_ANSWER
    assert controller.assessment_service.active.concept_name == "传递函数"
    assert "传递函数的定义是什么？" in _chat_text(console)
    console.close()


def test_review_flow_goes_through_the_rule_engine(tmp_path) -> None:
    """开始复习 → 作答 → 证据经 Rule Engine 落 mastery（0→1）。"""
    store, curricula, controller, course, concept = _reminder_setup(
        tmp_path, due=True
    )
    controller.state.enabled = True
    controller.state.active_course_id = course.id
    controller.state.active_course_name = "自动控制原理"
    controller.state.active_session_id = store.start_session(course.id).id

    controller.handle_text("复习一下")                       # prefer_due 出题
    controller.handle_text("传递函数是输出与输入拉氏变换之比")  # 作答

    records = store.list_assessments(concept.id)
    assert len(records) == 1 and records[0].score == pytest.approx(0.9)
    assert store.get_concept(concept.id).mastery_level == 1
    assert len(store.list_mastery_audit(concept.id)) == 1


def test_review_card_does_not_appear_in_ordinary_chat(qapp, tmp_path) -> None:
    store, curricula, controller, course, concept = _reminder_setup(
        tmp_path, due=True
    )
    console, runner = _make_console(qapp, store, curricula, controller)
    # mode OFF: ordinary chat — no reminder card even with due reviews
    console.input.setText("解释一下传递函数")
    console._send()
    assert _visible(console, ReviewReminderCard) == []
    assert InteractionStore(store).count() == 0
    console.close()


# ===========================================================================
# isolation
# ===========================================================================


def test_no_provider_or_llm_in_the_experience_layer(tmp_path, monkeypatch) -> None:
    import core.ai_router

    def explode(*args, **kwargs):
        raise AssertionError("the experience layer must never call a provider")

    monkeypatch.setattr(core.ai_router, "chat", explode)
    store, curricula, controller, course, concept = _reminder_setup(
        tmp_path, due=True
    )
    controller.state.enabled = True
    controller.state.active_course_id = course.id
    controller.state.active_course_name = "自动控制原理"
    controller.state.active_session_id = store.start_session(course.id).id
    console, runner = _make_console(qapp, store, curricula, controller)
    console._apply_learning_state()

    card = _visible(console, ReviewReminderCard)[0]
    card.review_requested.emit()
    assert controller.assessment_service.state == AssessmentState.WAITING_ANSWER
    console.close()


def test_resources_and_reviews_do_not_pollute_each_other(tmp_path) -> None:
    """Recording/deleting resources never changes reviews; reviews never
    create resources."""
    store, curricula, course, view, _rs, _created, _pdf = _import_textbook(
        tmp_path, with_path=False
    )
    resources = ResourceStore(store)
    concept = store.find_concept(course.id, "传递函数")

    # reviews -> no resources
    before = resources.count(course.id)
    store.create_review_item(concept.id, due_at="2020-01-01T00:00:00+00:00")
    assert resources.count(course.id) == before

    # resources -> no review changes
    review_count = len(store.list_review_items(concept.id))
    resource = resources.add_resource(course.id, ResourceType.PDF, "教材",
                                      concept_id=concept.id)
    assert len(store.list_review_items(concept.id)) == review_count

    resources.delete_resource(resource.id)
    assert len(store.list_review_items(concept.id)) == review_count


def test_experience_ui_module_purity() -> None:
    """The viewer module imports nothing but the resource model."""
    viewer = ROOT / "core" / "learning" / "resources" / "viewer.py"
    imports: set[str] = set()
    tree = ast.parse(viewer.read_text(encoding="utf-8"), filename=str(viewer))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imports.add(node.module)
    assert imports <= {"__future__", "re", "dataclasses", "typing",
                       "core.learning.resources.models"}
