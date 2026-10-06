"""Course/chapter/package namespace for authoring drafts in the MCP process.

Only output routing changes: model calls, generated spec and package validator
remain with teach-mcp. Direct authoring without a chapter ID retains its path.
"""

from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from threading import RLock
from typing import Iterator

_CHAPTER: ContextVar[str | None] = ContextVar("firefly_authoring_chapter", default=None)
_COURSE: ContextVar[str | None] = ContextVar("firefly_authoring_course", default=None)
_PACKAGE: ContextVar[str | None] = ContextVar("firefly_authoring_package", default=None)
_BUILD_LOCK = RLock()
_CHAPTER_RE = re.compile(r"ch\d{2,3}", re.ASCII)
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", re.ASCII)
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
             *(f"lpt{i}" for i in range(1, 10))}


class CourseNamespaceError(ValueError):
    """A Firefly course/package ID cannot safely name an output directory."""


def _safe_id(value: str, kind: str) -> str:
    candidate = str(value or "")
    if not _ID_RE.fullmatch(candidate) or candidate.lower() in _RESERVED:
        raise CourseNamespaceError(f"invalid {kind} namespace")
    return candidate


def chapter_id_from_slug(slug: str) -> str | None:
    """Accept only pipeline-issued chapter IDs, never arbitrary model text."""
    candidate = str(slug or "").lower()
    return candidate if _CHAPTER_RE.fullmatch(candidate) else None


def course_id_from_context(
    *, env: dict[str, str] | None = None, cwd: Path | None = None
) -> str | None:
    """Read the existing Firefly context; never derive an ID from a filename."""
    environment = os.environ if env is None else env
    selected = environment.get("FIREFLY_LEARNING_CONTEXT", "").strip()
    path = Path(selected) if selected else Path(cwd or Path.cwd()) / ".firefly" / "learning_context.json"
    if not path.is_file():
        if selected:
            raise CourseNamespaceError("Firefly learning context is unavailable")
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        course_id = _safe_id(data["course_id"], "course_id")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CourseNamespaceError("Firefly learning context has no valid course_id") from exc
    if (path.parent.name != ".firefly" or path.parent.parent.name != "workspace"
            or path.parent.parent.parent.name != course_id):
        raise CourseNamespaceError("Firefly learning context course path mismatch")
    return course_id


def resolve_draft_root(
    base: Path, chapter_id: str | None,
    course_id: str | None = None, package_id: str | None = None,
) -> Path:
    """Place IDs above teach-mcp's unchanged domain/slug suffix."""
    if chapter_id is None:
        return Path(base)
    if not _CHAPTER_RE.fullmatch(chapter_id):
        raise ValueError("invalid chapter namespace")
    if course_id is not None:
        course = _safe_id(course_id, "course_id")
        if package_id is None:
            raise CourseNamespaceError("package_id missing for course output")
        package = _safe_id(package_id, "package_id")
        return Path(base) / course / chapter_id / package
    return Path(base) / chapter_id


@contextmanager
def chapter_output(chapter_id: str | None, course_id: str | None = None) -> Iterator[None]:
    """Bind one authoring call to stable course/chapter output namespaces."""
    chapter_token = _CHAPTER.set(chapter_id_from_slug(chapter_id or ""))
    course_token = _COURSE.set(_safe_id(course_id, "course_id") if course_id else None)
    try:
        yield
    finally:
        _COURSE.reset(course_token)
        _CHAPTER.reset(chapter_token)


def install_output_path_resolver() -> None:
    """Attach to existing session/build edges without editing teach-mcp."""
    import book_pipeline
    import authoring_draft
    from knowledge_authoring import builders

    if not getattr(book_pipeline.run_authoring_session, "_firefly_output_resolver", False):
        original_run = book_pipeline.run_authoring_session

        def run_with_chapter(session, material_text: str, slug: str, trace=None):
            chapter = chapter_id_from_slug(slug)
            course = course_id_from_context() if chapter else None
            with chapter_output(chapter, course):
                return original_run(session, material_text, slug, trace)

        run_with_chapter._firefly_output_resolver = True  # type: ignore[attr-defined]
        book_pipeline.run_authoring_session = run_with_chapter

    if not getattr(authoring_draft.finalize_authoring_impl, "_firefly_output_resolver", False):
        original_finalize = authoring_draft.finalize_authoring_impl

        def finalize_with_package(draft_id: str) -> dict:
            package_id = (
                _safe_id(draft_id, "package_id")
                if _COURSE.get() and _CHAPTER.get() else None
            )
            token = _PACKAGE.set(package_id)
            try:
                return original_finalize(draft_id)
            finally:
                _PACKAGE.reset(token)

        finalize_with_package._firefly_output_resolver = True  # type: ignore[attr-defined]
        authoring_draft.finalize_authoring_impl = finalize_with_package

    if getattr(builders.build_package, "_firefly_output_resolver", False):
        return
    original_build = builders.build_package

    def build_in_namespace(domain: str, slug: str, spec: dict) -> dict:
        # builders.DRAFT_DIR is a module global. Serialize all calls while its
        # value is scoped so concurrent chapters cannot redirect each other.
        with _BUILD_LOCK:
            original_root = builders.DRAFT_DIR
            builders.DRAFT_DIR = resolve_draft_root(
                original_root, _CHAPTER.get(), _COURSE.get(), _PACKAGE.get()
            )
            try:
                return original_build(domain, slug, spec)
            finally:
                builders.DRAFT_DIR = original_root

    build_in_namespace._firefly_output_resolver = True  # type: ignore[attr-defined]
    builders.build_package = build_in_namespace
