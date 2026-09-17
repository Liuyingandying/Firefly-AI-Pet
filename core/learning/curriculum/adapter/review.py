"""Curriculum draft review service (Phase 2-CF3 §七).

Sits between a built ``CurriculumDraft`` and the store's activation: it
produces a ``ReviewSummary`` (data a UI can render — the adapter itself never
opens a window) and is the ONLY intended path from a draft to activation,
because activation must follow an explicit user confirmation.

    adapter builds draft  ->  review.review(draft)   ->  summary shown
    user clicks 确认创建    ->  review.save(draft)    ->  store (DRAFT)
                            ->  review.confirm(draft_id, confirmed_by="user")
                                -> CurriculumStore.confirm_draft  (validator gate)

Nothing here auto-activates: ``confirm`` requires an actor, and the store
rejects any non-user actor. Writing the draft to the store is a separate,
reviewer-driven step — the DRAFT BUILDER itself never touches the database.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.learning.curriculum.models import Curriculum, CurriculumDraft
from core.learning.curriculum.store import CurriculumStore


@dataclass(frozen=True, slots=True)
class ReviewSummary:
    """Read-only projection of a draft for the confirmation UI."""

    draft_id: str
    course_id: str
    title: str
    source_line: str
    chapters: tuple[tuple[int, str], ...]      # (position, title)
    concept_proposals: tuple[tuple[str, str, float | None], ...]  # (name, source_section, confidence)
    ready: bool
    warnings: tuple[str, ...] = ()

    @property
    def chapter_count(self) -> int:
        return len(self.chapters)

    @property
    def concept_count(self) -> int:
        return len(self.concept_proposals)

    def render(self) -> str:
        """A plain-text block a terminal/log or a later UI widget can show."""
        lines = [f"📘 {self.title}", f"来源：{self.source_line}", "", "章节："]
        lines += [f"{position} {title}" for position, title in self.chapters]
        if self.concept_proposals:
            lines += ["", "发现概念："]
            lines += [
                f"· {name}（{source_section or '未标注来源'}"
                + (f"，置信度 {confidence:.0%}" if confidence is not None else "")
                + "）"
                for name, source_section, confidence in self.concept_proposals
            ]
        if self.warnings:
            lines += ["", "提示："] + [f"· {warning}" for warning in self.warnings]
        status = "确认创建" if self.ready else "（暂无内容，无法确认）"
        lines += ["", f"按钮：{status} / 返回修改"]
        return "\n".join(lines)


class CurriculumDraftReviewService:
    """Review gate over a curriculum draft (never opens a window)."""

    def __init__(self, store: CurriculumStore) -> None:
        self._store = store

    @property
    def store(self) -> CurriculumStore:
        return self._store

    # ------------------------------------------------------------------
    # review
    # ------------------------------------------------------------------

    def review(self, draft: CurriculumDraft) -> ReviewSummary:
        """Summarize a draft for the confirmation step."""
        chapters = tuple(
            (chapter.position, chapter.title) for chapter in draft.chapters_in_order
        )
        proposals = set()
        for link in draft.concept_links:
            proposals.add(
                (
                    link.name,
                    link.proposal.source_section,
                    link.proposal.confidence,
                )
            )
        ordered = tuple(sorted(proposals, key=lambda item: (item[0], item[1] or "")))
        warnings = list(self._collect_warnings(draft))
        ready = bool(draft.chapters)
        return ReviewSummary(
            draft_id=draft.id,
            course_id=draft.course_id,
            title=draft.title,
            source_line=self._source_line(draft),
            chapters=chapters,
            concept_proposals=ordered,
            ready=ready,
            warnings=tuple(warnings),
        )

    @staticmethod
    def _source_line(draft: CurriculumDraft) -> str:
        if not draft.sources:
            return "（无来源）"
        parts = []
        for source in draft.sources:
            label = f"《{source.title}》" if source.title else ""
            if source.locator:
                label += f"（{source.locator}）"
            if source.institution:
                label += f"·{source.institution}"
            parts.append(label.strip() or f"[{source.kind}]")
        return "；".join(parts)

    @staticmethod
    def _collect_warnings(draft: CurriculumDraft) -> list[str]:
        warnings = []
        if not draft.chapters:
            warnings.append("目录为空：没有从文档中识别出章节。")
        elif not draft.concept_candidates:
            warnings.append("没有识别出任何概念建议（确认后仍可手动补充）。")
        for chapter in draft.chapters_in_order:
            if not chapter.concepts:
                warnings.append(f"章节「{chapter.title}」暂无概念建议。")
        if draft.created_by in ("pagelens", "ai"):
            warnings.append(
                f"本草稿由 {draft.created_by} 生成，确认必须来自你的显式操作。"
            )
        return warnings

    # ------------------------------------------------------------------
    # persist + confirm (explicit user action only)
    # ------------------------------------------------------------------

    def save(self, draft: CurriculumDraft) -> str:
        """Persist the draft so it survives and can be confirmed later."""
        return self._store.create_draft(draft)

    def confirm(self, draft_id: str, *, confirmed_by: str) -> Curriculum:
        """Confirm a persisted draft — the only way into an ACTIVE curriculum.

        The store runs the curriculum validator first; the actor must be an
        explicit user (AI / PageLens / empty actors are refused). Nothing here
        decides on the user's behalf.
        """
        return self._store.confirm_draft(draft_id, confirmed_by=confirmed_by)

    def confirm_reviewed(
        self, draft: CurriculumDraft, *, confirmed_by: str
    ) -> Curriculum:
        """Convenience: save + confirm in one explicit call."""
        draft_id = self.save(draft)
        return self.confirm(draft_id, confirmed_by=confirmed_by)


__all__ = [
    "CurriculumDraftReviewService",
    "ReviewSummary",
]