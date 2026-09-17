"""Learning Store persistence (Phase 1A).

A local, transactional, testable SQLite store that saves *learning facts*
only — it never infers, teaches, grades, decides mastery, or calls models.

Persistence conventions:
- DB file lives under the user data dir (``%LOCALAPPDATA%/FireflyAI/learning``
  on Windows, ``~/.config/FireflyAI/learning`` elsewhere) — never inside the
  repo, never in ``runtime/``, never in the Mem0 database.
- sqlite3 stdlib only; no ORM. connection-per-operation with
  ``PRAGMA foreign_keys=ON`` and a busy timeout, so GUI and worker threads
  never share a raw connection.
- All writes run inside the connection's transaction (commit / rollback on
  exception) — no half-written states.
- Times are UTC ISO 8601 (see :mod:`core.learning.models`).

Mastery write boundary:
- ``apply_mastery_update`` is the ONLY path that changes ``concepts.mastery``.
  It takes an explicit deterministic value from the caller (future Rule
  Engine, or a user override through the same channel), validates range and
  enums, and appends an audit row. It performs no inference of its own.
- ``record_assessment`` NEVER touches mastery: inserting an assessment is
  pure evidence capture (test-locked).
- Ambient candidates are not persisted: there is no candidate write API.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Iterable

from core.learning.models import (
    AssessmentRecord,
    AssessmentSource,
    Concept,
    ConceptState,
    Course,
    CourseStatus,
    Difficulty,
    MasteryUpdate,
    Retention,
    ReviewItem,
    SourceRef,
    StudySession,
    normalize_name,
    utc_now_iso,
)
from core.user_paths import get_user_data_paths

SCHEMA_VERSION = 4

DEFAULT_DB_PATH = get_user_data_paths().learning / "learning_store.sqlite3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS learning_schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS courses (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    source_title TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS concepts (
    id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    canonical_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    aliases TEXT NOT NULL DEFAULT '[]',
    mastery_level INTEGER NOT NULL DEFAULT 0
        CHECK (mastery_level BETWEEN 0 AND 5),
    retention TEXT NOT NULL DEFAULT 'low',
    state TEXT NOT NULL DEFAULT 'discovered',
    source_refs TEXT NOT NULL DEFAULT '[]',
    last_studied_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (course_id, normalized_name)
);

CREATE TABLE IF NOT EXISTS study_sessions (
    id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    summary TEXT NOT NULL DEFAULT '',
    concept_ids TEXT NOT NULL DEFAULT '[]',
    activities TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assessment_records (
    id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    concept_id TEXT NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    session_id TEXT REFERENCES study_sessions(id) ON DELETE SET NULL,
    source TEXT NOT NULL,
    score REAL NOT NULL CHECK (score >= 0.0 AND score <= 1.0),
    confidence REAL NOT NULL DEFAULT 1.0
        CHECK (confidence >= 0.0 AND confidence <= 1.0),
    difficulty TEXT NOT NULL DEFAULT 'recall',
    ai_generated INTEGER NOT NULL DEFAULT 0,
    source_refs TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_items (
    id TEXT PRIMARY KEY,
    concept_id TEXT NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    due_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    interval_days INTEGER NOT NULL DEFAULT 1,
    consecutive_success INTEGER NOT NULL DEFAULT 0,
    retention TEXT NOT NULL DEFAULT 'low',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS learning_audit (
    id TEXT PRIMARY KEY,
    concept_id TEXT NOT NULL,
    old_mastery INTEGER NOT NULL,
    new_mastery INTEGER NOT NULL,
    retention TEXT NOT NULL,
    reason TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_concepts_course ON concepts(course_id);
CREATE INDEX IF NOT EXISTS idx_assessments_concept ON assessment_records(concept_id);
CREATE INDEX IF NOT EXISTS idx_reviews_due ON review_items(status, due_at);
CREATE INDEX IF NOT EXISTS idx_sessions_course ON study_sessions(course_id);
"""

# Phase 2-CF2: Curriculum Foundation schema (v2).
#
# New tables only — v1 tables and their column semantics are untouched. Every
# statement is idempotent so ``initialize()`` may be re-run freely (a repeated
# migration must never fail). ``learning_schema_meta.schema_version`` is raised
# from 1 to 2 atomically in the same transaction that creates these tables.
_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS curriculums (
    id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    source_type TEXT NOT NULL DEFAULT 'manual',
    description TEXT NOT NULL DEFAULT '',
    goals TEXT NOT NULL DEFAULT '[]',
    based_on_curriculum_id TEXT,
    default_path_id TEXT,
    sources TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    activated_at TEXT,
    UNIQUE (course_id, version)
);

CREATE TABLE IF NOT EXISTS curriculum_chapters (
    id TEXT PRIMARY KEY,
    curriculum_id TEXT NOT NULL REFERENCES curriculums(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    position INTEGER NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    goals TEXT NOT NULL DEFAULT '[]',
    source_refs TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (curriculum_id, position)
);

CREATE TABLE IF NOT EXISTS learning_paths (
    id TEXT PRIMARY KEY,
    curriculum_id TEXT NOT NULL REFERENCES curriculums(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'canonical'
);

CREATE TABLE IF NOT EXISTS learning_path_items (
    id TEXT PRIMARY KEY,
    path_id TEXT NOT NULL REFERENCES learning_paths(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    stage TEXT,
    required INTEGER NOT NULL DEFAULT 1,
    UNIQUE (path_id, position)
);

CREATE TABLE IF NOT EXISTS chapter_concepts (
    id TEXT PRIMARY KEY,
    chapter_id TEXT NOT NULL REFERENCES curriculum_chapters(id) ON DELETE CASCADE,
    concept_id TEXT NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    role TEXT NOT NULL DEFAULT 'primary',
    required INTEGER NOT NULL DEFAULT 1,
    difficulty_hint TEXT,
    UNIQUE (chapter_id, concept_id)
);

CREATE TABLE IF NOT EXISTS concept_prerequisites (
    id TEXT PRIMARY KEY,
    curriculum_id TEXT NOT NULL REFERENCES curriculums(id) ON DELETE CASCADE,
    concept_id TEXT NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    prerequisite_id TEXT NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    kind TEXT NOT NULL DEFAULT 'required',
    UNIQUE (curriculum_id, concept_id, prerequisite_id)
);

CREATE TABLE IF NOT EXISTS curriculum_drafts (
    id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    goals TEXT NOT NULL DEFAULT '[]',
    based_on_curriculum_id TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    source_type TEXT NOT NULL DEFAULT 'manual',
    provenance TEXT NOT NULL DEFAULT '[]',
    created_by TEXT NOT NULL DEFAULT 'user',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    confirmed_by TEXT,
    confirmed_at TEXT
);

CREATE TABLE IF NOT EXISTS draft_chapters (
    id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES curriculum_drafts(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    position INTEGER NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    goals TEXT NOT NULL DEFAULT '[]',
    source_refs TEXT NOT NULL DEFAULT '[]',
    concepts TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS draft_paths (
    id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES curriculum_drafts(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'canonical',
    steps TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS draft_prerequisites (
    id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES curriculum_drafts(id) ON DELETE CASCADE,
    concept_proposal_id TEXT NOT NULL,
    prerequisite_proposal_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'required'
);

CREATE INDEX IF NOT EXISTS idx_curriculums_course ON curriculums(course_id, status);
CREATE INDEX IF NOT EXISTS idx_curriculum_chapters_curriculum ON curriculum_chapters(curriculum_id);
CREATE INDEX IF NOT EXISTS idx_learning_paths_curriculum ON learning_paths(curriculum_id);
CREATE INDEX IF NOT EXISTS idx_chapter_concepts_chapter ON chapter_concepts(chapter_id);
CREATE INDEX IF NOT EXISTS idx_chapter_concepts_concept ON chapter_concepts(concept_id);
CREATE INDEX IF NOT EXISTS idx_prereqs_curriculum ON concept_prerequisites(curriculum_id);
CREATE INDEX IF NOT EXISTS idx_drafts_course ON curriculum_drafts(course_id, status);
CREATE INDEX IF NOT EXISTS idx_draft_chapters_draft ON draft_chapters(draft_id);
"""

# Phase 3.5: Learning Interaction tracking (v3).
#
# Append-only facts about what the learner actually discussed. New table only —
# v1/v2 tables and their column semantics are untouched. Auditable by
# construction: there is no UPDATE/DELETE API, each row carries who produced it
# (``source``) and when (``created_at``).
_SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS learning_interactions (
    id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    concept_id TEXT NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_interactions_course
    ON learning_interactions(course_id, created_at);
CREATE INDEX IF NOT EXISTS idx_interactions_concept
    ON learning_interactions(concept_id, created_at);
"""

# Phase 7B: Learning Resource layer (v4).
#
# Course-scoped references to trusted learning material. Append/reference-only
# by contract: a resource row NEVER carries mastery and its deletion can never
# cascade into Concept / mastery / Assessment / Review rows (those live on
# other tables; only the resource row itself is removed). New table only —
# v1/v2/v3 tables and their column semantics are untouched.
_SCHEMA_V4 = """
CREATE TABLE IF NOT EXISTS learning_resources (
    id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    concept_id TEXT REFERENCES concepts(id) ON DELETE CASCADE,
    chapter_id TEXT REFERENCES curriculum_chapters(id) ON DELETE CASCADE,
    resource_type TEXT NOT NULL,
    title TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    locator TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_resources_course
    ON learning_resources(course_id, created_at);
CREATE INDEX IF NOT EXISTS idx_resources_concept
    ON learning_resources(concept_id, created_at);
CREATE INDEX IF NOT EXISTS idx_resources_chapter
    ON learning_resources(chapter_id, created_at);
"""


class LearningStoreError(RuntimeError):
    """Base error for the learning store."""


class ConceptExistsError(LearningStoreError):
    """A concept with the same (course_id, normalized_name) already exists."""


def _new_id() -> str:
    return uuid.uuid4().hex


def _json_loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


class LearningStore:
    """High-level CRUD facade over SQLite. Callers never write SQL."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH

    # ------------------------------------------------------------------
    # connection / schema
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Default (deferred) isolation: every write inside
        # ``with connection:`` commits on success / rolls back on exception,
        # so multi-statement operations never leave half-written state.
        connection = sqlite3.connect(str(self.db_path), timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def connect(self) -> sqlite3.Connection:
        """Public connection factory for co-processes that must run ONE
        transaction across many tables (e.g. CurriculumStore).

        Same isolation mode, row factory and pragmas as the internal
        connections: default (deferred) isolation so ``with connection:``
        commits on success and rolls back on exception.
        """
        return self._connect()

    def initialize(self) -> None:
        """Create or migrate the schema to the latest version (v2).

        Idempotent — safe to call any number of times on any database:
        v1 tables are created if missing, then the v2 curriculum tables, the
        v3 interaction table and the v4 resource table, then
        ``schema_version`` is raised to :data:`SCHEMA_VERSION`. A v1/v2
        database is upgraded in place; no existing table is dropped and no
        column semantics change. Because the DDL is idempotent, an interruption
        between the DDL pass and the version bump self-heals on the next call.
        """
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
            connection.executescript(_SCHEMA_V2)
            connection.executescript(_SCHEMA_V3)
            connection.executescript(_SCHEMA_V4)
            row = connection.execute(
                "SELECT value FROM learning_schema_meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO learning_schema_meta (key, value) VALUES (?, ?)",
                    ("schema_version", str(SCHEMA_VERSION)),
                )
            else:
                current = int(row["value"] or 0)
                if current < SCHEMA_VERSION:
                    connection.execute(
                        "UPDATE learning_schema_meta SET value=? WHERE key='schema_version'",
                        (str(SCHEMA_VERSION),),
                    )

    def get_schema_version(self) -> int:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM learning_schema_meta WHERE key='schema_version'"
            ).fetchone()
            return int(row["value"]) if row is not None else 0

    # ------------------------------------------------------------------
    # Course
    # ------------------------------------------------------------------

    def create_course(
        self,
        name: str,
        description: str = "",
        source_title: str = "",
    ) -> Course:
        name = (name or "").strip()
        if not name:
            raise LearningStoreError("course name must not be empty")
        now = utc_now_iso()
        course_id = _new_id()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO courses (id, name, description, status, source_title,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (course_id, name, description, CourseStatus.ACTIVE.value,
                 source_title, now, now),
            )
        return Course(id=course_id, name=name, description=description,
                      status=CourseStatus.ACTIVE.value, source_title=source_title,
                      created_at=now, updated_at=now)

    def get_course(self, course_id: str) -> Course | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM courses WHERE id=?", (course_id,)
            ).fetchone()
        return self._course_from_row(row) if row is not None else None

    def list_courses(self, include_archived: bool = False) -> list[Course]:
        sql = "SELECT * FROM courses"
        params: tuple = ()
        if not include_archived:
            sql += " WHERE status=?"
            params = (CourseStatus.ACTIVE.value,)
        sql += " ORDER BY created_at"
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._course_from_row(row) for row in rows]

    def update_course(self, course_id: str, *, name: str | None = None,
                      description: str | None = None,
                      source_title: str | None = None) -> Course | None:
        course = self.get_course(course_id)
        if course is None:
            return None
        fields: list[str] = []
        params: list[Any] = []
        if name is not None:
            name = name.strip()
            if not name:
                raise LearningStoreError("course name must not be empty")
            fields.append("name=?"), params.append(name)
        if description is not None:
            fields.append("description=?"), params.append(description)
        if source_title is not None:
            fields.append("source_title=?"), params.append(source_title)
        if not fields:
            return course
        fields.append("updated_at=?"), params.append(utc_now_iso())
        params.append(course_id)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE courses SET {', '.join(fields)} WHERE id=?",
                params,
            )
        return self.get_course(course_id)

    def archive_course(self, course_id: str) -> Course | None:
        course = self.get_course(course_id)
        if course is None:
            return None
        with self._connect() as connection:
            connection.execute(
                "UPDATE courses SET status=?, updated_at=? WHERE id=?",
                (CourseStatus.ARCHIVED.value, utc_now_iso(), course_id),
            )
        return self.get_course(course_id)

    def delete_course(self, course_id: str) -> None:
        """Hard delete; concepts/sessions cascade via foreign keys, and
        assessments cascade (via concept) — all inside one transaction."""
        with self._connect() as connection:
            connection.execute("DELETE FROM courses WHERE id=?", (course_id,))

    # ------------------------------------------------------------------
    # Concept
    # ------------------------------------------------------------------

    def add_concept(
        self,
        course_id: str,
        canonical_name: str,
        aliases: Iterable[str] = (),
        source_refs: Iterable[SourceRef] = (),
        mastery_level: int = 0,
        retention: str = Retention.LOW.value,
        state: str = ConceptState.DISCOVERED.value,
        created_at: str | None = None,
    ) -> Concept:
        """Insert a concept. Raises ConceptExistsError when the
        (course_id, normalized_name) key already exists. ``created_at`` is
        an optional explicit UTC-ISO timestamp (imports / simulation);
        it defaults to the store clock."""
        self._require_course(course_id)
        self._validate_mastery(mastery_level)
        retention = self._validate_retention(retention)
        state = self._validate_state(state)
        canonical_name = (canonical_name or "").strip()
        if not canonical_name:
            raise LearningStoreError("concept name must not be empty")
        normalized = normalize_name(canonical_name)
        aliases_list = [str(a).strip() for a in aliases if str(a).strip()]
        refs = [self._as_source_ref(r) for r in source_refs]
        now = created_at or utc_now_iso()
        concept_id = _new_id()
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO concepts (id, course_id, canonical_name,"
                    " normalized_name, aliases, mastery_level, retention, state,"
                    " source_refs, last_studied_at, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (concept_id, course_id, canonical_name, normalized,
                     _json_dumps(aliases_list), mastery_level, retention, state,
                     _json_dumps([r.to_dict() for r in refs]), None, now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise ConceptExistsError(
                f"concept {canonical_name!r} already exists in course {course_id}"
            ) from exc
        return Concept(
            id=concept_id, course_id=course_id, canonical_name=canonical_name,
            normalized_name=normalized, aliases=aliases_list,
            mastery_level=mastery_level, retention=retention, state=state,
            source_refs=list(refs), last_studied_at=None, created_at=now,
            updated_at=now,
        )

    def get_or_add_concept(
        self,
        course_id: str,
        canonical_name: str,
        aliases: Iterable[str] = (),
        source_refs: Iterable[SourceRef] = (),
    ) -> tuple[Concept, bool]:
        """Course-scoped merge: return the existing concept (appending new
        aliases/source_refs) or create one. ``created`` tells which."""
        existing = self.find_concept(course_id, canonical_name)
        if existing is not None:
            merged_aliases = list(dict.fromkeys(existing.aliases + [
                str(a).strip() for a in aliases if str(a).strip()
            ]))
            merged_refs = list(existing.source_refs) + [
                self._as_source_ref(r) for r in source_refs
            ]
            if merged_aliases != existing.aliases or merged_refs != existing.source_refs:
                self._update_concept_row(
                    existing.id,
                    aliases=merged_aliases,
                    source_refs=merged_refs,
                )
                existing = self.get_concept(existing.id)
            return existing, False
        return self.add_concept(
            course_id, canonical_name, aliases=aliases, source_refs=source_refs
        ), True

    def find_concept(self, course_id: str, name: str) -> Concept | None:
        """Look up by (course_id, normalized_name) — deterministic only."""
        normalized = normalize_name(name)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM concepts WHERE course_id=? AND normalized_name=?",
                (course_id, normalized),
            ).fetchone()
        return self._concept_from_row(row) if row is not None else None

    def get_concept(self, concept_id: str) -> Concept | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM concepts WHERE id=?", (concept_id,)
            ).fetchone()
        return self._concept_from_row(row) if row is not None else None

    def list_concepts(self, course_id: str) -> list[Concept]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM concepts WHERE course_id=? ORDER BY created_at",
                (course_id,),
            ).fetchall()
        return [self._concept_from_row(row) for row in rows]

    def delete_concept(self, concept_id: str) -> None:
        """Delete one concept; assessments/reviews cascade in one transaction."""
        with self._connect() as connection:
            connection.execute("DELETE FROM concepts WHERE id=?", (concept_id,))

    # -- controlled mastery write (Rule Engine / user override channel) --

    def apply_mastery_update(
        self,
        concept_id: str,
        new_mastery: int,
        retention: str,
        reason: str,
        source: str = "rule_engine",
    ) -> MasteryUpdate:
        """The ONLY entry that changes ``concepts.mastery_level``.

        The caller supplies the explicit deterministic value; the store only
        validates (mastery 0-5, retention enum), applies it transactionally
        and records an audit row. No inference happens here.
        """
        concept = self.get_concept(concept_id)
        if concept is None:
            raise LearningStoreError(f"concept not found: {concept_id}")
        self._validate_mastery(new_mastery)
        retention = self._validate_retention(retention)
        reason = (reason or "").strip()
        if not reason:
            raise LearningStoreError("mastery update requires a reason")
        now = utc_now_iso()
        audit_id = _new_id()
        with self._connect() as connection:
            connection.execute(
                "UPDATE concepts SET mastery_level=?, retention=?, updated_at=?"
                " WHERE id=?",
                (new_mastery, retention, now, concept_id),
            )
            connection.execute(
                "INSERT INTO learning_audit (id, concept_id, old_mastery,"
                " new_mastery, retention, reason, source, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (audit_id, concept_id, concept.mastery_level, new_mastery,
                 retention, reason, source, now),
            )
        return MasteryUpdate(
            concept_id=concept_id,
            old_mastery=concept.mastery_level,
            new_mastery=new_mastery,
            retention=retention,
            reason=reason,
            source=source,
            updated_at=now,
        )

    def list_mastery_audit(self, concept_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM learning_audit WHERE concept_id=? ORDER BY created_at",
                (concept_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_concept_state(
        self,
        concept_id: str,
        *,
        state: str | None = None,
        retention: str | None = None,
    ) -> Concept | None:
        """Controlled rule-layer write for concept ``state`` and ``retention``
        ONLY — mastery is never touched here. Both values are validated
        against the frozen enums; updated_at is refreshed."""
        concept = self.get_concept(concept_id)
        if concept is None:
            return None
        fields: list[str] = []
        params: list[Any] = []
        if state is not None:
            fields.append("state=?")
            params.append(self._validate_state(state))
        if retention is not None:
            fields.append("retention=?")
            params.append(self._validate_retention(retention))
        if not fields:
            return concept
        fields.append("updated_at=?")
        params.append(utc_now_iso())
        params.append(concept_id)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE concepts SET {', '.join(fields)} WHERE id=?",
                params,
            )
        return self.get_concept(concept_id)

    # ------------------------------------------------------------------
    # StudySession
    # ------------------------------------------------------------------

    def start_session(self, course_id: str, summary: str = "") -> StudySession:
        """Start a session for the course. One active session per course:
        if one exists it is returned unchanged (idempotent)."""
        self._require_course(course_id)
        active = self.get_active_session(course_id)
        if active is not None:
            return active
        now = utc_now_iso()
        session_id = _new_id()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO study_sessions (id, course_id, started_at, ended_at,"
                " status, summary, concept_ids, activities, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, course_id, now, None, "active", summary, "[]", "[]", now),
            )
        return StudySession(id=session_id, course_id=course_id, started_at=now,
                            ended_at=None, status="active", summary=summary,
                            concept_ids=[], activities=[], created_at=now)

    def get_active_session(self, course_id: str) -> StudySession | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM study_sessions WHERE course_id=? AND status='active'"
                " ORDER BY started_at DESC LIMIT 1",
                (course_id,),
            ).fetchone()
        return self._session_from_row(row) if row is not None else None

    def end_session(self, session_id: str) -> StudySession | None:
        session = self.get_session(session_id)
        if session is None:
            return None
        now = utc_now_iso()
        with self._connect() as connection:
            connection.execute(
                "UPDATE study_sessions SET status='ended', ended_at=?,"
                " summary=? WHERE id=?",
                (now, session.summary, session_id),
            )
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> StudySession | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM study_sessions WHERE id=?", (session_id,)
            ).fetchone()
        return self._session_from_row(row) if row is not None else None

    def list_sessions(self, course_id: str) -> list[StudySession]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM study_sessions WHERE course_id=?"
                " ORDER BY started_at",
                (course_id,),
            ).fetchall()
        return [self._session_from_row(row) for row in rows]

    def touch_concept(self, session_id: str, concept_id: str,
                      activity: str) -> None:
        """Append a concept/activity to a session (no chat text ever)."""
        session = self.get_session(session_id)
        if session is None:
            raise LearningStoreError(f"session not found: {session_id}")
        concept_ids = list(session.concept_ids)
        if concept_id not in concept_ids:
            concept_ids.append(concept_id)
        activities = list(session.activities) + [activity]
        with self._connect() as connection:
            connection.execute(
                "UPDATE study_sessions SET concept_ids=?, activities=? WHERE id=?",
                (_json_dumps(concept_ids), _json_dumps(activities), session_id),
            )
            connection.execute(
                "UPDATE concepts SET last_studied_at=? WHERE id=?",
                (utc_now_iso(), concept_id),
            )

    # ------------------------------------------------------------------
    # AssessmentRecord (evidence capture — never touches mastery)
    # ------------------------------------------------------------------

    def record_assessment(
        self,
        course_id: str,
        concept_id: str,
        source: str = AssessmentSource.QUIZ.value,
        score: float = 0.0,
        confidence: float = 1.0,
        difficulty: str = Difficulty.RECALL.value,
        ai_generated: bool = False,
        session_id: str | None = None,
        source_refs: Iterable[SourceRef] = (),
        created_at: str | None = None,
    ) -> AssessmentRecord:
        """Insert one assessment record. Pure evidence capture: the concept's
        mastery_level is NOT touched (test-locked). ``created_at`` is an
        optional explicit UTC-ISO timestamp (rule-engine simulation and
        audit replay); it defaults to the store clock."""
        concept = self.get_concept(concept_id)
        if concept is None or concept.course_id != course_id:
            raise LearningStoreError("concept does not belong to course")
        if source not in AssessmentSource._value2member_map_:
            raise LearningStoreError(f"invalid assessment source: {source}")
        if difficulty not in Difficulty._value2member_map_:
            raise LearningStoreError(f"invalid difficulty: {difficulty}")
        score = float(score)
        confidence = float(confidence)
        if not (0.0 <= score <= 1.0):
            raise LearningStoreError("score must be within 0.0-1.0")
        if not (0.0 <= confidence <= 1.0):
            raise LearningStoreError("confidence must be within 0.0-1.0")
        now = created_at or utc_now_iso()
        assessment_id = _new_id()
        refs = [self._as_source_ref(r) for r in source_refs]
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO assessment_records (id, course_id, concept_id,"
                    " session_id, source, score, confidence, difficulty,"
                    " ai_generated, source_refs, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (assessment_id, course_id, concept_id, session_id, source,
                     score, confidence, difficulty, int(bool(ai_generated)),
                     _json_dumps([r.to_dict() for r in refs]), now),
                )
        except sqlite3.IntegrityError as exc:
            raise LearningStoreError(
                f"assessment references unknown session: {session_id}"
            ) from exc
        return AssessmentRecord(
            id=assessment_id, course_id=course_id, concept_id=concept_id,
            session_id=session_id, source=source, score=score,
            confidence=confidence, difficulty=difficulty,
            ai_generated=bool(ai_generated), source_refs=list(refs),
            created_at=now,
        )

    def list_assessments(self, concept_id: str | None = None) -> list[AssessmentRecord]:
        sql = "SELECT * FROM assessment_records"
        params: tuple = ()
        if concept_id is not None:
            sql += " WHERE concept_id=?"
            params = (concept_id,)
        sql += " ORDER BY created_at"
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._assessment_from_row(row) for row in rows]

    # ------------------------------------------------------------------
    # ReviewItem (schedule is owned by the future Rule Engine)
    # ------------------------------------------------------------------

    def create_review_item(
        self,
        concept_id: str,
        due_at: str,
        interval_days: int = 1,
        retention: str = Retention.LOW.value,
        status: str = "pending",
    ) -> ReviewItem:
        concept = self.get_concept(concept_id)
        if concept is None:
            raise LearningStoreError(f"concept not found: {concept_id}")
        if not due_at:
            raise LearningStoreError("due_at is required")
        if interval_days < 1:
            raise LearningStoreError("interval_days must be >= 1")
        retention = self._validate_retention(retention)
        now = utc_now_iso()
        review_id = _new_id()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO review_items (id, concept_id, due_at, status,"
                " interval_days, consecutive_success, retention, created_at,"
                " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (review_id, concept_id, due_at, status, interval_days, 0,
                 retention, now, now),
            )
        return ReviewItem(id=review_id, concept_id=concept_id, due_at=due_at,
                          status=status, interval_days=interval_days,
                          consecutive_success=0, retention=retention,
                          created_at=now, updated_at=now)

    def update_review_item(
        self,
        review_id: str,
        *,
        status: str | None = None,
        due_at: str | None = None,
        interval_days: int | None = None,
        consecutive_success: int | None = None,
        retention: str | None = None,
    ) -> ReviewItem | None:
        item = self.get_review_item(review_id)
        if item is None:
            return None
        fields: list[str] = []
        params: list[Any] = []
        if status is not None:
            if status not in ("pending", "done", "skipped"):
                raise LearningStoreError(f"invalid review status: {status}")
            fields.append("status=?"), params.append(status)
        if due_at is not None:
            fields.append("due_at=?"), params.append(due_at)
        if interval_days is not None:
            if interval_days < 1:
                raise LearningStoreError("interval_days must be >= 1")
            fields.append("interval_days=?"), params.append(interval_days)
        if consecutive_success is not None:
            if consecutive_success < 0:
                raise LearningStoreError("consecutive_success must be >= 0")
            fields.append("consecutive_success=?"), params.append(consecutive_success)
        if retention is not None:
            fields.append("retention=?"), params.append(self._validate_retention(retention))
        if not fields:
            return item
        fields.append("updated_at=?"), params.append(utc_now_iso())
        params.append(review_id)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE review_items SET {', '.join(fields)} WHERE id=?",
                params,
            )
        return self.get_review_item(review_id)

    def get_review_item(self, review_id: str) -> ReviewItem | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM review_items WHERE id=?", (review_id,)
            ).fetchone()
        return self._review_from_row(row) if row is not None else None

    def list_review_items(self, concept_id: str | None = None) -> list[ReviewItem]:
        sql = "SELECT * FROM review_items"
        params: tuple = ()
        if concept_id is not None:
            sql += " WHERE concept_id=?"
            params = (concept_id,)
        sql += " ORDER BY due_at"
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._review_from_row(row) for row in rows]

    def get_due_reviews(self, now: str | None = None) -> list[ReviewItem]:
        """Pending reviews with due_at <= now (query only; scheduling rules
        live in the future Rule Engine)."""
        now = now or utc_now_iso()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM review_items WHERE status='pending' AND due_at <= ?"
                " ORDER BY due_at",
                (now,),
            ).fetchall()
        return [self._review_from_row(row) for row in rows]

    # ------------------------------------------------------------------
    # export / clear
    # ------------------------------------------------------------------

    def export_course(self, course_id: str) -> dict[str, Any]:
        """Read-only structured export of one course (JSON-compatible)."""
        course = self.get_course(course_id)
        if course is None:
            raise LearningStoreError(f"course not found: {course_id}")
        concepts = self.list_concepts(course_id)
        concept_ids = [c.id for c in concepts]
        assessments = self.list_assessments()  # filtered below
        assessments = [a for a in assessments if a.concept_id in concept_ids]
        reviews = self.list_review_items()
        reviews = [r for r in reviews if r.concept_id in concept_ids]
        sessions = self.list_sessions(course_id)
        return {
            "schema_version": SCHEMA_VERSION,
            "exported_at": utc_now_iso(),
            "course": self._course_to_dict(course),
            "concepts": [self._concept_to_dict(c) for c in concepts],
            "assessment_records": [
                self._assessment_to_dict(a) for a in assessments
            ],
            "review_items": [self._review_to_dict(r) for r in reviews],
            "study_sessions": [self._session_to_dict(s) for s in sessions],
        }

    def clear_learning_data(self) -> None:
        """Explicit full reset of learning facts (never called at startup).
        Schema/meta stay intact."""
        with self._connect() as connection:
            for table in (
                "assessment_records",
                "review_items",
                "study_sessions",
                "concepts",
                "learning_audit",
                "courses",
            ):
                connection.execute(f"DELETE FROM {table}")

    # ------------------------------------------------------------------
    # row mapping helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _course_from_row(row: sqlite3.Row) -> Course:
        return Course(
            id=row["id"], name=row["name"], description=row["description"],
            status=row["status"], source_title=row["source_title"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    @staticmethod
    def _course_to_dict(course: Course) -> dict[str, Any]:
        return {
            "id": course.id, "name": course.name,
            "description": course.description, "status": course.status,
            "source_title": course.source_title,
            "created_at": course.created_at, "updated_at": course.updated_at,
        }

    @staticmethod
    def _concept_from_row(row: sqlite3.Row) -> Concept:
        return Concept(
            id=row["id"], course_id=row["course_id"],
            canonical_name=row["canonical_name"],
            normalized_name=row["normalized_name"],
            aliases=_json_loads(row["aliases"], []),
            mastery_level=row["mastery_level"], retention=row["retention"],
            state=row["state"],
            source_refs=[
                SourceRef.from_dict(d)
                for d in _json_loads(row["source_refs"], [])
            ],
            last_studied_at=row["last_studied_at"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    @staticmethod
    def _concept_to_dict(concept: Concept) -> dict[str, Any]:
        return {
            "id": concept.id, "course_id": concept.course_id,
            "canonical_name": concept.canonical_name,
            "normalized_name": concept.normalized_name,
            "aliases": list(concept.aliases),
            "mastery_level": concept.mastery_level,
            "retention": concept.retention, "state": concept.state,
            "source_refs": [r.to_dict() for r in concept.source_refs],
            "last_studied_at": concept.last_studied_at,
            "created_at": concept.created_at, "updated_at": concept.updated_at,
        }

    @staticmethod
    def _session_from_row(row: sqlite3.Row) -> StudySession:
        return StudySession(
            id=row["id"], course_id=row["course_id"],
            started_at=row["started_at"], ended_at=row["ended_at"],
            status=row["status"], summary=row["summary"],
            concept_ids=_json_loads(row["concept_ids"], []),
            activities=_json_loads(row["activities"], []),
            created_at=row["created_at"],
        )

    @staticmethod
    def _session_to_dict(session: StudySession) -> dict[str, Any]:
        return {
            "id": session.id, "course_id": session.course_id,
            "started_at": session.started_at, "ended_at": session.ended_at,
            "status": session.status, "summary": session.summary,
            "concept_ids": list(session.concept_ids),
            "activities": list(session.activities),
            "created_at": session.created_at,
        }

    @staticmethod
    def _assessment_from_row(row: sqlite3.Row) -> AssessmentRecord:
        return AssessmentRecord(
            id=row["id"], course_id=row["course_id"], concept_id=row["concept_id"],
            session_id=row["session_id"], source=row["source"],
            score=row["score"], confidence=row["confidence"],
            difficulty=row["difficulty"], ai_generated=bool(row["ai_generated"]),
            source_refs=[
                SourceRef.from_dict(d)
                for d in _json_loads(row["source_refs"], [])
            ],
            created_at=row["created_at"],
        )

    @staticmethod
    def _assessment_to_dict(record: AssessmentRecord) -> dict[str, Any]:
        return {
            "id": record.id, "course_id": record.course_id,
            "concept_id": record.concept_id, "session_id": record.session_id,
            "source": record.source, "score": record.score,
            "confidence": record.confidence, "difficulty": record.difficulty,
            "ai_generated": record.ai_generated,
            "source_refs": [r.to_dict() for r in record.source_refs],
            "created_at": record.created_at,
        }

    @staticmethod
    def _review_from_row(row: sqlite3.Row) -> ReviewItem:
        return ReviewItem(
            id=row["id"], concept_id=row["concept_id"], due_at=row["due_at"],
            status=row["status"], interval_days=row["interval_days"],
            consecutive_success=row["consecutive_success"],
            retention=row["retention"], created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _review_to_dict(item: ReviewItem) -> dict[str, Any]:
        return {
            "id": item.id, "concept_id": item.concept_id, "due_at": item.due_at,
            "status": item.status, "interval_days": item.interval_days,
            "consecutive_success": item.consecutive_success,
            "retention": item.retention, "created_at": item.created_at,
            "updated_at": item.updated_at,
        }

    # ------------------------------------------------------------------
    # validation helpers
    # ------------------------------------------------------------------

    def _require_course(self, course_id: str) -> None:
        if self.get_course(course_id) is None:
            raise LearningStoreError(f"course not found: {course_id}")

    @staticmethod
    def _validate_mastery(value: int) -> int:
        value = int(value)
        if not 0 <= value <= 5:
            raise LearningStoreError("mastery_level must be an integer 0-5")
        return value

    @staticmethod
    def _validate_retention(value: str) -> str:
        if value not in Retention._value2member_map_:
            raise LearningStoreError(f"invalid retention: {value}")
        return value

    @staticmethod
    def _validate_state(value: str) -> str:
        if value not in ConceptState._value2member_map_:
            raise LearningStoreError(f"invalid concept state: {value}")
        return value

    @staticmethod
    def _as_source_ref(value: SourceRef | dict[str, Any]) -> SourceRef:
        if isinstance(value, SourceRef):
            return value
        return SourceRef.from_dict(value)

    def _update_concept_row(self, concept_id: str, *, aliases: list[str],
                            source_refs: list[SourceRef]) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE concepts SET aliases=?, source_refs=?, updated_at=?"
                " WHERE id=?",
                (_json_dumps(aliases),
                 _json_dumps([r.to_dict() for r in source_refs]),
                 utc_now_iso(), concept_id),
            )
