"""Firefly-side input adapter for an isolated teach-mcp curriculum builder run.

The teach-mcp source and its configured DRAFT_ROOT are never changed. This
module loads a separate module instance and supplies manifest-declared nodes.
"""

from __future__ import annotations

from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import re
from types import ModuleType
import uuid

import yaml


MCP_BUILDER = Path(r"E:\Firefly_AI_MCP\teach_mcp\curriculum_draft.py")
SECTION_NUMBER = re.compile(r"^\s*(\d+)\s*[-–]\s*(\d+)\s+")


def load_isolated_builder(state_root: Path, source: Path = MCP_BUILDER) -> ModuleType:
    if not source.is_file():
        raise FileNotFoundError(source)
    name = f"firefly_isolated_curriculum_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, source)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load curriculum builder")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.DRAFT_ROOT = Path(state_root).resolve()
    return module


def _units(draft: dict) -> list[dict]:
    return [unit for chapter in draft.get("chapters", []) for unit in chapter.get("units", [])]


def declared_nodes(draft: dict, run_root: Path) -> list[dict]:
    """Prefer v2 IDs; for v1 use stored concept ID, never a path basename."""
    schema = draft.get("schema")
    if schema not in ("firefly.curriculum.preflight.v1", "firefly.curriculum.preflight.v2"):
        raise ValueError("unsupported curriculum draft schema")
    course_id = draft.get("course", {}).get("course_id")
    if not course_id or draft.get("course", {}).get("registered") is not False:
        raise ValueError("course identity missing or draft already registered")
    nodes: list[dict] = []
    topic_ids: set[str] = set()
    package_root = (Path(run_root) / "draft_packages" / course_id).resolve()
    for unit in _units(draft):
        chapter_id = unit.get("chapter_id") or unit.get("unit_id")
        if unit.get("course_id") not in (None, course_id):
            raise ValueError("unit course_id mismatch")
        if unit.get("chapter_id") not in (None, unit.get("unit_id")):
            raise ValueError("unit chapter_id mismatch")
        concepts = unit.get("concepts") or []
        if len(concepts) != 1:
            raise ValueError("each unit needs one concept")
        concept = concepts[0]
        concept_id = concept.get("concept_id") or concept.get("id")
        if not concept_id:
            raise ValueError("concept_id missing")
        if schema.endswith(".v2"):
            topic_id = unit.get("topic_id") or concept.get("topic_id")
            if not topic_id or concept.get("topic_id") != topic_id:
                raise ValueError("v2 declared topic_id missing or inconsistent")
        else:
            topic_id = unit.get("topic_id") or concept.get("topic_id") or concept_id
        if topic_id in topic_ids:
            raise ValueError(f"duplicate declared topic_id: {topic_id}")
        topic_ids.add(topic_id)
        package = (Path(run_root) / unit["package_path"]).resolve()
        if schema.endswith(".v2"):
            try:
                package.relative_to(package_root)
            except ValueError as exc:
                raise ValueError("v2 package outside isolated course root") from exc
        if not (package / "concept.yaml").is_file():
            raise ValueError(f"concept.yaml missing for {chapter_id}")
        stored_concept = yaml.safe_load((package / "concept.yaml").read_text(encoding="utf-8")) or {}
        nodes.append({"topic_id": topic_id, "course_id": course_id,
                      "chapter_id": chapter_id, "concept_id": concept_id,
                      "title": concept.get("title") or stored_concept.get("topic"),
                      "aliases": stored_concept.get("aliases", []),
                      "core_points": [point.get("text", "") for point in concept.get("core_points", [])],
                      "raw_prerequisites": list(concept.get("prerequisites", [])),
                      "package_path": str(package)})
    if len(nodes) < 2:
        raise ValueError("builder needs at least two declared nodes")
    return nodes


def consume_with_builder(draft: dict, *, run_root: Path, state_root: Path,
                         builder_source: Path = MCP_BUILDER) -> dict:
    """Run the real builder state machine with declared IDs in local state."""
    nodes = declared_nodes(draft, run_root)
    module = load_isolated_builder(state_root, builder_source)
    by_path = {node["package_path"]: node for node in nodes}
    if len(by_path) != len(nodes):
        raise ValueError("duplicate package path")

    def input_node(package_path: str) -> dict | None:
        node = by_path.get(str(Path(package_path).resolve()))
        return dict(node) if node else None

    module.register_node = input_node
    package_paths = [node["package_path"] for node in nodes]
    start = module.start_curriculum_impl(
        package_paths, goal="全书教材草稿结构复核", title=draft["course"].get("title", "课程草稿"))
    if start.get("status") != "ok":
        raise RuntimeError(f"builder start failed: {start}")
    curriculum_id = start["curriculum_id"]
    expected = [node["topic_id"] for node in nodes]
    state = module._load(curriculum_id)
    if state["topic_ids"] != expected:
        raise RuntimeError("builder changed declared topic IDs at start")
    # Preflight has no approved prerequisite graph. Preserve every statement as
    # external rather than inventing course-internal edges.
    resolved = 0
    for _ in range(sum(len(node["raw_prerequisites"]) for node in nodes) + 2):
        step = module.get_curriculum_step_impl(curriculum_id)
        if step.get("step") == "resolve_prerequisite":
            response = module.submit_curriculum_step_impl(curriculum_id, {"match": "external"})
            if response.get("status") != "ok":
                raise RuntimeError(f"prerequisite step failed: {response}")
            resolved += 1
            continue
        if step.get("step") == "goal_priority":
            response = module.submit_curriculum_step_impl(curriculum_id, {"priority_topics": []})
            if response.get("status") != "ok":
                raise RuntimeError(f"priority step failed: {response}")
            break
        raise RuntimeError(f"unexpected builder step: {step}")
    else:
        raise RuntimeError("builder prerequisite step budget exceeded")
    finalized = module.finalize_curriculum_impl(curriculum_id)
    state = module._load(curriculum_id)
    if finalized.get("status") != "success" or state.get("status") != "finalized":
        raise RuntimeError(f"builder finalize failed: {finalized}")
    if (state["topic_ids"] != expected or
            [item["topic_id"] for item in state["plan"]] != expected or
            len({item["topic_id"] for item in state["plan"]}) != len(expected)):
        raise RuntimeError("builder changed or collapsed declared topic IDs")
    if any(node["topic_id"] != source["topic_id"] for node, source in zip(state["nodes"], nodes)):
        raise RuntimeError("builder node identity changed")
    return {"curriculum_id": curriculum_id, "state_path": str(module._cpath(curriculum_id)),
            "builder_source": str(builder_source), "builder_source_sha256": sha256(builder_source.read_bytes()).hexdigest(),
            "nodes": state["nodes"], "plan": state["plan"],
            "topic_ids": state["topic_ids"], "resolved_as_external": resolved,
            "unresolved_prerequisites": state["unresolved_prerequisites"],
            "status": state["status"]}


def build_hierarchy(draft: dict, nodes: list[dict]) -> dict:
    """Use chapter bookmarks as chapter boundaries and keep uncertain units aside."""
    if draft.get("schema") != "firefly.curriculum.preflight.v2":
        raise ValueError("hierarchy requires v2 title classifications")
    chapters = []
    for source in draft["chapters"]:
        classification = source.get("title_classification") or {}
        if classification.get("classified_type") != "CHAPTER_TITLE":
            raise ValueError("chapter lacks a confirmed chapter bookmark")
        bookmark = classification.get("evidence", {}).get("bookmark") or {}
        if not isinstance(bookmark.get("page"), int):
            raise ValueError("chapter bookmark page missing")
        chapters.append({"chapter_id": source["chapter_id"], "order": source["order"],
                         "title": source["title"], "start_page": bookmark["page"],
                         "end_page": None, "sections": [], "exercises": []})
    chapters.sort(key=lambda item: item["start_page"])
    if len({item["chapter_id"] for item in chapters}) != len(chapters):
        raise ValueError("duplicate book chapter IDs")
    last_page = max(unit["page_range"][1] for unit in _units(draft))
    for index, chapter in enumerate(chapters):
        chapter["end_page"] = chapters[index + 1]["start_page"] - 1 if index + 1 < len(chapters) else last_page
    by_chapter_id = {node["chapter_id"]: node for node in nodes}
    topics = []
    review_units = []
    for unit in _units(draft):
        node = by_chapter_id.get(unit["unit_id"])
        if node is None or node["topic_id"] != unit["topic_id"]:
            raise ValueError(f"unit not represented by builder node: {unit['unit_id']}")
        start, end = unit["page_range"]
        memberships = [chapter for chapter in chapters
                       if start <= chapter["end_page"] and end >= chapter["start_page"]]
        if not memberships:
            raise ValueError(f"unit has no chapter overlap: {unit['unit_id']}")
        title = unit["title_classification"]
        kind = title["classified_type"]
        match = SECTION_NUMBER.match(unit["source_heading"])
        section_number = (f"{match.group(1)}-{match.group(2)}"
                          if kind == "SECTION_TITLE" and match else None)
        entry = {"unit_id": unit["unit_id"], "topic_id": unit["topic_id"],
                 "concept_id": unit["concepts"][0]["id"],
                 "lesson_id": unit["lessons"][0]["id"],
                 "quiz_id": unit["quizzes"][0]["id"],
                 "title": unit["title"], "original_title": title["original_title"],
                 "classified_type": kind, "decision_reason": title["decision_reason"],
                 "section_number": section_number, "page_range": [start, end],
                 "book_chapter_ids": [chapter["chapter_id"] for chapter in memberships],
                 "package_path": unit["package_path"]}
        topics.append(entry)
        if len(memberships) > 1 or kind in ("NOISE", "TRUNCATED_TITLE", "CHAPTER_TITLE"):
            entry["hierarchy_status"] = "REVIEW_REQUIRED"
            review_units.append(entry)
        elif kind == "EXERCISE_TITLE":
            entry["hierarchy_status"] = "EXERCISE_NOT_CHAPTER"
            memberships[0]["exercises"].append(entry)
        elif kind == "SECTION_TITLE":
            entry["hierarchy_status"] = "SECTION"
            memberships[0]["sections"].append(entry)
        else:
            raise ValueError(f"unsupported title type: {kind}")
    return {"chapters": chapters, "topics": topics, "review_units": review_units}
