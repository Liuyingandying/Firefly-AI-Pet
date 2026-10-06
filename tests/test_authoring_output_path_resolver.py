"""Stable chapter namespaces for shared authoring draft packages."""

from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if os.environ.get("FIREFLY_TEACH_MCP_ROOT"):
    sys.path.insert(0, os.environ["FIREFLY_TEACH_MCP_ROOT"])

from tools.authoring_output_path_resolver import (  # noqa: E402
    CourseNamespaceError,
    course_id_from_context,
    install_output_path_resolver,
    resolve_draft_root,
)


def test_ch01_ch02_paths_are_unique_and_resume_stable(tmp_path):
    first = resolve_draft_root(tmp_path, "ch01") / "control_system" / "topic"
    second = resolve_draft_root(tmp_path, "ch02") / "control_system" / "topic"
    assert first != second
    assert first == resolve_draft_root(tmp_path, "ch01") / "control_system" / "topic"
    with pytest.raises(ValueError):
        resolve_draft_root(tmp_path, "../ch02")


def test_control_theory_course_context_and_package_path(tmp_path):
    context = tmp_path / "courses/control_theory/workspace/.firefly/learning_context.json"
    context.parent.mkdir(parents=True)
    context.write_text(json.dumps({"course_id": "control_theory"}), encoding="utf-8")
    course_id = course_id_from_context(env={"FIREFLY_LEARNING_CONTEXT": str(context)})
    assert course_id == "control_theory"
    path = resolve_draft_root(tmp_path, "ch01", course_id, "ad-1111111111")
    assert path == tmp_path / "control_theory/ch01/ad-1111111111"
    assert path == resolve_draft_root(tmp_path, "ch01", course_id, "ad-1111111111")
    assert path != resolve_draft_root(tmp_path, "ch01", course_id, "ad-2222222222")


def test_another_course_same_ch01_cannot_collide(tmp_path):
    a = resolve_draft_root(tmp_path, "ch01", "control_theory", "ad-1111111111")
    b = resolve_draft_root(tmp_path, "ch01", "another_course", "ad-1111111111")
    assert a != b
    with pytest.raises(CourseNamespaceError, match="package_id missing"):
        resolve_draft_root(tmp_path, "ch01", "control_theory")
    with pytest.raises(CourseNamespaceError):
        resolve_draft_root(tmp_path, "ch01", "../another_course", "ad-1111111111")


def test_course_context_flows_through_session_and_finalize(tmp_path, monkeypatch):
    if os.environ.get("FIREFLY_RUN_TEACH_MCP_INTEGRATION") != "1":
        pytest.skip("explicit external teach-mcp integration opt-in required")
    authoring_draft = pytest.importorskip("authoring_draft", reason="optional teach-mcp runtime unavailable")
    book_pipeline = pytest.importorskip("book_pipeline", reason="optional teach-mcp runtime unavailable")
    builders = pytest.importorskip("knowledge_authoring.builders", reason="optional teach-mcp runtime unavailable")

    context = tmp_path / "courses/control_theory/workspace/.firefly/learning_context.json"
    context.parent.mkdir(parents=True)
    context.write_text(json.dumps({"course_id": "control_theory"}), encoding="utf-8")
    monkeypatch.setenv("FIREFLY_LEARNING_CONTEXT", str(context))
    monkeypatch.setattr(builders, "DRAFT_DIR", tmp_path / "draft_packages")

    def fake_build(domain: str, slug: str, spec: dict) -> dict:
        path = builders.DRAFT_DIR / domain / slug
        path.mkdir(parents=True, exist_ok=True)
        (path / "marker.txt").write_text(spec["package_id"], encoding="utf-8")
        return {"status": "success", "draft_path": str(path)}

    def fake_finalize(draft_id: str) -> dict:
        return builders.build_package("control_system", "topic", {"package_id": draft_id})

    def fake_run(session, material_text: str, slug: str, trace=None) -> dict:
        return authoring_draft.finalize_authoring_impl("ad-1111111111")

    monkeypatch.setattr(builders, "build_package", fake_build)
    monkeypatch.setattr(authoring_draft, "finalize_authoring_impl", fake_finalize)
    monkeypatch.setattr(book_pipeline, "run_authoring_session", fake_run)
    install_output_path_resolver()

    result = book_pipeline.run_authoring_session(None, "", "ch01")
    path = Path(result["draft_path"])
    assert path == tmp_path / "draft_packages/control_theory/ch01/ad-1111111111/control_system/topic"
    assert (path / "marker.txt").read_text(encoding="utf-8") == "ad-1111111111"


@pytest.fixture()
def shared_resolver(tmp_path, monkeypatch):
    if os.environ.get("FIREFLY_RUN_TEACH_MCP_INTEGRATION") != "1":
        pytest.skip("explicit external teach-mcp integration opt-in required")
    book_pipeline = pytest.importorskip("book_pipeline", reason="optional teach-mcp runtime unavailable")
    builders = pytest.importorskip("knowledge_authoring.builders", reason="optional teach-mcp runtime unavailable")

    monkeypatch.setattr(builders, "DRAFT_DIR", tmp_path / "draft_packages")

    def fake_build(domain: str, slug: str, spec: dict) -> dict:
        path = builders.DRAFT_DIR / domain / slug
        path.mkdir(parents=True, exist_ok=True)
        (path / "marker.txt").write_text(spec["chapter"], encoding="utf-8")
        return {"status": "success", "draft_path": str(path)}

    def fake_run(session, material_text: str, slug: str, trace=None) -> dict:
        return builders.build_package("control_system", "topic", {"chapter": slug})

    monkeypatch.setattr(builders, "build_package", fake_build)
    monkeypatch.setattr(book_pipeline, "run_authoring_session", fake_run)
    install_output_path_resolver()
    return book_pipeline, builders, tmp_path / "draft_packages"


def test_concurrent_chapters_coexist_in_one_shared_draft_root(shared_resolver):
    book_pipeline, _, root = shared_resolver
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda chapter: book_pipeline.run_authoring_session(None, "", chapter),
            ("ch01", "ch02"),
        ))
    paths = [Path(result["draft_path"]) for result in results]
    assert paths == [root / "ch01/control_system/topic", root / "ch02/control_system/topic"]
    assert [path.joinpath("marker.txt").read_text(encoding="utf-8") for path in paths] == [
        "ch01", "ch02"
    ]


def test_direct_authoring_keeps_existing_package_path(shared_resolver):
    _, builders, root = shared_resolver
    result = builders.build_package("control_system", "topic", {"chapter": "legacy"})
    path = Path(result["draft_path"])
    assert path == root / "control_system/topic"
    assert (path / "marker.txt").read_text(encoding="utf-8") == "legacy"
