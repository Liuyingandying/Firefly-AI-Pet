"""Curriculum persistence (Phase 2-CF2) — CurriculumStore.

Bridges the pure Curriculum domain (:mod:`core.learning.curriculum.models` +
:mod:`core.learning.curriculum.validators`) into the existing LearningStore
SQLite database (schema v2). No new database: the v2 tables live next to the
v1 learning tables in the same file, added by the idempotent migration in
``LearningStore.initialize``.

Responsibilities and boundaries:

- Draft save / read / update / discard; confirm (validator-gated); activation
  (one ACTIVE per course, previous active -> SUPERSEDED); read projections;
  archive.
- EVERY write runs inside one ``with connection:`` transaction: on failure the
  whole operation rolls back — no half-written curricula, no orphan concepts,
  no dangling draft status.
- Concept reuse on activation: a proposal with ``matched_concept_id`` reuses
  that Concept (mastery/history intact); a new proposal reuses an existing
  course-scoped Concept by normalized name, or creates one with the same
  defaults as ``LearningStore.add_concept`` (mastery=0 / retention=low /
  state=discovered). The store never computes mastery.
- Import policy (Phase 2-CF2 §七): this module may import the learning store
  (and therefore sqlite3), but NEVER providers, LLM clients, PageLens or UI.

Deliberately NOT exported from ``core.learning.curriculum``'s ``__init__`` so
the pure domain package stays importable without the persistence dependency;
import it explicitly: ``from core.learning.curriculum.store import CurriculumStore``.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Mapping

from core.learning.curriculum.models import (
    ActivateResult,
    Chapter,
    ChapterConcept,
    ConceptPrerequisite,
    ConceptProposal,
    Curriculum,
    CurriculumDraft,
    CurriculumSource,
    CurriculumView,
    DraftStatus,
    LearningPath,
    LearningPathStep,
    build_curriculum_from_draft,
    next_version,
)
from core.learning.curriculum.validators import CurriculumValidator
from core.learning.models import SourceRef, utc_now_iso
from core.learning.store import LearningStore


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


class CurriculumStoreError(RuntimeError):
    """Base error for the curriculum store."""


class CurriculumStoreIntegrityError(CurriculumStoreError):
    """A database-level constraint failed (duplicate version, missing FK...).

    The failed transaction was rolled back — no partial state was written.
    """


class CurriculumNotFoundError(CurriculumStoreError):
    """No curriculum row with the given id."""


class DraftNotFoundError(CurriculumStoreError):
    """No draft row with the given id."""


# ---------------------------------------------------------------------------
# JSON codecs (the pure domain objects stay free of serialization)
# ---------------------------------------------------------------------------


def _source_to_dict(source: CurriculumSource) -> dict[str, Any]:
    return {
        "kind": source.kind,
        "title": source.title,
        "locator": source.locator,
        "source_version": source.source_version,
        "fingerprint": source.fingerprint,
        "institution": source.institution,
        "generated_by": source.generated_by,
        "captured_at": source.captured_at,
    }


def _source_from_dict(data: Mapping[str, Any]) -> CurriculumSource:
    return CurriculumSource(
        kind=data.get("kind", "manual"),
        title=data.get("title", ""),
        locator=data.get("locator"),
        source_version=data.get("source_version"),
        fingerprint=data.get("fingerprint"),
        institution=data.get("institution"),
        generated_by=data.get("generated_by"),
        captured_at=data.get("captured_at") or "",
    )


def _source_ref_to_dict(ref: SourceRef) -> dict[str, Any]:
    return ref.to_dict()


def _source_ref_from_dict(data: Mapping[str, Any]) -> SourceRef:
    return SourceRef(
        source_type=data.get("source_type", "manual"),
        document_id=data.get("document_id"),
        page=data.get("page"),
        section=data.get("section"),
        timestamp=data.get("timestamp"),
        url=data.get("url"),
        quote=data.get("quote"),
    )


def _proposal_to_dict(proposal: ConceptProposal) -> dict[str, Any]:
    return {
        "proposal_id": proposal.proposal_id,
        "name": proposal.name,
        "aliases": list(proposal.aliases),
        "source_refs": [_source_ref_to_dict(ref) for ref in proposal.source_refs],
        "matched_concept_id": proposal.matched_concept_id,
        "source_section": proposal.source_section,
        "confidence": proposal.confidence,
    }


def _proposal_from_dict(data: Mapping[str, Any]) -> ConceptProposal:
    return ConceptProposal(
        proposal_id=data["proposal_id"],
        name=data.get("name", ""),
        aliases=tuple(data.get("aliases") or ()),
        source_refs=tuple(
            _source_ref_from_dict(ref) for ref in (data.get("source_refs") or ())
        ),
        matched_concept_id=data.get("matched_concept_id"),
        source_section=data.get("source_section"),
        confidence=data.get("confidence"),
    )


def _placement_to_dict(placement) -> dict[str, Any]:
    return {
        "proposal": _proposal_to_dict(placement.proposal),
        "position": placement.position,
        "role": placement.role,
        "required": placement.required,
        "difficulty_hint": placement.difficulty_hint,
    }


def _placement_from_dict(data: Mapping[str, Any]):
    from core.learning.curriculum.models import ChapterConceptDraft

    return ChapterConceptDraft(
        proposal=_proposal_from_dict(data["proposal"]),
        position=data.get("position", 1),
        role=data.get("role", "primary"),
        required=bool(data.get("required", True)),
        difficulty_hint=data.get("difficulty_hint"),
    )


def _step_to_dict(step) -> dict[str, Any]:
    return {
        "id": step.id,
        "position": step.position,
        "target_type": step.target_type,
        "target_id": step.target_id,
        "stage": step.stage,
        "required": step.required,
    }


def _step_from_dict(data: Mapping[str, Any]):
    from core.learning.curriculum.models import PathStepDraft

    return PathStepDraft(
        id=data["id"],
        position=data.get("position", 1),
        target_type=data.get("target_type", "chapter"),
        target_id=data["target_id"],
        stage=data.get("stage"),
        required=bool(data.get("required", True)),
    )


def _jloads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _jdumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _new_id() -> str:
    return uuid.uuid4().hex


def _as_bool(value: Any) -> bool:
    return bool(value)


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------


class CurriculumStore:
    """Transactional Curriculum persistence over the LearningStore database."""

    def __init__(
        self,
        store: LearningStore | None = None,
        validator: CurriculumValidator | None = None,
    ) -> None:
        self._store = store if store is not None else LearningStore()
        self._store.initialize()  # idempotent; guarantees the v2 schema exists
        self._validator = validator if validator is not None else CurriculumValidator()

    # ------------------------------------------------------------------
    # drafts
    # ------------------------------------------------------------------

    def create_draft(self, draft: CurriculumDraft) -> str:
        """Persist a new draft. Raises when the id already exists."""
        self._insert_draft(draft, replace=False)
        return draft.id

    def save_draft(self, draft: CurriculumDraft) -> str:
        """Insert the draft, replacing any existing row with the same id."""
        self._insert_draft(draft, replace=True)
        return draft.id

    def update_draft(self, draft: CurriculumDraft) -> None:
        """Overwrite an existing draft in place (id is the identity)."""
        self._insert_draft(draft, replace=True)

    def get_draft(self, draft_id: str) -> CurriculumDraft | None:
        with self._store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM curriculum_drafts WHERE id=?", (draft_id,)
            ).fetchone()
            if row is None:
                return None
            return self._draft_from_rows(connection, row)

    def list_drafts(self, course_id: str | None = None) -> list[CurriculumDraft]:
        with self._store.connect() as connection:
            sql = "SELECT * FROM curriculum_drafts"
            params: tuple = ()
            if course_id is not None:
                sql += " WHERE course_id=?"
                params = (course_id,)
            sql += " ORDER BY created_at"
            rows = connection.execute(sql, params).fetchall()
            return [self._draft_from_rows(connection, row) for row in rows]

    def discard_draft(self, draft_id: str) -> None:
        """Discard a draft (task-spec DISCARDED -> the frozen ``rejected``
        state; it stays in the store for audit, never consumable)."""
        with self._store.connect() as connection:
            updated = connection.execute(
                "UPDATE curriculum_drafts SET status=?, updated_at=? WHERE id=?",
                (DraftStatus.REJECTED.value, utc_now_iso(), draft_id),
            )
            if updated.rowcount == 0:
                raise DraftNotFoundError(f"draft not found: {draft_id}")

    # ------------------------------------------------------------------
    # confirm / activate
    # ------------------------------------------------------------------

    def confirm_draft(
        self,
        draft_id: str,
        *,
        confirmed_by: str,
        curriculum_id: str | None = None,
    ) -> Curriculum:
        """Validate, confirm and publish a draft — one atomic transaction.

        Pipeline (order is enforced):

            1. load the draft;
            2. validate it with the curriculum validator, injecting the live
               course set and concept->course map so isolation is PROVEN, not
               assumed — a failing draft raises and writes nothing;
            3. resolve proposals to concept ids (reuse matched concepts; reuse
               course-scoped concepts by normalized name; else create new ones
               with mastery=0) INSIDE the transaction;
            4. record the user confirmation event, assign the next version;
            5. persist the new ACTIVE curriculum + structure, flip the previous
               ACTIVE to SUPERSEDED, mark the draft confirmed — all or nothing.
        """
        draft = self.get_draft(draft_id)
        if draft is None:
            raise DraftNotFoundError(f"draft not found: {draft_id}")

        validator = self._validator.with_context(**self._live_context())
        validator.validate_draft(draft).raise_if_invalid(f"draft {draft_id}")

        with self._store.connect() as connection:
            confirmed = draft.confirm(confirmed_by=confirmed_by)
            resolved = self._resolve_proposals(connection, confirmed)
            existing = self._curricula_in(connection, confirmed.course_id)
            previous_active = self._active_in(connection, confirmed.course_id)
            version = next_version(existing, confirmed.course_id)
            result = build_curriculum_from_draft(
                confirmed,
                curriculum_id=curriculum_id or _new_id(),
                version=version,
                concept_ids=resolved,
                previous_active=previous_active,
            )
            # The live context snapshot predates the concepts just created;
            # merge them (they belong to this course by construction) so the
            # built version's isolation can be proven.
            augmented = validator.with_context(
                course_of_concept={
                    concept_id: confirmed.course_id for concept_id in resolved.values()
                }
            )
            augmented.validate_curriculum(result.view()).raise_if_invalid(
                f"curriculum {result.curriculum.id}"
            )
            self._persist_activation(connection, result, confirmed_draft=confirmed)
        return result.curriculum

    def activate_curriculum(self, result: ActivateResult) -> Curriculum:
        """Persist a prebuilt activation result atomically.

        The result must reference concepts that already exist in the database
        (foreign keys enforce this) — use :meth:`confirm_draft` when new
        concepts may need to be created. The previous ACTIVE of the course is
        flipped to SUPERSEDED in the same transaction.
        """
        curriculum = result.curriculum
        if curriculum.course_id not in self._course_ids():
            raise CurriculumStoreError(
                f"course {curriculum.course_id} does not exist"
            )
        with self._store.connect() as connection:
            self._persist_activation(connection, result, confirmed_draft=None)
        return curriculum

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    def get_curriculum(self, curriculum_id: str) -> CurriculumView | None:
        with self._store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM curriculums WHERE id=?", (curriculum_id,)
            ).fetchone()
            if row is None:
                return None
            return self._view_from_rows(connection, row)

    def get_active_curriculum(self, course_id: str) -> CurriculumView | None:
        """Read-only ACTIVE view for a course; None when the course has none
        (a legal state)."""
        with self._store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM curriculums WHERE course_id=? AND status='active'"
                " ORDER BY version DESC LIMIT 1",
                (course_id,),
            ).fetchone()
            if row is None:
                return None
            return self._view_from_rows(connection, row)

    def list_curriculums(self, course_id: str) -> list[Curriculum]:
        """Every version of a course's curricula, oldest first."""
        with self._store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM curriculums WHERE course_id=? ORDER BY version",
                (course_id,),
            ).fetchall()
            return [self._curriculum_from_row(row) for row in rows]

    def archive_curriculum(self, curriculum_id: str) -> None:
        """Archive a curriculum (any status; the row is kept, not deleted)."""
        with self._store.connect() as connection:
            updated = connection.execute(
                "UPDATE curriculums SET status='archived' WHERE id=?",
                (curriculum_id,),
            )
            if updated.rowcount == 0:
                raise CurriculumNotFoundError(f"curriculum not found: {curriculum_id}")

    # ------------------------------------------------------------------
    # internal helpers — persistence
    # ------------------------------------------------------------------

    def _live_context(self) -> dict[str, Any]:
        """course_of_concept + known_course_ids taken from the live database."""
        with self._store.connect() as connection:
            courses = {
                row["id"] for row in connection.execute("SELECT id FROM courses").fetchall()
            }
            concept_owner = {
                row["id"]: row["course_id"]
                for row in connection.execute(
                    "SELECT id, course_id FROM concepts"
                ).fetchall()
            }
        return {
            "course_of_concept": concept_owner,
            "known_course_ids": courses,
        }

    def _course_ids(self) -> set[str]:
        with self._store.connect() as connection:
            return {
                row["id"] for row in connection.execute("SELECT id FROM courses").fetchall()
            }

    def _curricula_in(self, connection: sqlite3.Connection, course_id: str) -> list[Curriculum]:
        rows = connection.execute(
            "SELECT * FROM curriculums WHERE course_id=? ORDER BY version",
            (course_id,),
        ).fetchall()
        return [self._curriculum_from_row(row) for row in rows]

    def _active_in(self, connection: sqlite3.Connection, course_id: str) -> Curriculum | None:
        row = connection.execute(
            "SELECT * FROM curriculums WHERE course_id=? AND status='active'"
            " ORDER BY version DESC LIMIT 1",
            (course_id,),
        ).fetchone()
        return self._curriculum_from_row(row) if row is not None else None

    def _resolve_proposals(
        self, connection: sqlite3.Connection, draft: CurriculumDraft
    ) -> dict[str, str]:
        """proposal_id -> concept_id, creating NEW concepts inside the
        transaction (so a failed activation never leaves orphan concepts).

        Reuse order: explicit ``matched_concept_id`` first, then an existing
        course-scoped concept with the same normalized name. New rows use the
        exact same defaults as ``LearningStore.add_concept`` (mastery=0,
        retention=low, state=discovered) — this is row creation, not mastery
        logic.
        """
        now = utc_now_iso()
        resolved: dict[str, str] = {}
        for link in draft.concept_links:
            proposal = link.proposal
            if proposal.proposal_id in resolved:
                continue
            if proposal.matched_concept_id:
                resolved[proposal.proposal_id] = proposal.matched_concept_id
                continue
            row = connection.execute(
                "SELECT id FROM concepts WHERE course_id=? AND normalized_name=?",
                (draft.course_id, proposal.normalized_name),
            ).fetchone()
            if row is not None:
                resolved[proposal.proposal_id] = row["id"]
                continue
            concept_id = _new_id()
            connection.execute(
                "INSERT INTO concepts (id, course_id, canonical_name, normalized_name,"
                " aliases, mastery_level, retention, state, source_refs,"
                " last_studied_at, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 0, 'low', 'discovered', ?, NULL, ?, ?)",
                (
                    concept_id,
                    draft.course_id,
                    proposal.name,
                    proposal.normalized_name,
                    _jdumps(list(proposal.aliases)),
                    _jdumps([_source_ref_to_dict(ref) for ref in proposal.source_refs]),
                    now,
                    now,
                ),
            )
            resolved[proposal.proposal_id] = concept_id
        return resolved

    def _persist_activation(
        self,
        connection: sqlite3.Connection,
        result: ActivateResult,
        *,
        confirmed_draft: CurriculumDraft | None,
    ) -> None:
        """Write one activation inside the caller's transaction.

        Order matters for foreign keys: curriculum -> chapters -> links, paths
        -> items, prerequisites; the previous ACTIVE flips to SUPERSEDED; a
        confirming draft is marked confirmed. Any IntegrityError propagates
        and rolls the whole transaction back (callers wrap it in a friendly
        error — see the public methods).
        """
        curriculum = result.curriculum
        try:
            connection.execute(
                "INSERT INTO curriculums (id, course_id, version, title, status,"
                " source_type, description, goals, based_on_curriculum_id,"
                " default_path_id, sources, created_at, activated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    curriculum.id,
                    curriculum.course_id,
                    curriculum.version,
                    curriculum.title,
                    curriculum.status,
                    self._source_type_of(curriculum),
                    curriculum.description,
                    _jdumps(list(curriculum.goals)),
                    curriculum.based_on_curriculum_id,
                    curriculum.default_path_id,
                    _jdumps([_source_to_dict(s) for s in curriculum.sources]),
                    curriculum.created_at,
                    curriculum.activated_at,
                ),
            )
            if result.superseded is not None:
                connection.execute(
                    "UPDATE curriculums SET status='superseded' WHERE id=?",
                    (result.superseded.id,),
                )
            for chapter in result.chapters:
                connection.execute(
                    "INSERT INTO curriculum_chapters (id, curriculum_id, title,"
                    " position, description, goals, source_refs, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        chapter.id,
                        curriculum.id,
                        chapter.title,
                        chapter.position,
                        chapter.description,
                        _jdumps(list(chapter.goals)),
                        _jdumps([_source_ref_to_dict(ref) for ref in chapter.source_refs]),
                        chapter.created_at,
                        chapter.updated_at,
                    ),
                )
            for link in result.chapter_concepts:
                connection.execute(
                    "INSERT INTO chapter_concepts (id, chapter_id, concept_id,"
                    " position, role, required, difficulty_hint)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        _new_id(),
                        link.chapter_id,
                        link.concept_id,
                        link.position,
                        link.role,
                        int(link.required),
                        link.difficulty_hint,
                    ),
                )
            for path in result.paths:
                connection.execute(
                    "INSERT INTO learning_paths (id, curriculum_id, title,"
                    " description, kind) VALUES (?, ?, ?, ?, ?)",
                    (path.id, curriculum.id, path.title, path.description, path.kind),
                )
                for step in path.ordered_steps:
                    connection.execute(
                        "INSERT INTO learning_path_items (id, path_id, position,"
                        " target_type, target_id, stage, required)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            f"{curriculum.id}:{step.id}",
                            path.id,
                            step.position,
                            step.target_type,
                            step.target_id,
                            step.stage,
                            int(step.required),
                        ),
                    )
            for edge in result.prerequisites:
                connection.execute(
                    "INSERT INTO concept_prerequisites (id, curriculum_id, concept_id,"
                    " prerequisite_id, kind) VALUES (?, ?, ?, ?, ?)",
                    (
                        _new_id(),
                        curriculum.id,
                        edge.concept_id,
                        edge.prerequisite_concept_id,
                        edge.kind,
                    ),
                )
            if confirmed_draft is not None:
                connection.execute(
                    "UPDATE curriculum_drafts SET status=?, confirmed_by=?,"
                    " confirmed_at=?, updated_at=? WHERE id=?",
                    (
                        DraftStatus.CONFIRMED.value,
                        confirmed_draft.confirmed_by,
                        confirmed_draft.confirmed_at,
                        confirmed_draft.updated_at,
                        confirmed_draft.id,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise CurriculumStoreIntegrityError(
                f"activation of curriculum {curriculum.id} violated a database "
                f"constraint (rolled back): {exc}"
            ) from exc

    @staticmethod
    def _strip_draft_prefix(stored_id: str, draft_id: str) -> str:
        """Undo the ``{draft_id}:`` namespacing applied on write."""
        prefix = f"{draft_id}:"
        return stored_id[len(prefix):] if stored_id.startswith(prefix) else stored_id

    @staticmethod
    def _source_type_of(curriculum: Curriculum) -> str:
        return curriculum.sources[0].kind if curriculum.sources else "manual"

    # ------------------------------------------------------------------
    # internal helpers — drafts
    # ------------------------------------------------------------------

    def _insert_draft(self, draft: CurriculumDraft, *, replace: bool) -> None:
        with self._store.connect() as connection:
            try:
                if replace:
                    connection.execute(
                        "INSERT OR REPLACE INTO curriculum_drafts (id, course_id,"
                        " title, description, goals, based_on_curriculum_id, status,"
                        " source_type, provenance, created_by, created_at, updated_at,"
                        " confirmed_by, confirmed_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        self._draft_row(draft),
                    )
                else:
                    connection.execute(
                        "INSERT INTO curriculum_drafts (id, course_id, title,"
                        " description, goals, based_on_curriculum_id, status,"
                        " source_type, provenance, created_by, created_at, updated_at,"
                        " confirmed_by, confirmed_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        self._draft_row(draft),
                    )
                connection.execute(
                    "DELETE FROM draft_chapters WHERE draft_id=?", (draft.id,)
                )
                connection.execute(
                    "DELETE FROM draft_paths WHERE draft_id=?", (draft.id,)
                )
                connection.execute(
                    "DELETE FROM draft_prerequisites WHERE draft_id=?", (draft.id,)
                )
                # Sub-entity ids are namespaced by the draft id so hand-authored
                # drafts (CF3, tests) may reuse short ids like "ch1" without
                # colliding across drafts; reads strip the prefix back.
                for chapter in draft.chapters:
                    connection.execute(
                        "INSERT INTO draft_chapters (id, draft_id, title, position,"
                        " description, goals, source_refs, concepts)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            f"{draft.id}:{chapter.id}",
                            draft.id,
                            chapter.title,
                            chapter.position,
                            chapter.description,
                            _jdumps(list(chapter.goals)),
                            _jdumps([_source_ref_to_dict(ref) for ref in chapter.source_refs]),
                            _jdumps([_placement_to_dict(p) for p in chapter.ordered_concepts]),
                        ),
                    )
                for path in draft.paths:
                    connection.execute(
                        "INSERT INTO draft_paths (id, draft_id, title, description,"
                        " kind, steps) VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            f"{draft.id}:{path.id}",
                            draft.id,
                            path.title,
                            path.description,
                            path.kind,
                            _jdumps([_step_to_dict(step) for step in path.steps]),
                        ),
                    )
                for edge in draft.prerequisites:
                    connection.execute(
                        "INSERT INTO draft_prerequisites (id, draft_id,"
                        " concept_proposal_id, prerequisite_proposal_id, kind)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (
                            _new_id(),
                            draft.id,
                            edge.concept_proposal_id,
                            edge.prerequisite_proposal_id,
                            edge.kind,
                        ),
                    )
            except sqlite3.IntegrityError as exc:
                raise CurriculumStoreIntegrityError(
                    f"draft {draft.id} violates a database constraint: {exc}"
                ) from exc

    def _draft_row(self, draft: CurriculumDraft) -> tuple:
        return (
            draft.id,
            draft.course_id,
            draft.title,
            draft.description,
            _jdumps(list(draft.goals)),
            draft.based_on_curriculum_id,
            draft.status,
            draft.source_type or "manual",
            _jdumps([_source_to_dict(s) for s in draft.sources]),
            draft.created_by,
            draft.created_at,
            draft.updated_at,
            draft.confirmed_by,
            draft.confirmed_at,
        )

    def _draft_from_rows(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> CurriculumDraft:
        from core.learning.curriculum.models import (
            ChapterDraft,
            LearningPathDraft,
            PrerequisiteDraft,
        )

        draft_id = row["id"]
        chapters: list[ChapterDraft] = []
        for chapter_row in connection.execute(
            "SELECT * FROM draft_chapters WHERE draft_id=? ORDER BY position, id",
            (draft_id,),
        ).fetchall():
            chapters.append(
                ChapterDraft(
                    id=self._strip_draft_prefix(chapter_row["id"], draft_id),
                    title=chapter_row["title"],
                    position=chapter_row["position"],
                    description=chapter_row["description"],
                    goals=tuple(_jloads(chapter_row["goals"], [])),
                    source_refs=tuple(
                        _source_ref_from_dict(ref)
                        for ref in _jloads(chapter_row["source_refs"], [])
                    ),
                    concepts=tuple(
                        _placement_from_dict(p)
                        for p in _jloads(chapter_row["concepts"], [])
                    ),
                )
            )
        paths: list[LearningPathDraft] = []
        for path_row in connection.execute(
            "SELECT * FROM draft_paths WHERE draft_id=? ORDER BY id",
            (draft_id,),
        ).fetchall():
            paths.append(
                LearningPathDraft(
                    id=self._strip_draft_prefix(path_row["id"], draft_id),
                    title=path_row["title"],
                    description=path_row["description"],
                    kind=path_row["kind"],
                    steps=tuple(
                        _step_from_dict(step)
                        for step in _jloads(path_row["steps"], [])
                    ),
                )
            )
        prerequisites = tuple(
            PrerequisiteDraft(
                concept_proposal_id=edge["concept_proposal_id"],
                prerequisite_proposal_id=edge["prerequisite_proposal_id"],
                kind=edge["kind"],
            )
            for edge in connection.execute(
                "SELECT * FROM draft_prerequisites WHERE draft_id=?", (draft_id,)
            ).fetchall()
        )
        return CurriculumDraft(
            id=row["id"],
            course_id=row["course_id"],
            title=row["title"],
            description=row["description"],
            goals=tuple(_jloads(row["goals"], [])),
            based_on_curriculum_id=row["based_on_curriculum_id"],
            chapters=tuple(chapters),
            paths=tuple(paths),
            prerequisites=prerequisites,
            sources=tuple(
                _source_from_dict(s) for s in _jloads(row["provenance"], [])
            ),
            status=row["status"],
            created_by=row["created_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            confirmed_by=row["confirmed_by"],
            confirmed_at=row["confirmed_at"],
        )

    # ------------------------------------------------------------------
    # internal helpers — curricula
    # ------------------------------------------------------------------

    def _curriculum_from_row(self, row: sqlite3.Row) -> Curriculum:
        return Curriculum(
            id=row["id"],
            course_id=row["course_id"],
            title=row["title"],
            version=row["version"],
            status=row["status"],
            description=row["description"],
            goals=tuple(_jloads(row["goals"], [])),
            based_on_curriculum_id=row["based_on_curriculum_id"],
            default_path_id=row["default_path_id"],
            sources=tuple(
                _source_from_dict(s) for s in _jloads(row["sources"], [])
            ),
            created_at=row["created_at"],
            activated_at=row["activated_at"],
        )

    def _view_from_rows(
        self, connection: sqlite3.Connection, curriculum_row: sqlite3.Row
    ) -> CurriculumView:
        curriculum = self._curriculum_from_row(curriculum_row)
        curriculum_id = curriculum.id
        chapters = [
            Chapter(
                id=chapter_row["id"],
                curriculum_id=curriculum_id,
                title=chapter_row["title"],
                position=chapter_row["position"],
                description=chapter_row["description"],
                goals=tuple(_jloads(chapter_row["goals"], [])),
                source_refs=tuple(
                    _source_ref_from_dict(ref)
                    for ref in _jloads(chapter_row["source_refs"], [])
                ),
                created_at=chapter_row["created_at"],
                updated_at=chapter_row["updated_at"],
            )
            for chapter_row in connection.execute(
                "SELECT * FROM curriculum_chapters WHERE curriculum_id=?"
                " ORDER BY position, id",
                (curriculum_id,),
            ).fetchall()
        ]
        chapter_ids = [chapter.id for chapter in chapters]
        chapter_concepts: list[ChapterConcept] = []
        if chapter_ids:
            placeholders = ",".join("?" for _ in chapter_ids)
            for link_row in connection.execute(
                f"SELECT * FROM chapter_concepts WHERE chapter_id IN ({placeholders})"
                " ORDER BY position, concept_id",
                tuple(chapter_ids),
            ).fetchall():
                chapter_concepts.append(
                    ChapterConcept(
                        chapter_id=link_row["chapter_id"],
                        concept_id=link_row["concept_id"],
                        position=link_row["position"],
                        role=link_row["role"],
                        required=_as_bool(link_row["required"]),
                        difficulty_hint=link_row["difficulty_hint"],
                    )
                )
        paths: list[LearningPath] = []
        for path_row in connection.execute(
            "SELECT * FROM learning_paths WHERE curriculum_id=? ORDER BY id",
            (curriculum_id,),
        ).fetchall():
            steps = [
                LearningPathStep(
                    id=self._strip_draft_prefix(step_row["id"], curriculum_id),
                    path_id=path_row["id"],
                    position=step_row["position"],
                    target_type=step_row["target_type"],
                    target_id=step_row["target_id"],
                    stage=step_row["stage"],
                    required=_as_bool(step_row["required"]),
                )
                for step_row in connection.execute(
                    "SELECT * FROM learning_path_items WHERE path_id=?"
                    " ORDER BY position, id",
                    (path_row["id"],),
                ).fetchall()
            ]
            paths.append(
                LearningPath(
                    id=path_row["id"],
                    curriculum_id=curriculum_id,
                    title=path_row["title"],
                    description=path_row["description"],
                    kind=path_row["kind"],
                    steps=tuple(steps),
                )
            )
        prerequisites = [
            ConceptPrerequisite(
                curriculum_id=curriculum_id,
                concept_id=edge_row["concept_id"],
                prerequisite_concept_id=edge_row["prerequisite_id"],
                kind=edge_row["kind"],
            )
            for edge_row in connection.execute(
                "SELECT * FROM concept_prerequisites WHERE curriculum_id=?",
                (curriculum_id,),
            ).fetchall()
        ]
        return CurriculumView.of(
            curriculum,
            chapters=chapters,
            learning_paths=paths,
            chapter_concepts=chapter_concepts,
            prerequisites=prerequisites,
        )


__all__ = [
    "CurriculumStore",
    "CurriculumStoreError",
    "CurriculumStoreIntegrityError",
    "CurriculumNotFoundError",
    "DraftNotFoundError",
]
