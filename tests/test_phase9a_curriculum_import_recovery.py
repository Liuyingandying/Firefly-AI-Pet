"""Phase 9A: concept-free textbook imports recover through explicit consent."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from core.learning.controller import LearningModeController
from core.learning.curriculum.store import CurriculumStore
from core.learning.store import LearningStore
from ui.v2.course_picker import (
    TextbookDraftReviewCard,
    TextbookDraftReviewState,
)


class _Runner:
    agent_event = None
    session_video = None
    video_study = None
    _screen_vision_settings = None
    learning_controller = None

    def ask(self, text: str) -> bool:
        return True


class _Settings:
    def __init__(self) -> None:
        self.learning_last_course_id = ""

    def set_learning_last_course_id(self, course_id) -> None:
        self.learning_last_course_id = course_id or ""


@dataclass
class _FakePdfQa:
    result: object
    calls: list[tuple[str, bool]]

    def extract_concepts(self, path: str, *, consent: bool = False):
        self.calls.append((path, consent))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


@pytest.fixture()
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    return app


def _make_pdf(path: Path) -> Path:
    import fitz

    doc = fitz.open()
    for title in ("第一章 绪论", "第二章 数学模型", "第三章 时域分析"):
        page = doc.new_page()
        page.insert_text((72, 72), title)
    doc.set_toc(
        [
            [1, "第一章 绪论", 1],
            [1, "第二章 数学模型", 2],
            [1, "第三章 时域分析", 3],
        ]
    )
    doc.save(str(path))
    doc.close()
    return path


def _console(qapp, tmp_path):
    from ui.v2.console import CompanionConsole

    store = LearningStore(tmp_path / "learning.sqlite3")
    store.initialize()
    curricula = CurriculumStore(store)
    controller = LearningModeController(
        store=store,
        settings=_Settings(),
        curriculum_store=curricula,
    )
    console = CompanionConsole(_Runner())
    console.learning = controller
    return console, store, curricula


def _prepare_import(console, tmp_path):
    pdf = _make_pdf(tmp_path / "自动控制原理.pdf")
    draft, service = console._build_textbook_draft(str(pdf))
    service.save(draft)
    console._textbook_import_path = str(pdf)
    console._textbook_import_paths[draft.id] = str(pdf)
    card = console._show_textbook_review_card(service.review(draft))
    return pdf, draft, service, card


def _audit_count(store: LearningStore) -> int:
    with store.connect() as connection:
        return connection.execute("SELECT COUNT(*) FROM learning_audit").fetchone()[0]


def test_real_import_entry_creates_draft_and_shows_recovery_without_error(
    qapp, tmp_path, monkeypatch
) -> None:
    from PySide6.QtWidgets import QFileDialog

    console, store, curricula = _console(qapp, tmp_path)
    pdf = _make_pdf(tmp_path / "自动控制原理.pdf")
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        lambda *args, **kwargs: (str(pdf), "PDF (*.pdf)"),
    )

    console._on_import_textbook()

    drafts = curricula.list_drafts()
    assert len(drafts) == 1
    draft = drafts[0]
    assert draft.chapters and draft.concept_candidates == ()
    assert console._textbook_review_cards[draft.id].state is TextbookDraftReviewState.NEED_CONCEPT_GENERATION
    assert store.list_concepts(draft.course_id) == []
    assert curricula.get_active_curriculum(draft.course_id) is None
    console.close()


def test_concept_free_import_enters_recovery_and_confirm_never_reaches_validator(
    qapp, tmp_path, monkeypatch
) -> None:
    console, store, curricula = _console(qapp, tmp_path)
    _pdf, draft, service, card = _prepare_import(console, tmp_path)

    assert service.store.get_draft(draft.id) is not None
    assert card.state is TextbookDraftReviewState.NEED_CONCEPT_GENERATION
    assert "当前教材已有章节结构，但还没有知识点" in card._state_label.text()
    assert card.confirm_btn.isHidden()
    assert card.generate_btn.text() == "生成知识点"
    assert card.edit_btn.text() == "仅保存草稿"

    called = 0

    def forbidden_confirm(*args, **kwargs):
        nonlocal called
        called += 1
        raise AssertionError("concept-free Draft reached confirm_draft")

    monkeypatch.setattr(curricula, "confirm_draft", forbidden_confirm)
    console._on_textbook_confirm(draft.id)
    assert called == 0
    assert curricula.get_active_curriculum(draft.course_id) is None
    assert store.list_concepts(draft.course_id) == []
    console.close()


def test_no_consent_never_calls_pdfqa_and_keeps_the_draft(
    qapp, tmp_path
) -> None:
    console, store, curricula = _console(qapp, tmp_path)
    _pdf, draft, service, _card = _prepare_import(console, tmp_path)
    calls: list[tuple[str, bool]] = []
    console._textbook_pdf_qa_factory = lambda: _FakePdfQa(["不应出现"], calls)
    console._confirm_textbook_concept_generation_consent = lambda: False

    console._on_textbook_generate(draft.id)

    assert calls == []
    assert service.store.get_draft(draft.id) == draft
    assert curricula.get_active_curriculum(draft.course_id) is None
    assert store.list_concepts(draft.course_id) == []
    console.close()


def test_authorized_generation_updates_same_draft_then_confirms_and_builds_context(
    qapp, tmp_path
) -> None:
    console, store, curricula = _console(qapp, tmp_path)
    pdf, draft, service, _card = _prepare_import(console, tmp_path)
    original_chapters = draft.chapters
    original_sources = draft.sources
    calls: list[tuple[str, bool]] = []
    console._textbook_pdf_qa_factory = lambda: _FakePdfQa(
        ["传递函数", "状态空间", "拉普拉斯变换"], calls
    )
    console._confirm_textbook_concept_generation_consent = lambda: True

    console._on_textbook_generate(draft.id)

    generated = service.store.get_draft(draft.id)
    assert calls == [(str(pdf), True)]
    assert generated.id == draft.id
    assert generated.course_id == draft.course_id
    assert generated.sources == original_sources
    assert generated.chapters[: len(original_chapters)] == original_chapters
    assert {proposal.name for proposal in generated.concept_candidates} == {
        "传递函数", "状态空间", "拉普拉斯变换"
    }
    assert generated.paths[-1].steps[-1].target_id == generated.chapters[-1].id
    assert console._textbook_review_cards[draft.id].state is TextbookDraftReviewState.GENERATED_READY

    # Generation only edits the Draft: no learning facts exist yet.
    assert store.list_concepts(draft.course_id) == []
    assert store.list_assessments() == []
    assert store.list_review_items() == []
    assert _audit_count(store) == 0
    assert curricula.get_active_curriculum(draft.course_id) is None

    console._on_textbook_confirm(draft.id)
    active = curricula.get_active_curriculum(draft.course_id)
    assert active is not None and active.curriculum.is_active
    concepts = store.list_concepts(draft.course_id)
    assert len(concepts) == 3
    assert all(concept.mastery_level == 0 for concept in concepts)

    console.learning.enter_mode()
    console.learning.continue_last_course()
    context = console.learning.learning_context()
    assert context is not None and context.has_curriculum
    assert context.curriculum_id == active.curriculum.id
    console.close()


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (RuntimeError("provider down"), "知识点生成失败，可以稍后重试。"),
        ([], "未发现可用知识点。"),
    ],
)
def test_failed_or_empty_generation_keeps_draft_in_retryable_state(
    qapp, tmp_path, result, message
) -> None:
    console, store, curricula = _console(qapp, tmp_path)
    _pdf, draft, service, _card = _prepare_import(console, tmp_path)
    calls: list[tuple[str, bool]] = []
    console._textbook_pdf_qa_factory = lambda: _FakePdfQa(result, calls)
    console._confirm_textbook_concept_generation_consent = lambda: True

    console._on_textbook_generate(draft.id)

    kept = service.store.get_draft(draft.id)
    card = console._textbook_review_cards[draft.id]
    assert len(calls) == 1 and calls[0][1] is True
    assert kept == draft
    assert card.state is TextbookDraftReviewState.GENERATION_FAILED
    assert message in card._state_label.text()
    assert not card.generate_btn.isHidden()
    assert curricula.get_active_curriculum(draft.course_id) is None
    assert store.list_concepts(draft.course_id) == []
    assert store.list_assessments() == []
    assert store.list_review_items() == []
    assert _audit_count(store) == 0
    console.close()


def test_save_only_keeps_draft_without_activation(qapp, tmp_path) -> None:
    console, store, curricula = _console(qapp, tmp_path)
    _pdf, draft, service, card = _prepare_import(console, tmp_path)

    card._edit()

    assert service.store.get_draft(draft.id) == draft
    assert not card.isHidden()
    assert curricula.get_active_curriculum(draft.course_id) is None
    assert store.list_concepts(draft.course_id) == []
    console.close()
