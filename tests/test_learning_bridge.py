"""Learning Bridge key tests (Phase 13) + failure scenarios (Phase 16).

Covers the 17 mandated checks: managed import, sha256 dedup, survival after
original deletion, manifest/context/binding schemas, identity stability,
launch command construction, new/resume/review actions, session_id
provenance, resilience to Z Code conversation deletion, course/learner
isolation — plus failure scenarios A-F.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from learning.bridge_state import (  # noqa: E402
    COURSE_READY,
    PROCESSING,
    RESUME,
    RESOURCE_NEW,
    WAITING_REVIEW,
    action_for_state,
    derive_state,
    write_processing_marker,
    clear_processing_marker,
)
from learning.course_binding import BindingError, load_binding, save_binding  # noqa: E402
from learning.identity import IdentityError, ensure_learner_id  # noqa: E402
from learning.launcher import (  # noqa: E402
    CONTEXT_ENV_VAR,
    LaunchError,
    launch_learning_mode,
)
from learning.learning_context import (  # noqa: E402
    CONTEXT_RELATIVE_PATH,
    ContextError,
    build_learning_context,
    context_path,
    read_learning_context,
    write_learning_context,
)
from learning.resource_manager import (  # noqa: E402
    MANIFEST_NAME,
    ResourceManager,
    ResourceImportError,
)
from core.settings_manager import SettingsManager  # noqa: E402

PDF_BYTES = b"%PDF-1.4\n%fake-but-hash-stable\n1 2 3\n"
PDF_BYTES_OTHER = b"%PDF-1.4\n%another-document\n9 8 7\n"


def _pdf(path: Path, content: bytes = PDF_BYTES) -> Path:
    path.write_bytes(content)
    return path


def _settings(tmp_path: Path) -> SettingsManager:
    return SettingsManager(preferences_file=tmp_path / "pet_preferences.json")


def _manager(tmp_path: Path) -> ResourceManager:
    return ResourceManager(tmp_path / "courses")


# ---------------------------------------------------------------------------
# 1-3. Managed import / dedup / survival


def test_01_pdf_managed_import(tmp_path):
    manager = _manager(tmp_path)
    result = manager.import_pdf(_pdf(tmp_path / "讲义.pdf"), "ff-11111111", title="电磁学")
    assert result.course_dir.is_dir()
    assert (result.course_dir / MANIFEST_NAME).is_file()
    assert (result.course_dir / "bridge").is_dir()
    assert (result.course_dir / "workspace" / ".firefly").is_dir()
    manifest = result.manifest
    resource = manifest["resources"][0]
    stored = result.course_dir / resource["relative_path"]
    assert stored.is_file() and stored.read_bytes() == PDF_BYTES
    assert resource["original_name"] == "讲义.pdf"
    assert resource["relative_path"].startswith("source/")
    assert result.resource_id == "res-" + resource["sha256"][:12]


def test_02_sha256_dedup(tmp_path):
    manager = _manager(tmp_path)
    first = manager.import_pdf(_pdf(tmp_path / "讲义.pdf"), "ff-11111111")
    # same content, different filename and location
    copy = tmp_path / "downloads" / "renamed.pdf"
    copy.parent.mkdir()
    copy.write_bytes(PDF_BYTES)
    second = manager.import_pdf(copy, "ff-11111111")
    assert second.deduplicated is True
    assert second.course_id == first.course_id
    assert len(manager.list_courses()) == 1


def test_03_course_survives_original_deletion(tmp_path):
    manager = _manager(tmp_path)
    original = _pdf(tmp_path / "讲义.pdf")
    result = manager.import_pdf(original, "ff-11111111")
    original.unlink()
    manifest = manager.load_manifest(result.course_id)
    resolved = manager.resolve_resource_path(
        result.course_dir, manifest["resources"][0]
    )
    assert resolved.is_file() and resolved.stat().st_size > 0


# ---------------------------------------------------------------------------
# 4. Manifest schema


def test_04_manifest_schema_validation(tmp_path):
    manager = _manager(tmp_path)
    result = manager.import_pdf(_pdf(tmp_path / "a.pdf"), "ff-11111111")
    manifest = json.loads((result.course_dir / MANIFEST_NAME).read_text(encoding="utf-8"))

    broken = dict(manifest)
    del broken["learner_id"]
    with pytest.raises(ResourceImportError):
        manager.validate_manifest(broken)

    broken = dict(manifest, unexpected_field=1)
    with pytest.raises(ResourceImportError):
        manager.validate_manifest(broken)

    broken = dict(manifest, schema_version=2)
    with pytest.raises(ResourceImportError):
        manager.validate_manifest(broken)

    broken = json.loads(json.dumps(manifest))
    broken["resources"][0]["sha256"] = "nothex"
    with pytest.raises(ResourceImportError):
        manager.validate_manifest(broken)

    # relative paths escaping the course dir are unusable at resolve time
    broken = json.loads(json.dumps(manifest))
    broken["resources"][0]["relative_path"] = "../../etc/passwd"
    with pytest.raises(ResourceImportError):
        manager.resolve_resource_path(result.course_dir, broken["resources"][0])


# ---------------------------------------------------------------------------
# 5-6. Identity stability


def test_05_learner_id_stable(tmp_path):
    settings = _settings(tmp_path)
    first = ensure_learner_id(settings)
    assert first.startswith("ff-")
    # same settings object and a fresh instance both return the same id
    assert ensure_learner_id(settings) == first
    assert SettingsManager(preferences_file=settings.preferences_file) and (
        SettingsManager(preferences_file=settings.preferences_file).learning_learner_id
        == first
    )
    # corrupt persisted id must fail loudly, never silently regenerate
    settings.set_learning_learner_id("totally-bogus")
    with pytest.raises(IdentityError):
        ensure_learner_id(settings)


def test_06_course_id_stable(tmp_path):
    manager = _manager(tmp_path)
    first = manager.import_pdf(_pdf(tmp_path / "a.pdf"), "ff-11111111")
    again = manager.import_pdf(_pdf(tmp_path / "b.pdf"), "ff-11111111")
    assert again.course_id == first.course_id
    other = manager.import_pdf(_pdf(tmp_path / "c.pdf", PDF_BYTES_OTHER), "ff-11111111")
    assert other.course_id != first.course_id
    assert other.course_id.startswith("crs-")


# ---------------------------------------------------------------------------
# 7-8. Context schema


def test_07_context_schema_roundtrip(tmp_path):
    manager = _manager(tmp_path)
    course = manager.import_pdf(_pdf(tmp_path / "a.pdf"), "ff-11111111")
    context = build_learning_context(course.course_dir, "new", "ff-11111111")
    assert context["source"] == "firefly"
    assert context["action"] == "new"
    assert Path(context["manifest_path"]) == (course.course_dir / MANIFEST_NAME).resolve()
    written = write_learning_context(course.course_dir, context)
    assert written == context_path(course.course_dir)
    assert CONTEXT_RELATIVE_PATH.as_posix() in written.as_posix()
    assert read_learning_context(written) == context


def test_08_context_excludes_mastery(tmp_path):
    manager = _manager(tmp_path)
    course = manager.import_pdf(_pdf(tmp_path / "a.pdf"), "ff-11111111")
    context = build_learning_context(course.course_dir, "resume", "ff-11111111")
    for field in ("mastery", "progress", "misconception", "quiz"):
        assert field not in json.dumps(context)
    with pytest.raises(ResourceImportError):
        build_learning_context(course.course_dir, "resume", "ff-11111111", manifest=dict(course.manifest, mastery=0.5))
    with pytest.raises(ContextError):
        build_learning_context(course.course_dir, "study", "ff-11111111")  # bad action


# ---------------------------------------------------------------------------
# 9. Binding opaque ids only


def test_09_binding_only_opaque_ids(tmp_path):
    manager = _manager(tmp_path)
    course = manager.import_pdf(_pdf(tmp_path / "a.pdf"), "ff-11111111")
    assert load_binding(course.course_dir) is None
    save_binding(
        course.course_dir,
        course.course_id,
        pipeline_id="bp-abc123",
        curriculum_id="curr-xyz",
    )
    binding = load_binding(course.course_dir)
    assert binding == {
        "course_id": course.course_id,
        "pipeline_id": "bp-abc123",
        "curriculum_id": "curr-xyz",
    }
    raw = json.loads(
        (course.course_dir / "bridge" / "course_binding.json").read_text(encoding="utf-8")
    )
    assert set(raw) <= {"course_id", "pipeline_id", "curriculum_id"}
    with pytest.raises((TypeError, BindingError)):
        save_binding(course.course_dir, course.course_id, progress=0.4)  # type: ignore[arg-type]


def test_existing_zcode_session_is_offered_as_continue_learning(tmp_path):
    manager = _manager(tmp_path)
    course = manager.import_pdf(_pdf(tmp_path / "course.pdf"), "ff-11111111")
    save_binding(course.course_dir, course.course_id, zcode_session_id="sess-existing")

    state = derive_state(course.course_dir)
    assert state == RESUME
    assert action_for_state(state) == "resume"


# ---------------------------------------------------------------------------
# 10-12. Launch command construction + actions


@pytest.fixture()
def fake_zcode_env(tmp_path, monkeypatch):
    from learning import launcher

    cli = tmp_path / "standalone" / "bin" / "zcode.mjs"
    cli.parent.mkdir(parents=True)
    cli.write_text("// fake cli\n", encoding="utf-8")
    agent = cli.parent.parent / "agent"
    agent.mkdir()
    (agent / "zcode.cjs").write_text("// fake agent", encoding="utf-8")
    monkeypatch.setenv("FIREFLY_ZCODE_RUNTIME", str(cli))
    monkeypatch.setenv("FIREFLY_NODE", "node-fake")
    monkeypatch.setattr(launcher, "_ensure_tui_runtime", lambda *args: None)
    return cli


def test_10_launch_command_construction(tmp_path, fake_zcode_env):
    manager = _manager(tmp_path)
    settings = _settings(tmp_path)
    learner = ensure_learner_id(settings)
    course = manager.import_pdf(_pdf(tmp_path / "a.pdf"), learner)

    captured = {}

    def spawner(argv, cwd, env, creationflags, stdout_path=None):
        captured.update(argv=list(argv), cwd=cwd, env=dict(env))
        return 4321

    result = launch_learning_mode(
        course.course_id,
        "new",
        settings=settings,
        courses_root=manager.courses_root,
        dry_run=False,
        spawner=spawner,
    )
    argv = result.plan.argv
    assert "--prompt" in argv and "--cwd" in argv
    assert argv[argv.index("--cwd") + 1] == str(course.course_dir / "workspace")
    assert "firefly-learning" in argv[argv.index("--prompt") + 1]
    assert result.plan.env[CONTEXT_ENV_VAR] == str(result.plan.context_file)
    assert captured["env"][CONTEXT_ENV_VAR] == str(result.plan.context_file)
    assert Path(captured["env"][CONTEXT_ENV_VAR]).is_file()
    assert result.plan.learner_id == learner
    assert result.pid == 4321
    # manifest learner_id matches the settings learner (single identity)
    assert manager.load_manifest(course.course_id)["learner_id"] == learner


def test_11_new_action_flow(tmp_path, fake_zcode_env):
    manager = _manager(tmp_path)
    settings = _settings(tmp_path)
    course = manager.import_pdf(
        _pdf(tmp_path / "a.pdf"), ensure_learner_id(settings)
    )
    assert derive_state(course.course_dir) == RESOURCE_NEW
    assert action_for_state(RESOURCE_NEW) == "new"
    result = launch_learning_mode(
        course.course_id,
        "new",
        settings=settings,
        courses_root=manager.courses_root,
        dry_run=True,
    )
    context = read_learning_context(result.plan.context_file)
    assert context["action"] == "new"
    assert "curriculum_id" not in context and "pipeline_id" not in context
    # task-injection contract: prompt carries skill name, context path,
    # this action, and the anti-reuse rule (never trust chat history)
    prompt = result.plan.argv[result.plan.argv.index("--prompt") + 1]
    assert "firefly-learning" in prompt
    assert str(result.plan.context_file) in prompt
    assert "本次 action：new" in prompt
    assert "不得根据当前 Z Code 对话历史猜测学习状态" in prompt


def test_12_resume_action_flow(tmp_path, fake_zcode_env):
    manager = _manager(tmp_path)
    settings = _settings(tmp_path)
    course = manager.import_pdf(
        _pdf(tmp_path / "a.pdf"), ensure_learner_id(settings)
    )
    save_binding(course.course_dir, course.course_id, curriculum_id="curr-fixed-1")
    learner = ensure_learner_id(settings)
    result = launch_learning_mode(
        course.course_id,
        "resume",
        settings=settings,
        courses_root=manager.courses_root,
        dry_run=True,
    )
    context = read_learning_context(result.plan.context_file)
    assert context["action"] == "resume"
    assert context["curriculum_id"] == "curr-fixed-1"  # opaque echo only
    assert context["learner_id"] == learner


# ---------------------------------------------------------------------------
# 13. Review gate


def test_13_review_gate(tmp_path):
    manager = _manager(tmp_path)
    course = manager.import_pdf(_pdf(tmp_path / "a.pdf"), "ff-11111111")
    save_binding(course.course_dir, course.course_id, pipeline_id="bp-1")
    assert derive_state(course.course_dir) == WAITING_REVIEW
    assert action_for_state(WAITING_REVIEW) == "review"
    skill = (
        PROJECT_DIR / "learning" / "skill_source" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "禁止自动进入正式教学" in skill
    assert "Human Review Gate" in skill
    assert "draft_packages" in skill and "knowledge/" in skill


# ---------------------------------------------------------------------------
# 14. session_id is never minted by model / Firefly / skill


def test_14_session_id_provenance():
    """session_id may only be ECHOED (result envelope), never MINTED.

    Strict AST scan applies to the id-minting surfaces (identity /
    learning_context / course_binding / launcher). The result envelope
    (learning_result.py) and session routing (bridge_session.py) carry
    teach-mcp-returned ids verbatim — that echo is schema-required — so for
    the whole package the decisive rule is: no uuid/secrets minting outside
    the two sanctioned generators (learner_id, task_id)."""
    import ast

    strict_modules = {
        "identity.py",
        "learning_context.py",
        "course_binding.py",
        # launcher.py is intentionally NOT strict anymore: the interactive
        # init run READS the Z Code sessionId from the official --json output
        # and echoes it into binding.zcode_session_id (pure pass-through).
    }
    sanctioned_minters = {
        "identity.py",          # learner_id
        "task_state.py",        # task_id
        "resource_manager.py",  # course_id / resource_id
        "launcher.py",          # per-run task_id (incl. rollover init)
    }
    for py in sorted((PROJECT_DIR / "learning").glob("*.py")):
        source = py.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    assert root not in {"uuid", "secrets"} or py.name in sanctioned_minters, (
                        f"{py.name} 引入了 ID 生成库 {alias.name}"
                    )
            if isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                assert root not in {"uuid", "secrets"} or py.name in sanctioned_minters, (
                    f"{py.name} 引入了 ID 生成库 {node.module}"
                )
        if py.name in strict_modules:
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    assert node.id != "session_id", f"{py.name} 引用 session_id 变量"
                if isinstance(node, ast.Constant) and node.value == "session_id":
                    raise AssertionError(f"{py.name} 出现 session_id 字面量")
                if isinstance(node, ast.keyword) and node.arg == "session_id":
                    raise AssertionError(f"{py.name} 使用 session_id 关键字参数")
    skill = (PROJECT_DIR / "learning" / "skill_source" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "永不生成任何 ID" in skill
    assert "session_id" in skill and "teach-mcp" in skill


# ---------------------------------------------------------------------------
# 15. Z Code conversation deletion does not break resume


def test_15_resume_after_zcode_conversation_deleted(tmp_path, fake_zcode_env):
    manager = _manager(tmp_path)
    settings = _settings(tmp_path)
    course = manager.import_pdf(
        _pdf(tmp_path / "a.pdf"), ensure_learner_id(settings)
    )
    save_binding(course.course_dir, course.course_id, curriculum_id="curr-fixed-1")
    ensure_learner_id(settings)
    first = launch_learning_mode(
        course.course_id,
        "resume",
        settings=settings,
        courses_root=manager.courses_root,
        dry_run=True,
    )
    # nothing the bridge owns refers to a Z Code session id
    context = read_learning_context(first.plan.context_file)
    assert "sess_" not in json.dumps(context)
    assert "session_id" not in json.dumps(context)
    # simulate "delete the Z Code conversation": the workspace is wiped
    import shutil

    shutil.rmtree(course.course_dir / "workspace")
    # a fresh launch regenerates the context; binding and teach-mcp state
    # remain the only carriers of learning state
    second = launch_learning_mode(
        course.course_id,
        "resume",
        settings=settings,
        courses_root=manager.courses_root,
        dry_run=True,
    )
    assert second.plan.context_file.is_file()
    assert load_binding(course.course_dir)["curriculum_id"] == "curr-fixed-1"


# ---------------------------------------------------------------------------
# 16-17. Course / learner isolation


def test_16_courses_do_not_cross(tmp_path):
    manager = _manager(tmp_path)
    a = manager.import_pdf(_pdf(tmp_path / "a.pdf"), "ff-11111111", title="A")
    b = manager.import_pdf(_pdf(tmp_path / "b.pdf", PDF_BYTES_OTHER), "ff-11111111", title="B")
    save_binding(a.course_dir, a.course_id, curriculum_id="curr-A")
    assert derive_state(a.course_dir) == COURSE_READY
    assert derive_state(b.course_dir) == RESOURCE_NEW
    assert load_binding(b.course_dir) is None
    # markers are per-course too
    write_processing_marker(b.course_dir)
    assert derive_state(a.course_dir) == COURSE_READY
    assert derive_state(b.course_dir) == PROCESSING
    clear_processing_marker(b.course_dir)


def test_17_learners_do_not_cross(tmp_path):
    manager = _manager(tmp_path)
    course_a = manager.import_pdf(_pdf(tmp_path / "a.pdf"), "ff-11111111", title="A")
    course_b = manager.import_pdf(
        _pdf(tmp_path / "b.pdf", PDF_BYTES_OTHER), "ff-22222222", title="B"
    )
    save_binding(course_a.course_dir, course_a.course_id, curriculum_id="curr-A")
    ctx_a = build_learning_context(course_a.course_dir, "resume", "ff-11111111")
    ctx_b = build_learning_context(course_b.course_dir, "resume", "ff-22222222")
    assert ctx_a["learner_id"] == "ff-11111111"
    assert ctx_b["learner_id"] == "ff-22222222"

    # open-session peek is learner-scoped: A's session must not flip B
    session_db = tmp_path / "session.db"
    con = sqlite3.connect(session_db)
    con.execute(
        "CREATE TABLE learning_sessions (learner_id TEXT, status TEXT)"
    )
    con.execute("INSERT INTO learning_sessions VALUES ('ff-11111111', 'open')")
    con.commit()
    con.close()
    assert (
        derive_state(course_a.course_dir, learner_id="ff-11111111", session_db=session_db)
        == RESUME
    )
    assert derive_state(
        course_a.course_dir, learner_id="ff-22222222", session_db=session_db
    ) == COURSE_READY


# ---------------------------------------------------------------------------
# Phase 16 failure scenarios


def test_16a_zcode_not_installed_structured_error_and_course_intact(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("FIREFLY_ZCODE_RUNTIME", raising=False)
    monkeypatch.delenv("FIREFLY_NODE", raising=False)
    fake_local = tmp_path / "localappdata"
    fake_local.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(fake_local))
    monkeypatch.setattr("learning.launcher.shutil_which", lambda name: None)
    monkeypatch.setattr(
        "learning.launcher.find_zcode_runtime",
        lambda **_kwargs: (_ for _ in ()).throw(
            LaunchError("ZCODE_TUI_RUNTIME_MISSING", "未安装 Z Code 命令行学习环境")
        ),
    )

    manager = _manager(tmp_path)
    settings = _settings(tmp_path)
    course = manager.import_pdf(
        _pdf(tmp_path / "a.pdf"), ensure_learner_id(settings)
    )
    with pytest.raises(LaunchError) as excinfo:
        launch_learning_mode(
            course.course_id,
            "new",
            settings=settings,
            courses_root=manager.courses_root,
            dry_run=True,
        )
    assert excinfo.value.code == "ZCODE_TUI_RUNTIME_MISSING"
    # course resources are untouched by the failed launch
    assert manager.load_manifest(course.course_id)["course_id"] == course.course_id


def test_16b_teach_mcp_unavailable_policy_is_stop_not_fake():
    skill = (PROJECT_DIR / "learning" / "skill_source" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "学习服务不可用" in skill
    assert "不得伪造" in skill


def test_16c_manifest_corrupted_structured_error(tmp_path):
    manager = _manager(tmp_path)
    course = manager.import_pdf(_pdf(tmp_path / "a.pdf"), "ff-11111111")
    manifest_path = course.course_dir / MANIFEST_NAME
    manifest_path.write_text("{not json", encoding="utf-8")
    from learning._storage import BridgeFileError

    with pytest.raises(BridgeFileError) as excinfo:
        manager.load_manifest(course.course_id)
    assert excinfo.value.code == "FILE_MALFORMED"
    manifest_path.write_text("[]", encoding="utf-8")
    with pytest.raises(ResourceImportError):
        manager.load_manifest(course.course_id)


def test_16d_duplicate_import_handled_by_hash(tmp_path):
    manager = _manager(tmp_path)
    manager.import_pdf(_pdf(tmp_path / "one.pdf"), "ff-11111111")
    before = {s.course_id for s in manager.list_courses()}
    result = manager.import_pdf(_pdf(tmp_path / "two.pdf"), "ff-11111111")
    assert result.deduplicated
    assert {s.course_id for s in manager.list_courses()} == before


def test_16e_unreviewed_draft_blocks_teaching(tmp_path):
    manager = _manager(tmp_path)
    course = manager.import_pdf(_pdf(tmp_path / "a.pdf"), "ff-11111111")
    save_binding(course.course_dir, course.course_id, pipeline_id="bp-1")
    state = derive_state(course.course_dir)
    assert state == WAITING_REVIEW
    assert action_for_state(state) == "review"  # never "resume"/"new"


def test_16f_missing_session_resolves_via_teach_mcp_not_invention(tmp_path):
    manager = _manager(tmp_path)
    settings = _settings(tmp_path)
    course = manager.import_pdf(
        _pdf(tmp_path / "a.pdf"), ensure_learner_id(settings)
    )
    save_binding(course.course_dir, course.course_id, curriculum_id="curr-1")
    # no session db at all -> degraded COURSE_READY, and the launch stays
    # action=resume so the SKILL resolves reality through teach-mcp
    assert derive_state(course.course_dir) == COURSE_READY
    result = launch_learning_mode(
        course.course_id,
        "resume",
        settings=settings,
        courses_root=manager.courses_root,
        dry_run=True,
    )
    assert read_learning_context(result.plan.context_file)["action"] == "resume"


# ---------------------------------------------------------------------------
# Skill discovery protocol contract


def test_skill_context_discovery_contract():
    skill = (PROJECT_DIR / "learning" / "skill_source" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert CONTEXT_ENV_VAR in skill
    assert ".firefly/learning_context.json" in skill
    assert "course_binding.json" in skill
    assert "bridge/processing.json" in skill
    assert "start_book_pipeline" in skill
    assert "resume_learning" in skill
    assert "generate_question" in skill and "record_result" in skill


# ---------------------------------------------------------------------------
# Minimal UI smoke (Phase 11)


def test_bridge_dialog_lists_and_launches(tmp_path, fake_zcode_env, monkeypatch):
    manager = _manager(tmp_path)
    settings = _settings(tmp_path)
    course = manager.import_pdf(
        _pdf(tmp_path / "a.pdf"), ensure_learner_id(settings), title="对话框课程"
    )
    save_binding(course.course_dir, course.course_id, curriculum_id="curr-1")

    from PySide6.QtWidgets import QApplication, QMessageBox

    app = QApplication.instance() or QApplication([])
    from learning.bridge_dialog import LearningBridgeDialog

    monkeypatch.setattr(
        "learning.bridge_dialog.launch_learning_mode",
        lambda *a, **k: _FakeLaunchResult(k),
    )
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    dialog = LearningBridgeDialog(settings=settings, courses_root=manager.courses_root)
    assert dialog._course_list.count() == 1
    dialog._course_list.setCurrentRow(0)
    assert dialog._launch_button.text() == "继续学习"
    dialog._launch_button.click()
    dialog.close()
    app.processEvents()


class _FakeLaunchResult:
    def __init__(self, kwargs):
        from learning.launcher import LaunchPlan
        from pathlib import Path as _P

        self.plan = LaunchPlan(
            course_id="x",
            action="resume",
            learner_id="ff-00000000",
            workspace=_P("."),
            context_file=_P("."),
            argv=[],
            cwd=_P("."),
        )
        self.spawned = True
        self.kwargs = kwargs
