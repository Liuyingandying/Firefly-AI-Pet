"""Learning task state (Return Channel v0.1, Phase 5).

``<course>/bridge/task.json`` holds the reference to the CURRENT headless
run — task_id, lifecycle status, pid. It is a run reference for UI purposes,
never a learning-state store.
"""

from __future__ import annotations

from pathlib import Path

from learning._storage import atomic_write_json, read_json, utc_now_iso

TASK_RELATIVE_PATH = Path("bridge") / "task.json"
STATUSES = ("pending", "running", "completed", "failed")


def new_task_id() -> str:
    import secrets

    return "task-" + secrets.token_hex(5)


def task_path(course_dir: Path | str) -> Path:
    return Path(course_dir) / TASK_RELATIVE_PATH


def write_task(
    course_dir: Path | str,
    *,
    task_id: str,
    status: str,
    action: str,
    pid: int | None = None,
    created_at: str | None = None,
) -> Path:
    if status not in STATUSES:
        raise ValueError(f"task status 非法: {status!r}")
    return atomic_write_json(
        task_path(course_dir),
        {
            "task_id": task_id,
            "status": status,
            "action": action,
            "pid": pid,
            "created_at": created_at or utc_now_iso(),
        },
    )


def read_task(course_dir: Path | str) -> dict | None:
    path = task_path(course_dir)
    if not path.is_file():
        return None
    try:
        data = read_json(path)
    except Exception:  # noqa: BLE001 — task state is best-effort for UI
        return None
    return data if isinstance(data, dict) else None
