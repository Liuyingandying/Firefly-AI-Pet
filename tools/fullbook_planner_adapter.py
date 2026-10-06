"""Firefly chapter task state and multi-package adapter for installed teach-mcp.

Installed before server.py loads. The upstream pipeline and provider are unchanged.
"""

from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_CAPTURE: ContextVar[dict[str, dict] | None] = ContextVar("fullbook_capture", default=None)
STATES = {"PENDING", "RUNNING", "FAILED_RETRYABLE", "RETRYING", "SUCCESS"}


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _state(task: dict, new_state: str, error: str = "") -> None:
    if task.get("state") != new_state:
        task.setdefault("history", []).append({
            "timestamp": _now(), "from": task.get("state"), "to": new_state,
            "retry_count": task.get("retry_count", 0), "error": error,
        })
    task["state"] = new_state
    task["updated_at"] = _now()


def _task_id(pipeline_id: str, chapter_id: str) -> str:
    return f"{pipeline_id}:{chapter_id}"


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _checkpoint(run: dict, task: dict) -> dict:
    return {
        "stage": run["stage"],
        "current_chapter": run.get("current_chapter"),
        "updated_at": run.get("updated_at"),
        "draft_ids": list(task["draft_ids"]),
        "package_paths": list(task["package_paths"]),
    }


def _ensure_tasks(run: dict, discovery: dict | None = None) -> dict:
    """Migrate an old checkpoint in memory without losing its legacy fields."""
    tasks = run.setdefault("chapter_tasks", {})
    chapters = (discovery or {}).get("chapters", [])
    known = {chapter["chapter_id"]: chapter for chapter in run.get("chapters", [])}
    for chapter in chapters + list(known.values()):
        cid = chapter["chapter_id"]
        record = known.get(cid, {})
        task = tasks.setdefault(cid, {
            "task_id": _task_id(run["pipeline_id"], cid),
            "chapter_id": cid,
            "state": "SUCCESS" if record.get("status") == "valid" else
                     "FAILED_RETRYABLE" if record.get("status") == "failed" else "PENDING",
            "error": record.get("error", ""), "retry_count": 0,
            "draft_ids": [], "package_paths": [], "checkpoint": {},
            "history": [], "validation": {"success": 0, "error": 0},
            "updated_at": _now(),
        })
        task.setdefault("task_id", _task_id(run["pipeline_id"], cid))
        task.setdefault("chapter_id", cid)
        task.setdefault("error", "")
        task.setdefault("retry_count", 0)
        task.setdefault("draft_ids", [])
        task.setdefault("package_paths", [])
        task.setdefault("checkpoint", {})
        task.setdefault("history", [])
        task.setdefault("validation", {"success": 0, "error": 0})
        task.setdefault("updated_at", _now())
        if record:
            task["draft_ids"] = _unique(task["draft_ids"] +
                                         record.get("authoring_draft_ids", []) +
                                         [record.get("authoring_draft_id")])
            task["package_paths"] = _unique(task["package_paths"] +
                                             record.get("package_paths", []) +
                                             [record.get("package_path")])
            if record.get("status") == "valid":
                _state(task, "SUCCESS")
                task["error"] = ""
            elif record.get("status") == "failed" and task["state"] != "RETRYING":
                _state(task, "FAILED_RETRYABLE", record.get("error", ""))
                task["error"] = record.get("error", "")
        if task["state"] not in STATES:
            raise ValueError(f"invalid task state for {cid}")
        if not task["checkpoint"]:
            task["checkpoint"] = _checkpoint(run, task)
    return tasks


def _packages(result: dict) -> list[tuple[str, str]]:
    """Read a multi-package authoring result; keep the old singular contract."""
    pairs = []
    if result.get("draft_path"):
        pairs.append((result.get("draft_id") or "", result["draft_path"]))
    for item in result.get("packages") or []:
        if not isinstance(item, dict):
            raise ValueError("packages entry must be an object")
        pairs.append((item.get("draft_id") or "", item.get("draft_path") or ""))
    paths = result.get("package_paths") or []
    ids = result.get("draft_ids") or []
    if ids and len(ids) != len(paths):
        raise ValueError("draft_ids and package_paths length mismatch")
    for index, path in enumerate(paths):
        pairs.append((ids[index] if ids else "", path))
    by_path: dict[str, str] = {}
    for draft_id, path in pairs:
        if not isinstance(path, str) or not path or not isinstance(draft_id, str) or not draft_id:
            raise ValueError("each package needs draft_id and draft_path")
        if path in by_path and by_path[path] != draft_id:
            raise ValueError("one package path has conflicting draft IDs")
        by_path[path] = draft_id
    return [(draft_id, path) for path, draft_id in by_path.items()]


def _record_step(bp: Any, run: dict, before: dict, captured: dict[str, dict]) -> None:
    discovery = None
    if run.get("discovery_id"):
        import chapter_discovery
        discovery = chapter_discovery._load(run["discovery_id"])
    tasks = _ensure_tasks(run, discovery)
    previous = {item["chapter_id"]: item for item in before.get("chapters", [])}
    for record in run.get("chapters", []):
        cid = record["chapter_id"]
        task = tasks[cid]
        if previous.get(cid) == record and cid not in captured:
            continue
        if record["status"] == "failed":
            _state(task, "FAILED_RETRYABLE", record.get("error", ""))
            task["error"] = record.get("error") or "chapter authoring failed"
            task["validation"] = {"success": 0, "error": 0}
        else:
            result = captured.get(cid)
            if result is not None:
                try:
                    pairs = _packages(result)
                    if not pairs:
                        raise ValueError("successful chapter has no package")
                    from knowledge_authoring import builders
                    for _, path in pairs:
                        if not Path(path).is_dir() or builders.validate_package(path)["status"] != "success":
                            raise ValueError(f"invalid package: {path}")
                except (TypeError, ValueError, KeyError) as exc:
                    record["status"] = "failed"
                    record["error"] = str(exc)
                    run["completed_chapters"] = [x for x in run["completed_chapters"] if x != cid]
                    if cid not in run["failed_chapters"]:
                        run["failed_chapters"].append(cid)
                    _state(task, "FAILED_RETRYABLE", str(exc))
                    task["error"] = str(exc)
                    task["package_paths"] = []
                    task["draft_ids"] = []
                    task["validation"] = {"success": 0, "error": 1}
                    continue
                task["draft_ids"] = [draft_id for draft_id, _ in pairs]
                task["package_paths"] = [path for _, path in pairs]
                record["authoring_draft_ids"] = list(task["draft_ids"])
                record["package_paths"] = list(task["package_paths"])
                task["validation"] = {"success": len(pairs), "error": 0}
            _state(task, "SUCCESS")
            task["error"] = ""
        task["checkpoint"] = _checkpoint(run, task)
    # Preserve old unassigned paths, while the chapter task list owns all new paths.
    assigned = {path for task in tasks.values() for path in task["package_paths"]}
    legacy = [path for path in before.get("package_paths", []) if path not in assigned]
    run["package_paths"] = _unique(legacy + [path for task in tasks.values()
                                              if task["state"] == "SUCCESS"
                                              for path in task["package_paths"]])
    if run["status"] == "FAILED" and run["failed_chapters"]:
        run["status"] = "FAILED_RETRYABLE"
    elif not run["failed_chapters"] and run["status"] in ("FAILED_RETRYABLE", "PARTIAL_SUCCESS"):
        run["status"] = "running"
    bp._save(run)


def _mark_running(bp: Any, run: dict) -> None:
    if run["stage"] != "AUTHORING" or not run.get("discovery_id"):
        return
    import chapter_discovery
    discovery = chapter_discovery._load(run["discovery_id"])
    if not discovery or not discovery.get("chapters"):
        return
    tasks = _ensure_tasks(run, discovery)
    recorded = {item["chapter_id"] for item in run["chapters"]}
    failed = set(run["failed_chapters"])
    pending = [item for item in discovery["chapters"]
               if item["chapter_id"] not in recorded and
               item["chapter_id"] not in failed]
    budget = max(0, run["max_chapters"] - len(run["completed_chapters"]))
    chapter = pending[0] if pending and budget else None
    if chapter is None:
        return
    cid = chapter["chapter_id"]
    task = tasks[cid]
    if task["state"] == "PENDING":
        _state(task, "RUNNING")
    run["current_chapter"] = cid
    task["checkpoint"] = _checkpoint(run, task)
    bp._save(run)


def install_fullbook_planner_adapter() -> None:
    import book_pipeline as bp

    if getattr(bp.advance_book_pipeline_impl, "_firefly_fullbook_planner", False):
        return
    original_advance = bp.advance_book_pipeline_impl
    original_session = bp.run_authoring_session

    def capture_session(session, material_text: str, slug: str, trace=None):
        result = original_session(session, material_text, slug, trace)
        captured = _CAPTURE.get()
        if captured is not None:
            captured[slug] = result
        return result

    def advance(pipeline_id: str, max_steps: int = 4, session=None,
                dry_run: bool | None = None) -> dict:
        if max_steps < 1:
            return original_advance(pipeline_id, max_steps, session, dry_run)
        last: dict = {}
        total = 0
        for _ in range(max_steps):
            before = bp._load(pipeline_id)
            if before is None:
                return original_advance(pipeline_id, 1, session, dry_run)
            if before["status"] in ("READY_TO_LEARN", "FAILED"):
                return original_advance(pipeline_id, 1, session, dry_run)
            _mark_running(bp, before)
            before = bp._load(pipeline_id)
            captured: dict[str, dict] = {}
            token = _CAPTURE.set(captured)
            try:
                last = original_advance(pipeline_id, 1, session, dry_run)
            finally:
                _CAPTURE.reset(token)
            run = bp._load(pipeline_id)
            if run is None:
                return last
            _record_step(bp, run, before, captured)
            total += last.get("steps_done", 0)
            if last.get("steps_done", 0) == 0 or run["status"] in ("PAUSED", "READY_TO_LEARN"):
                break
            if run["stage"] == "PACKAGE_VALIDATION" and run["status"] == "FAILED_RETRYABLE":
                break
        run = bp._load(pipeline_id)
        if run is not None and "steps_done" in last:
            last.update(steps_done=total, stage=run["stage"],
                        pipeline_status=run["status"],
                        completed=run["completed_chapters"],
                        failed=run["failed_chapters"],
                        curriculum_id=run["curriculum_id"])
        return last

    def retry(pipeline_id: str, chapter_id: str, session=None) -> dict:
        run = bp._load(pipeline_id)
        if run is None:
            return {"status": "error", "error_code": "unknown_pipeline_id"}
        discovery = None
        if run.get("discovery_id"):
            import chapter_discovery
            discovery = chapter_discovery._load(run["discovery_id"])
        tasks = _ensure_tasks(run, discovery)
        task = tasks.get(chapter_id)
        if task is None or task["state"] != "FAILED_RETRYABLE":
            return {"status": "error", "error_code": "chapter_not_retryable",
                    "chapter_id": chapter_id, "pipeline_id": pipeline_id}
        task["retry_count"] += 1
        _state(task, "RETRYING")
        task["checkpoint"] = _checkpoint(run, task)
        run["failed_chapters"] = [cid for cid in run["failed_chapters"] if cid != chapter_id]
        run["chapters"] = [item for item in run["chapters"] if item["chapter_id"] != chapter_id]
        run["package_paths"] = [path for path in run["package_paths"]
                                if path not in task["package_paths"]]
        task["package_paths"] = []
        task["draft_ids"] = []
        task["validation"] = {"success": 0, "error": 0}
        run["status"] = "running"
        if run["stage"] in ("PACKAGE_VALIDATION", "CURRICULUM", "READY_TO_LEARN"):
            run["stage"] = "AUTHORING"
        bp._save(run)
        sess = session or bp._SESSION_CACHE.get(pipeline_id)
        return advance(pipeline_id, max_steps=2, session=sess)

    advance._firefly_fullbook_planner = True  # type: ignore[attr-defined]
    bp.run_authoring_session = capture_session
    bp.advance_book_pipeline_impl = advance
    bp.retry_book_pipeline_chapter_impl = retry
