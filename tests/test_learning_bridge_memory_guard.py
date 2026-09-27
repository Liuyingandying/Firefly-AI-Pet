"""Memory Consistency Guard (Learning Bridge Phase 12).

Architecture rule (docs/learning/MEMORY_OWNERSHIP.md, frozen):
    Firefly = read-through display.  teach-mcp = source of truth.

These tests fail if any Firefly bridge module starts *writing* learning-state
fields (mastery / misconception_records / quiz_history / learning_progress)
as its own authoritative data, or if any bridge protocol artifact ever
carries them.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from learning._storage import atomic_write_json  # noqa: E402
from learning.course_binding import (  # noqa: E402
    BindingError,
    save_binding,
)
from learning.learning_context import (  # noqa: E402
    ContextError,
    build_learning_context,
    write_learning_context,
)
from learning.resource_manager import (  # noqa: E402
    MANIFEST_NAME,
    FORBIDDEN_STATE_KEYS,
    ResourceManager,
    ResourceImportError,
)

FORBIDDEN = sorted(FORBIDDEN_STATE_KEYS)
LEARNING_PACKAGE_DIR = PROJECT_DIR / "learning"


def _import_fake_pdf(path: Path, content: bytes = b"%PDF-1.4 guard") -> Path:
    path.write_bytes(content)
    return path


# ---------------------------------------------------------------------------
# 1. Static scan: no bridge module may WRITE a forbidden field.


def _forbidden_write_sites(node: ast.AST) -> list[str]:
    """Find dict-literal keys, keyword args and subscript targets that write
    a forbidden field name. Validators compare against the FORBIDDEN_STATE_KEYS
    constant via membership, so no literal writes appear in clean code."""
    hits: list[str] = []
    for tree in ast.walk(node):
        if isinstance(tree, ast.Dict):
            for key in tree.keys:
                if isinstance(key, ast.Constant) and key.value in FORBIDDEN_STATE_KEYS:
                    hits.append(f"dict literal key {key.value!r}")
        if isinstance(tree, ast.keyword) and tree.arg in FORBIDDEN_STATE_KEYS:
            hits.append(f"keyword argument {tree.arg!r}")
        if (
            isinstance(tree, ast.Subscript)
            and isinstance(tree.slice, ast.Constant)
            and tree.slice.value in FORBIDDEN_STATE_KEYS
            and isinstance(tree.ctx, ast.Store)
        ):
            hits.append(f"subscript store {tree.slice.value!r}")
    return hits


@pytest.mark.parametrize(
    "py_file", sorted(LEARNING_PACKAGE_DIR.glob("*.py")), ids=lambda p: p.name
)
def test_no_bridge_module_writes_learning_state_fields(py_file: Path):
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    hits = _forbidden_write_sites(tree)
    assert not hits, f"{py_file.name} 写入了学习状态权威字段: {hits}"


# ---------------------------------------------------------------------------
# 2. Every artifact the bridge writes stays clean.


def _make_course(tmp_path: Path) -> tuple[ResourceManager, Path]:
    manager = ResourceManager(tmp_path / "courses")
    pdf = _import_fake_pdf(tmp_path / "book.pdf")
    result = manager.import_pdf(pdf, "ff-guard01", title="Guard 课程")
    return manager, result.course_dir


def test_manifest_never_contains_forbidden_fields(tmp_path: Path):
    manager, course_dir = _make_course(tmp_path)
    import json

    manifest = json.loads((course_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    for field in FORBIDDEN:
        assert field not in manifest
    # and the validator rejects an injected one
    manifest["mastery"] = 0.75
    with pytest.raises(ResourceImportError) as excinfo:
        manager.validate_manifest(manifest)
    assert excinfo.value.code == "MANIFEST_FORBIDDEN_FIELD"


def test_context_never_contains_forbidden_fields(tmp_path: Path):
    _manager, course_dir = _make_course(tmp_path)
    context = build_learning_context(course_dir, "resume", "ff-guard01")
    for field in FORBIDDEN:
        assert field not in context
    context["learning_progress"] = {"chapter": 3}
    with pytest.raises(ContextError) as excinfo:
        write_learning_context(course_dir, context)
    assert excinfo.value.code == "CONTEXT_FORBIDDEN_FIELD"


def test_binding_rejects_forbidden_fields(tmp_path: Path):
    _manager, course_dir = _make_course(tmp_path)
    save_binding(course_dir, "crs-000000000000", pipeline_id="bp-ok")
    # forbidden field can't even be expressed as a kwarg (structural refusal)
    with pytest.raises((BindingError, TypeError)):
        save_binding(course_dir, "crs-000000000000", mastery="0.75")  # type: ignore[arg-type]
    # unknown keys are structurally impossible via save_binding; a hand-edited
    # file must fail on load too
    atomic_write_json(
        course_dir / "bridge" / "course_binding.json",
        {"course_id": "crs-000000000000", "misconception_records": []},
    )
    from learning.course_binding import load_binding

    with pytest.raises(BindingError) as excinfo:
        load_binding(course_dir)
    assert excinfo.value.code == "BINDING_FORBIDDEN_FIELD"


# ---------------------------------------------------------------------------
# 3. Settings: the only learning keys Firefly persists are ids/preferences.


def test_settings_only_persist_ids_and_preferences(tmp_path: Path):
    from core.settings_manager import SettingsManager

    settings = SettingsManager(preferences_file=tmp_path / "prefs.json")
    settings.set_learning_learner_id("ff-guard01")
    settings.set_learning_last_course_id("crs-000000000000")
    import json

    payload = json.loads((tmp_path / "prefs.json").read_text(encoding="utf-8"))
    learning_keys = {k for k in payload if k.startswith("learning.")}
    assert learning_keys <= {"learning.learner_id", "learning.last_course_id"}
    for key in learning_keys:
        for field in FORBIDDEN:
            assert field not in key


# ---------------------------------------------------------------------------
# 4. The frozen ownership contract doc still forbids what it forbids.


def test_ownership_contract_lists_all_forbidden_fields():
    text = (PROJECT_DIR / "docs" / "learning" / "MEMORY_OWNERSHIP.md").read_text(
        encoding="utf-8"
    )
    for field in FORBIDDEN:
        assert field in text, f"MEMORY_OWNERSHIP.md 未声明禁止字段 {field}"
    assert "source of truth" in text
