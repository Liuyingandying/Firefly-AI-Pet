"""Phase 9D.2 — ArtifactStore tests.

Covers the 22 required scenarios: artifact root creation, per-workflow
directories, PLAN write/read, UTF-8 Chinese, emoji, multiline, >1000 chars
preserved whole, atomic write (no partial/tmp leftovers), ArtifactRef producer
+ kind correctness, list_artifacts, remove_workflow, removing one workflow never
affects another, path traversal rejection (../../ , ..\\ , absolute Windows,
UNC, absolute POSIX), malformed metadata tolerance, unknown metadata field
tolerance, and no session / API credential fields anywhere in the index.

Pure storage tests: no Qt UI, no agents, no online calls.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from core.artifact_store import ArtifactError, ArtifactPathError, ArtifactStore
from core.workflow_models import ArtifactKind, ArtifactRef

W = "wf-test-1"


def _store(root: Path) -> ArtifactStore:
    return ArtifactStore(root / "runtime" / "artifacts")


# -- 1-4. root / layout / write / read -------------------------------------

def test_artifact_root_created(root: Path) -> None:
    store = _store(root)
    ref = store.write_text(W, ArtifactKind.PLAN, "step_1", "plan body")
    assert store.root.exists()
    assert store.root.is_dir()


def test_workflow_isolated_directory(root: Path) -> None:
    store = _store(root)
    store.write_text("wf-a", ArtifactKind.PLAN, "step_1", "a")
    store.write_text("wf-b", ArtifactKind.PLAN, "step_1", "b")
    assert (store.root / "wf-a" / "plan.md").exists()
    assert (store.root / "wf-b" / "plan.md").exists()
    assert not (store.root / "wf-a" / "b").exists()


def test_write_plan_creates_file(root: Path) -> None:
    store = _store(root)
    ref = store.write_text(W, ArtifactKind.PLAN, "step_1", "the plan")
    assert ref.kind == ArtifactKind.PLAN
    assert ref.producer_step_id == "step_1"
    target = store.resolve_path(ref)
    assert target.name == "plan.md"
    assert target.exists()


def test_read_plan(root: Path) -> None:
    store = _store(root)
    ref = store.write_text(W, ArtifactKind.PLAN, "step_1", "the plan text")
    assert store.read_text(ref) == "the plan text"


# -- 5-8. content fidelity --------------------------------------------------

def test_utf8_chinese(root: Path) -> None:
    store = _store(root)
    text = "实现方案：重构认证模块，保持向后兼容。"
    ref = store.write_text(W, ArtifactKind.PLAN, "step_1", text)
    assert store.read_text(ref) == text


def test_emoji(root: Path) -> None:
    store = _store(root)
    text = "Plan: add greeting \U0001f44b\U0001f3c1 done."
    ref = store.write_text(W, ArtifactKind.PLAN, "step_1", text)
    assert store.read_text(ref) == text


def test_multiline(root: Path) -> None:
    store = _store(root)
    text = "# Goal\nline one\n\n- item\n- item2\n"
    ref = store.write_text(W, ArtifactKind.PLAN, "step_1", text)
    assert store.read_text(ref) == text


def test_over_1000_chars_whole(root: Path) -> None:
    store = _store(root)
    text = "".join(f"plan line {i:04d}\n" for i in range(120))
    assert len(text) > 1000
    ref = store.write_text(W, ArtifactKind.PLAN, "step_1", text)
    assert store.read_text(ref) == text


# -- 9. atomic write --------------------------------------------------------

def test_atomic_write_no_leftover_tmp(root: Path) -> None:
    store = _store(root)
    ref = store.write_text(W, ArtifactKind.PLAN, "step_1", "complete plan body")
    target = store.resolve_path(ref)
    assert target.read_text(encoding="utf-8") == "complete plan body"
    assert not target.with_name(target.name + ".tmp").exists()
    assert not (target.parent / "artifacts.json.tmp").exists()
    # a leftover .tmp from a crashed writer must not confuse reads or indexing
    target.with_name(target.name + ".tmp").write_text("{broken", encoding="utf-8")
    assert store.read_text(ref) == "complete plan body"


# -- 10-11. ArtifactRef correctness -----------------------------------------

def test_artifactref_producer_correct(root: Path) -> None:
    store = _store(root)
    ref = store.write_text(W, ArtifactKind.PLAN, "step_1", "x")
    assert ref.producer_step_id == "step_1"


def test_artifactref_kind_correct(root: Path) -> None:
    store = _store(root)
    ref = store.write_text(W, ArtifactKind.PLAN, "step_1", "x")
    assert ref.kind == ArtifactKind.PLAN
    assert ref.kind.value == "plan"
    assert ref.artifact_id
    assert ref.created_at > 0
    assert ref.path.endswith("plan.md")


# -- 12-14. listing / removal -----------------------------------------------

def test_list_artifacts(root: Path) -> None:
    store = _store(root)
    ref1 = store.write_text(W, ArtifactKind.PLAN, "step_1", "p1")
    refs = store.list_artifacts(W)
    assert len(refs) == 1
    assert refs[0].artifact_id == ref1.artifact_id
    assert refs[0].kind == ArtifactKind.PLAN
    assert store.read_text(refs[0]) == "p1"


def test_remove_workflow(root: Path) -> None:
    store = _store(root)
    store.write_text(W, ArtifactKind.PLAN, "step_1", "p")
    store.remove_workflow(W)
    assert not (store.root / W).exists()
    assert store.list_artifacts(W) == []


def test_remove_one_keeps_another(root: Path) -> None:
    store = _store(root)
    store.write_text("wf-a", ArtifactKind.PLAN, "step_1", "a")
    ref_b = store.write_text("wf-b", ArtifactKind.PLAN, "step_1", "b")
    store.remove_workflow("wf-a")
    assert not (store.root / "wf-a").exists()
    assert (store.root / "wf-b" / "plan.md").exists()
    assert store.read_text(ref_b) == "b"


# -- 15-19. path traversal / absolute / UNC / POSIX -------------------------

def _forged_ref(path: str) -> ArtifactRef:
    base = ArtifactRef(
        artifact_id="a1",
        kind=ArtifactKind.PLAN,
        producer_step_id="step_1",
        path="wf/plan.md",
        created_at=1,
    )
    return dataclasses.replace(base, path=path)


def test_dotdot_slash_rejected(root: Path) -> None:
    store = _store(root)
    try:
        store.write_text("../../evil.txt", ArtifactKind.PLAN, "step_1", "x")
    except ArtifactPathError:
        pass
    else:
        raise AssertionError("../../ traversal must be rejected")
    for fn in (store.read_text, store.resolve_path):
        try:
            fn(_forged_ref("../../evil.txt"))
        except ArtifactPathError:
            pass
        else:
            raise AssertionError("read of ../../ must be rejected")
    assert store.exists(_forged_ref("../../evil.txt")) is False


def test_dotdot_backslash_rejected(root: Path) -> None:
    store = _store(root)
    for workflow in ("..\\..\\evil.txt", "..\\evil"):
        try:
            store.write_text(workflow, ArtifactKind.PLAN, "step_1", "x")
        except ArtifactPathError:
            continue
        raise AssertionError(f"Windows traversal {workflow!r} must be rejected")
    try:
        store.read_text(_forged_ref("..\\..\\evil.txt"))
    except ArtifactPathError:
        pass
    else:
        raise AssertionError("read of ..\\..\\ must be rejected")


def test_absolute_windows_path_rejected(root: Path) -> None:
    store = _store(root)
    try:
        store.write_text("C:\\evil.txt", ArtifactKind.PLAN, "step_1", "x")
    except ArtifactPathError:
        pass
    else:
        raise AssertionError("absolute Windows workflow id must be rejected")
    try:
        store.read_text(_forged_ref("C:\\evil.txt"))
    except ArtifactPathError:
        pass
    else:
        raise AssertionError("read of C:\\evil.txt must be rejected")


def test_unc_path_rejected(root: Path) -> None:
    store = _store(root)
    try:
        store.write_text("\\\\server\\share\\evil", ArtifactKind.PLAN, "step_1", "x")
    except ArtifactPathError:
        pass
    else:
        raise AssertionError("UNC workflow id must be rejected")
    try:
        store.read_text(_forged_ref("\\\\server\\share\\evil.txt"))
    except ArtifactPathError:
        pass
    else:
        raise AssertionError("read of a UNC path must be rejected")


def test_absolute_posix_path_rejected(root: Path) -> None:
    store = _store(root)
    try:
        store.write_text("/foo/bar", ArtifactKind.PLAN, "step_1", "x")
    except ArtifactPathError:
        pass
    else:
        raise AssertionError("absolute POSIX workflow id must be rejected")
    try:
        store.read_text(_forged_ref("/foo/bar"))
    except ArtifactPathError:
        pass
    else:
        raise AssertionError("read of an absolute POSIX path must be rejected")


# -- 20-21. metadata tolerance ----------------------------------------------

def test_malformed_metadata_tolerated(root: Path) -> None:
    store = _store(root)
    meta = store.root / W / "artifacts.json"
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text("{ this is not json !!!", encoding="utf-8")
    assert store.list_artifacts(W) == []
    ref = store.write_text(W, ArtifactKind.PLAN, "step_1", "ok")
    # a corrupt index must not break later reads
    assert store.read_text(ref) == "ok"
    assert len(store.list_artifacts(W)) == 1
    # a malformed single record is skipped, others survive
    payload = {
        "version": 1,
        "artifacts": [
            {"artifact_id": "good", "kind": "plan", "producer_step_id": "step_1", "path": f"{W}/plan.md", "created_at": 1},
            {"kind": "plan"},  # missing required fields
            "garbage",
            42,
        ],
    }
    meta.write_text(json.dumps(payload), encoding="utf-8")
    refs = store.list_artifacts(W)
    assert len(refs) == 1
    assert refs[0].artifact_id == "good"


def test_unknown_metadata_field_tolerated(root: Path) -> None:
    store = _store(root)
    meta = store.root / W / "artifacts.json"
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(
        json.dumps(
            {
                "version": 99,  # future version is not guessed
                "artifacts": [
                    {
                        "artifact_id": "a1",
                        "kind": "plan",
                        "producer_step_id": "step_1",
                        "path": f"{W}/plan.md",
                        "created_at": 1,
                        "some_future_field": {"nested": [1, 2]},
                        "other": "x",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    refs = store.list_artifacts(W)
    assert len(refs) == 1
    assert refs[0].artifact_id == "a1"


# -- 22. no credential / session fields -------------------------------------

def test_no_session_or_api_fields(root: Path) -> None:
    store = _store(root)
    store.write_text(W, ArtifactKind.PLAN, "step_1", "the plan content")
    index = (store.root / W / "artifacts.json").read_text(encoding="utf-8")
    for forbidden in ("session", "api_key", "apikey", "token", "transcript", "prompt", "env", "credential", "native_session"):
        assert forbidden not in index.lower(), f"index must not contain {forbidden!r}"
    # the plan file is the explicit artifact content, nothing else
    assert (store.root / W / "plan.md").read_text(encoding="utf-8") == "the plan content"


# -- bonus: identifier / kind guards ----------------------------------------

def test_bad_kind_rejected(root: Path) -> None:
    store = _store(root)
    try:
        store.write_text(W, "not-a-kind", "step_1", "x")
    except ArtifactError:
        return
    raise AssertionError("unknown kind must be rejected")


def test_empty_or_none_text_rejected(root: Path) -> None:
    store = _store(root)
    try:
        store.write_text(W, ArtifactKind.PLAN, "step_1", None)
    except ArtifactError:
        return
    raise AssertionError("non-str text must be rejected")


def _run(root: Path) -> None:
    test_artifact_root_created(root)
    test_workflow_isolated_directory(root)
    test_write_plan_creates_file(root)
    test_read_plan(root)
    test_utf8_chinese(root)
    test_emoji(root)
    test_multiline(root)
    test_over_1000_chars_whole(root)
    test_atomic_write_no_leftover_tmp(root)
    test_artifactref_producer_correct(root)
    test_artifactref_kind_correct(root)
    test_list_artifacts(root)
    test_remove_workflow(root)
    test_remove_one_keeps_another(root)
    test_dotdot_slash_rejected(root)
    test_dotdot_backslash_rejected(root)
    test_absolute_windows_path_rejected(root)
    test_unc_path_rejected(root)
    test_absolute_posix_path_rejected(root)
    test_malformed_metadata_tolerated(root)
    test_unknown_metadata_field_tolerated(root)
    test_no_session_or_api_fields(root)
    test_bad_kind_rejected(root)
    test_empty_or_none_text_rejected(root)
    print("Phase 9D.2 artifact store tests passed.")


def main() -> None:
    tests = [
        test_artifact_root_created,
        test_workflow_isolated_directory,
        test_write_plan_creates_file,
        test_read_plan,
        test_utf8_chinese,
        test_emoji,
        test_multiline,
        test_over_1000_chars_whole,
        test_atomic_write_no_leftover_tmp,
        test_artifactref_producer_correct,
        test_artifactref_kind_correct,
        test_list_artifacts,
        test_remove_workflow,
        test_remove_one_keeps_another,
        test_dotdot_slash_rejected,
        test_dotdot_backslash_rejected,
        test_absolute_windows_path_rejected,
        test_unc_path_rejected,
        test_absolute_posix_path_rejected,
        test_malformed_metadata_tolerated,
        test_unknown_metadata_field_tolerated,
        test_no_session_or_api_fields,
        test_bad_kind_rejected,
        test_empty_or_none_text_rejected,
    ]
    for fn in tests:
        with tempfile.TemporaryDirectory() as td:
            fn(Path(td))
    print(f"Phase 9D.2 artifact store tests passed ({len(tests)} tests).")


if __name__ == "__main__":
    main()
