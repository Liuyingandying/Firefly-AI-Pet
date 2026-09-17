"""Safe artifact persistence for workflow steps (Phase 9D.2).

ArtifactStore owns only the safe on-disk layout under ``runtime/artifacts/``:
one directory per workflow, one file per artifact, plus a small ``artifacts.json``
metadata index per workflow. It is Qt-free, provider-free, and knows nothing
about agents, sessions, or execution. Every identifier that can become a path
segment is validated, and every final resolved path is re-checked to stay
inside the artifact root, so ``..``, absolute paths, drive prefixes, and UNC
paths cannot escape the root.

Writes are atomic (temp file + ``os.replace``) and UTF-8; the returned
:class:`ArtifactRef` is handed back only after the file is fully written, so a
half-written plan can never be attached. Metadata is deliberately tiny and
non-sensitive: no API keys, env, session ids, AgentEvents, or transcripts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path

from core.workflow_models import ArtifactKind, ArtifactRef, make_artifact_ref

from core.user_paths import get_user_data_paths

DEFAULT_ARTIFACT_ROOT = get_user_data_paths().runtime / "artifacts"

# Conservative identifier rule: a single path segment, no separators, no
# leading dot (so ".", "..", ".hidden" are rejected), no drive characters.
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ArtifactError(Exception):
    """Base for artifact storage failures."""


class ArtifactPathError(ArtifactError):
    """A path/identifier would escape the artifact root."""


def is_safe_identifier(value: str) -> bool:
    """True only for values safe to use as one path segment under the root."""
    return isinstance(value, str) and bool(_SAFE_IDENTIFIER.fullmatch(value))


class ArtifactStore:
    def __init__(self, root: Path | str = DEFAULT_ARTIFACT_ROOT) -> None:
        self.root = Path(root).expanduser()

    # -- identifiers / path safety ----------------------------------------

    @staticmethod
    def _check_identifier(value, field: str) -> str:
        if not is_safe_identifier(value):
            raise ArtifactPathError(f"unsafe {field}: {value!r}")
        return value

    def _resolve_inside(self, candidate: Path | str) -> Path:
        """Resolve a relative or absolute candidate and prove it is inside root."""
        root = self.root.resolve()
        p = Path(str(candidate))
        if not p.is_absolute():
            p = root / p
        resolved = p.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            raise ArtifactPathError(f"path escapes artifact root: {candidate!r}")
        return resolved

    def _workflow_dir(self, workflow_id: str) -> Path:
        workflow_id = self._check_identifier(workflow_id, "workflow_id")
        resolved = self._resolve_inside(workflow_id)
        if resolved.is_file():
            raise ArtifactPathError(f"workflow path is a file: {workflow_id!r}")
        return resolved

    # -- write -------------------------------------------------------------

    def write_text(
        self,
        workflow_id: str,
        kind: ArtifactKind | str,
        producer_step_id: str,
        text: str,
    ) -> ArtifactRef:
        """Atomically write ``text`` as a workflow artifact and index it.

        Returns an ArtifactRef only after the file is completely on disk. The
        plan.md file is the explicit artifact; nothing else is logged here.
        """
        workflow_id = self._check_identifier(workflow_id, "workflow_id")
        try:
            kind_enum = kind if isinstance(kind, ArtifactKind) else ArtifactKind(kind)
        except ValueError:
            raise ArtifactError(f"unknown artifact kind: {kind!r}")
        producer_step_id = self._check_identifier(producer_step_id, "producer_step_id")
        if not isinstance(text, str):
            raise ArtifactError("artifact text must be a str")

        directory = self._workflow_dir(workflow_id)
        directory.mkdir(parents=True, exist_ok=True)
        filename = f"{kind_enum.value}.md"
        target = self._resolve_inside(directory / filename)
        temporary = target.with_name(target.name + ".tmp")
        try:
            temporary.write_text(text, encoding="utf-8")
            os.replace(temporary, target)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

        size = target.stat().st_size
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        relative_path = f"{workflow_id}/{filename}"
        ref = make_artifact_ref(
            kind=kind_enum,
            producer_step_id=producer_step_id,
            path=relative_path,
            metadata={"size": size, "sha256": digest},
        )
        self._record(workflow_id, ref)
        return ref

    # -- read --------------------------------------------------------------

    def read_text(self, ref: ArtifactRef) -> str:
        if not isinstance(ref, ArtifactRef):
            raise ArtifactError("read_text requires an ArtifactRef")
        target = self._resolve_inside(ref.path)
        if not target.is_file():
            raise ArtifactError(f"artifact file not found: {ref.path!r}")
        return target.read_text(encoding="utf-8")

    def exists(self, ref: ArtifactRef) -> bool:
        if not isinstance(ref, ArtifactRef):
            return False
        try:
            target = self._resolve_inside(ref.path)
        except ArtifactPathError:
            return False
        return target.is_file()

    def resolve_path(self, ref: ArtifactRef) -> Path:
        if not isinstance(ref, ArtifactRef):
            raise ArtifactError("resolve_path requires an ArtifactRef")
        return self._resolve_inside(ref.path)

    # -- listing / removal -------------------------------------------------

    def list_artifacts(self, workflow_id: str) -> list[ArtifactRef]:
        workflow_id = self._check_identifier(workflow_id, "workflow_id")
        out: list[ArtifactRef] = []
        for record in self._read_metadata(workflow_id):
            ref = self._ref_from_record(record)
            if ref is not None:
                out.append(ref)
        return out

    def remove_workflow(self, workflow_id: str) -> None:
        workflow_id = self._check_identifier(workflow_id, "workflow_id")
        target = self._resolve_inside(workflow_id)
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)

    def remove_artifact(self, ref: ArtifactRef) -> None:
        """Best-effort remove one artifact file and its metadata index record.

        Used by the Implement completion transaction (9D.4): if writing the
        second Step-2 artifact fails, the first file is rolled back before it
        is ever attached to the workflow, so a partial write can never leave a
        stray half-attached artifact.
        """
        if not isinstance(ref, ArtifactRef):
            return
        try:
            target = self._resolve_inside(ref.path)
        except ArtifactPathError:
            return
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        parts = Path(ref.path).parts
        if not parts:
            return
        workflow_id = parts[0]
        try:
            records = self._read_metadata(workflow_id)
        except ArtifactError:
            return
        remaining = [r for r in records if r.get("artifact_id") != ref.artifact_id]
        if len(remaining) != len(records):
            try:
                self._write_metadata(workflow_id, remaining)
            except OSError:
                pass

    # -- metadata index ----------------------------------------------------

    def _metadata_path(self, workflow_id: str) -> Path:
        workflow_id = self._check_identifier(workflow_id, "workflow_id")
        return self._resolve_inside(Path(workflow_id) / "artifacts.json")

    def _record(self, workflow_id: str, ref: ArtifactRef) -> None:
        records = self._read_metadata(workflow_id)
        records.append(
            {
                "artifact_id": ref.artifact_id,
                "kind": ref.kind.value,
                "producer_step_id": ref.producer_step_id,
                "path": ref.path,
                "created_at": ref.created_at,
            }
        )
        if ref.metadata:
            for key in ("size", "sha256"):
                if key in ref.metadata:
                    records[-1][key] = ref.metadata[key]
        self._write_metadata(workflow_id, records)

    def _write_metadata(self, workflow_id: str, records: list[dict]) -> None:
        payload = {"version": 1, "artifacts": records}
        target = self._metadata_path(workflow_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        try:
            temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, target)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _read_metadata(self, workflow_id: str) -> list[dict]:
        """Return valid index records; malformed/corrupt index degrades to []."""
        try:
            path = self._metadata_path(workflow_id)
        except ArtifactPathError:
            return []
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return []
        try:
            data = json.loads(raw)
        except ValueError:
            return []
        if not isinstance(data, dict):
            return []
        artifacts = data.get("artifacts")
        if not isinstance(artifacts, list):
            return []
        return [a for a in artifacts if isinstance(a, dict)]

    @staticmethod
    def _ref_from_record(record: dict) -> ArtifactRef | None:
        artifact_id = record.get("artifact_id")
        kind = record.get("kind")
        producer = record.get("producer_step_id")
        path = record.get("path")
        created_at = record.get("created_at")
        if not all(
            isinstance(v, str) and v
            for v in (artifact_id, kind, producer, path)
        ):
            return None
        if not isinstance(created_at, int):
            return None
        try:
            kind_enum = ArtifactKind(kind)
        except ValueError:
            return None
        metadata: dict | None = None
        size = record.get("size")
        digest = record.get("sha256")
        if isinstance(size, int) or isinstance(digest, str):
            metadata = {}
            if isinstance(size, int):
                metadata["size"] = size
            if isinstance(digest, str):
                metadata["sha256"] = digest
        return ArtifactRef(
            artifact_id=artifact_id,
            kind=kind_enum,
            producer_step_id=producer,
            path=path,
            created_at=created_at,
            metadata=metadata,
        )
