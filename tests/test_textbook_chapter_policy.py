"""Regression gates for the 692-page PDF import boundary.

teach-mcp is imported as-is; every draft and exported unit goes to tmp_path.
"""

from __future__ import annotations

from pathlib import Path
import os

import pytest
import yaml

from learning.skill_source.textbook_chapter_policy import (
    bookmark_policy,
    recommend_decision,
)


ROOT = Path(__file__).resolve().parents[1]
MCP = Path(os.environ.get("FIREFLY_TEACH_MCP_ROOT", ""))
TEXTBOOK = Path(os.environ.get("FIREFLY_TEST_TEXTBOOK", ""))


@pytest.fixture
def mcp_modules(monkeypatch):
    if (os.environ.get("FIREFLY_RUN_TEACH_MCP_INTEGRATION") != "1"
            or not os.environ.get("FIREFLY_TEACH_MCP_ROOT")
            or not os.environ.get("FIREFLY_TEST_TEXTBOOK")):
        pytest.skip("explicit external teach-mcp/textbook integration opt-in required")
    if not MCP.is_dir():
        pytest.skip("teach-mcp sibling checkout unavailable")
    monkeypatch.syspath_prepend(str(MCP))
    import chapter_discovery
    import curriculum_draft
    import book_pipeline
    return chapter_discovery, curriculum_draft, book_pipeline


def test_692_page_textbook_discovery_drains_verified_ambiguities(tmp_path, monkeypatch, mcp_modules):
    if not TEXTBOOK.is_file():
        pytest.skip("692-page source textbook unavailable")
    discovery, _, pipeline = mcp_modules
    monkeypatch.setattr(discovery, "DRAFT_ROOT", tmp_path / "chapter_discovery")
    monkeypatch.setattr(discovery, "UNITS_ROOT", tmp_path / "chapter_units")
    monkeypatch.setattr(pipeline, "RUN_ROOT", tmp_path / "book_pipeline")
    policy = bookmark_policy(TEXTBOOK)
    assert policy["page_count"] == 692
    assert policy["chapter_count"] == 10
    assert not any(r["start_page"] <= 633 <= r["end_page"]
                   for r in policy["exercise_ranges"])

    run = pipeline.start_book_pipeline_impl(str(TEXTBOOK), learner_id="isolated-test")
    assert run["pipeline_status"] == "PAUSED"
    assert run["stage"] == "DISCOVERY_REVIEW"
    status = pipeline.get_book_pipeline_status_impl(run["pipeline_id"])
    assert status["pipeline"]["curriculum_id"] is None
    discovery_id = status["pipeline"]["discovery_id"]
    started = discovery._load(discovery_id)
    assert started["status"] == "resolving"
    assert len(started["ambiguous_items"]) == 34
    remaining = len(started["ambiguous_items"])
    evidence = []
    while remaining:
        step = discovery.get_chapter_discovery_step_impl(discovery_id)
        assert step["step"] == "resolve_ambiguous"
        assert step["remaining"] == remaining
        decision = recommend_decision(step["current_item"], policy)
        assert decision is not None, f"requires review on page {step['current_item']['page']}"
        evidence.append(decision["evidence"])
        submitted = discovery.submit_chapter_discovery_step_impl(
            discovery_id, {k: decision[k] for k in
                           ("candidate_id", "decision", "normalized_title")})
        assert submitted["status"] == "ok"
        assert submitted["remaining"] == remaining - 1
        remaining -= 1
    assert evidence.count("bookmarked_exercise_range") == 32
    assert evidence.count("matching_section_bookmark") == 1
    assert discovery.get_chapter_discovery_step_impl(discovery_id)["step"] == "finalize"
    final = discovery.finalize_chapter_discovery_impl(discovery_id)
    assert final["status"] == "success"
    assert final["chapter_count"] >= 10
    assert all(Path(unit["content"]).is_file() for unit in final["units"])
    resumed = pipeline.resume_book_pipeline_impl(run["pipeline_id"], max_steps=1)
    assert resumed["stage"] == "AUTHORING"
    assert resumed["pipeline_status"] == "running"


def test_chapter_rule_keeps_unverified_item_for_review():
    policy = {"first_chapter_page": 9, "exercise_ranges": [],
              "section_bookmarks": [{"page": 82, "chapter": 3, "section": 1,
                                     "title": "系统时间响应的性能指标"}]}
    item = {"page": 82, "reason": "same_page_multiple_headings",
            "candidates": [
                {"candidate_id": "a", "ch": 3, "sec": 1,
                 "title": "系统时间响应的性能指标"},
                {"candidate_id": "b", "ch": 3, "sec": 2, "title": "误识别文本"},
            ]}
    assert recommend_decision(item, policy)["candidate_id"] == "a"
    assert recommend_decision(item, policy)["decision"] == "chapter"
    item["page"] = 83
    assert recommend_decision(item, policy) is None


def test_curriculum_generation_from_discovered_bookmark_topics(tmp_path, monkeypatch,
                                                               mcp_modules):
    if not TEXTBOOK.is_file():
        pytest.skip("692-page source textbook unavailable")
    _, curriculum, _ = mcp_modules
    monkeypatch.setattr(curriculum, "DRAFT_ROOT", tmp_path / "curriculum_drafts")
    policy = bookmark_policy(TEXTBOOK)
    section_titles = [entry["title"] for entry in policy["section_bookmarks"][:2]]
    assert len(section_titles) == 2
    paths = []
    for index, title in enumerate(section_titles, 1):
        package = tmp_path / f"chapter_{index}"
        package.mkdir()
        (package / "concept.yaml").write_text(
            yaml.safe_dump({"topic": title, "core_points": [{"text": title}],
                            "prerequisites": []}, allow_unicode=True), encoding="utf-8")
        paths.append(str(package))
    started = curriculum.start_curriculum_impl(paths, title="自动控制原理导入验证")
    assert started["status"] == "ok"
    assert started["topic_count"] == 2
    assert curriculum.submit_curriculum_step_impl(
        started["curriculum_id"], {"priority_topics": []})["status"] == "ok"
    final = curriculum.finalize_curriculum_impl(started["curriculum_id"])
    assert final["status"] == "success"
    assert [topic["title"] for topic in final["plan"]] == section_titles
    assert curriculum.DRAFT_ROOT.parent == tmp_path
