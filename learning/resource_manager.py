"""Managed course resources for the Learning Bridge (Phase 3 + 4).

The Resource Manager is the only component allowed to generate ``course_id``
and ``resource_id``. It copies imported files into the Firefly data root so a
course never depends on the original download location, and it owns
``manifest.json`` — a pure resource inventory that must never contain
mastery / progress / quiz fields (MEMORY_OWNERSHIP contract, guarded by
tests/test_learning_bridge_memory_guard.py).
"""

from __future__ import annotations

import hashlib
import re
import shutil
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from core.user_paths import get_user_data_paths

from learning._storage import BridgeFileError, atomic_write_json, read_json, utc_now_iso

MANIFEST_NAME = "manifest.json"
SCHEMA_VERSION = 1
MAX_IMPORT_BYTES = 200 * 1024 * 1024
SUPPORTED_TYPES = {".pdf": "pdf"}

# Fields that must never appear in a Firefly-maintained protocol file.
FORBIDDEN_STATE_KEYS = frozenset(
    {"mastery", "misconception_records", "quiz_history", "learning_progress"}
)

_MANIFEST_TOP_KEYS = (
    "schema_version",
    "course_id",
    "title",
    "learner_id",
    "created_at",
    "updated_at",
    "resources",
)
_RESOURCE_KEYS = (
    "resource_id",
    "type",
    "relative_path",
    "original_name",
    "sha256",
    "imported_at",
)

_COURSE_ID_RE = re.compile(r"^crs-[0-9a-f]{12}$")
_RESOURCE_ID_RE = re.compile(r"^res-[0-9a-f]{12}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")
_RELATIVE_PATH_RE = re.compile(r"^source/[^/\\]+\.pdf$")


class ResourceImportError(Exception):
    """Structured import failure; carries a stable machine-readable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


@dataclass
class ImportResult:
    course_id: str
    course_dir: Path
    resource_id: str
    manifest: dict
    deduplicated: bool = False


@dataclass
class CourseSummary:
    course_id: str
    title: str
    learner_id: str
    created_at: str
    resource_count: int
    course_dir: Path
    manifest: dict = field(repr=False, default_factory=dict)


def default_courses_root() -> Path:
    """Reuse the existing Firefly user data root; no second data root."""
    return get_user_data_paths().learning / "courses"


def _safe_filename(name: str) -> str:
    name = unicodedata.normalize("NFC", Path(name).name)
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name).strip().strip(".")
    if not name:
        name = "textbook.pdf"
    if len(name) > 120:
        stem = Path(name).stem[:100] or "textbook"
        name = f"{stem}{Path(name).suffix.lower()}"
    return name


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ResourceManager:
    """Creates, opens, and validates managed courses."""

    def __init__(self, courses_root: Path | str | None = None):
        self.courses_root = (
            Path(courses_root) if courses_root is not None else default_courses_root()
        )

    # -- import (Phase 4) -------------------------------------------------

    def import_pdf(
        self,
        pdf_path: Path | str,
        learner_id: str,
        title: str | None = None,
        *,
        course_id_factory=None,
    ) -> ImportResult:
        source = Path(pdf_path)
        if not source.is_file():
            raise ResourceImportError("FILE_NOT_FOUND", f"文件不存在: {source}")
        if source.suffix.lower() not in SUPPORTED_TYPES:
            raise ResourceImportError(
                "UNSUPPORTED_TYPE",
                f"暂只支持 PDF 导入，收到: {source.suffix or '(无后缀)'}",
            )
        try:
            size = source.stat().st_size
        except OSError as exc:
            raise ResourceImportError("FILE_UNREADABLE", f"文件无法读取: {exc}") from exc
        if size <= 0:
            raise ResourceImportError("FILE_EMPTY", "文件为空")
        if size > MAX_IMPORT_BYTES:
            raise ResourceImportError(
                "FILE_TOO_LARGE",
                f"文件超过导入上限 {MAX_IMPORT_BYTES // (1024 * 1024)}MB",
            )
        learner_id = (learner_id or "").strip()
        if not learner_id:
            raise ResourceImportError("LEARNER_ID_REQUIRED", "learner_id 不能为空")

        digest = sha256_file(source)

        existing = self.find_resource(digest)
        if existing is not None:
            course_dir, manifest = existing
            resource = next(
                r for r in manifest["resources"] if r["sha256"] == digest
            )
            return ImportResult(
                course_id=manifest["course_id"],
                course_dir=course_dir,
                resource_id=resource["resource_id"],
                manifest=manifest,
                deduplicated=True,
            )

        course_id = (course_id_factory or (lambda: "crs-" + _token_hex(6)))()
        if not _COURSE_ID_RE.match(course_id):
            raise ResourceImportError("ID_INVALID", f"course_id 形态非法: {course_id}")

        course_dir = self.courses_root / course_id
        (course_dir / "source").mkdir(parents=True, exist_ok=True)
        (course_dir / "bridge").mkdir(parents=True, exist_ok=True)
        (course_dir / "workspace" / ".firefly").mkdir(parents=True, exist_ok=True)

        safe_name = _safe_filename(source.name)
        target = self._unique_target(course_dir / "source" / safe_name)
        try:
            shutil.copy2(source, target)
        except OSError as exc:
            raise ResourceImportError("COPY_FAILED", f"复制文件失败: {exc}") from exc

        now = utc_now_iso()
        resource_id = "res-" + digest[:12]
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "course_id": course_id,
            "title": (title or "").strip() or Path(safe_name).stem,
            "learner_id": learner_id,
            "created_at": now,
            "updated_at": now,
            "resources": [
                {
                    "resource_id": resource_id,
                    "type": SUPPORTED_TYPES[source.suffix.lower()],
                    "relative_path": f"source/{target.name}",
                    "original_name": source.name,
                    "sha256": digest,
                    "imported_at": now,
                }
            ],
        }
        self.validate_manifest(manifest)
        atomic_write_json(course_dir / MANIFEST_NAME, manifest)
        return ImportResult(
            course_id=course_id,
            course_dir=course_dir,
            resource_id=resource_id,
            manifest=manifest,
        )

    # -- lookup ------------------------------------------------------------

    def course_dir(self, course_id: str) -> Path:
        return self.courses_root / course_id

    def load_manifest(self, course_id: str) -> dict:
        path = self.course_dir(course_id) / MANIFEST_NAME
        data = read_json(path)
        self.validate_manifest(data)
        return data

    def list_courses(self) -> list[CourseSummary]:
        summaries: list[CourseSummary] = []
        if not self.courses_root.is_dir():
            return summaries
        for manifest_path in sorted(self.courses_root.glob(f"*/{MANIFEST_NAME}")):
            try:
                data = read_json(manifest_path)
                self.validate_manifest(data)
            except BridgeFileError:
                continue
            summaries.append(
                CourseSummary(
                    course_id=data["course_id"],
                    title=data["title"],
                    learner_id=data["learner_id"],
                    created_at=data["created_at"],
                    resource_count=len(data["resources"]),
                    course_dir=manifest_path.parent,
                    manifest=data,
                )
            )
        summaries.sort(key=lambda s: s.created_at, reverse=True)
        return summaries

    def find_resource(self, sha256: str) -> tuple[Path, dict] | None:
        """Locate an already-managed copy of ``sha256`` across all courses."""
        for summary in self.list_courses():
            for resource in summary.manifest.get("resources", []):
                if resource.get("sha256") == sha256:
                    return summary.course_dir, summary.manifest
        return None

    def resolve_resource_path(self, course_dir: Path | str, resource: dict) -> Path:
        """Resolve a manifest resource to an absolute path inside the course.

        Original absolute paths must never become runtime dependencies: the
        relative_path is joined onto the course dir and verified to stay
        inside it.
        """
        relative = str(resource.get("relative_path", ""))
        if not _RELATIVE_PATH_RE.match(relative):
            raise ResourceImportError(
                "RESOURCE_PATH_INVALID", f"relative_path 非法: {relative!r}"
            )
        base = Path(course_dir).resolve()
        candidate = (base / relative).resolve()
        if candidate.parent != base / "source" or base not in candidate.parents:
            raise ResourceImportError(
                "RESOURCE_PATH_ESCAPES_COURSE", f"资源路径越界: {relative!r}"
            )
        if not candidate.is_file():
            raise ResourceImportError(
                "RESOURCE_MISSING", f"托管资源缺失（原始文件不可作为依赖）: {candidate}"
            )
        return candidate

    # -- validation ----------------------------------------------------------

    def validate_manifest(self, data: object) -> dict:
        if not isinstance(data, dict):
            raise ResourceImportError("MANIFEST_INVALID", "manifest 必须是 JSON 对象")
        for key in data:
            if key in FORBIDDEN_STATE_KEYS:
                raise ResourceImportError(
                    "MANIFEST_FORBIDDEN_FIELD",
                    f"manifest 出现学习状态权威字段（架构违规）: {key}",
                )
        missing = [key for key in _MANIFEST_TOP_KEYS if key not in data]
        if missing:
            raise ResourceImportError(
                "MANIFEST_INVALID", f"manifest 缺少字段: {', '.join(missing)}"
            )
        extra = [key for key in data if key not in _MANIFEST_TOP_KEYS]
        if extra:
            raise ResourceImportError(
                "MANIFEST_INVALID", f"manifest 出现未知字段: {', '.join(extra)}"
            )
        if data["schema_version"] != SCHEMA_VERSION:
            raise ResourceImportError(
                "MANIFEST_VERSION_UNSUPPORTED",
                f"不支持的 manifest 版本: {data['schema_version']!r}",
            )
        if not _COURSE_ID_RE.match(str(data["course_id"])):
            raise ResourceImportError(
                "MANIFEST_INVALID", f"course_id 形态非法: {data['course_id']!r}"
            )
        if not str(data["title"]).strip():
            raise ResourceImportError("MANIFEST_INVALID", "title 不能为空")
        if not str(data["learner_id"]).strip():
            raise ResourceImportError("MANIFEST_INVALID", "learner_id 不能为空")
        for key in ("created_at", "updated_at"):
            if not _TIMESTAMP_RE.match(str(data[key])):
                raise ResourceImportError(
                    "MANIFEST_INVALID", f"{key} 时间戳格式非法: {data[key]!r}"
                )
        resources = data["resources"]
        if not isinstance(resources, list) or not resources:
            raise ResourceImportError("MANIFEST_INVALID", "resources 必须是非空数组")
        seen_ids: set[str] = set()
        for resource in resources:
            if not isinstance(resource, dict):
                raise ResourceImportError("MANIFEST_INVALID", "resource 必须是对象")
            extra = [key for key in resource if key not in _RESOURCE_KEYS]
            if extra:
                raise ResourceImportError(
                    "MANIFEST_INVALID", f"resource 出现未知字段: {', '.join(extra)}"
                )
            missing = [key for key in _RESOURCE_KEYS if key not in resource]
            if missing:
                raise ResourceImportError(
                    "MANIFEST_INVALID", f"resource 缺少字段: {', '.join(missing)}"
                )
            if not _RESOURCE_ID_RE.match(str(resource["resource_id"])):
                raise ResourceImportError(
                    "MANIFEST_INVALID", f"resource_id 形态非法: {resource['resource_id']!r}"
                )
            if resource["resource_id"] in seen_ids:
                raise ResourceImportError(
                    "MANIFEST_INVALID", f"resource_id 重复: {resource['resource_id']}"
                )
            seen_ids.add(resource["resource_id"])
            if resource["type"] not in set(SUPPORTED_TYPES.values()):
                raise ResourceImportError(
                    "MANIFEST_INVALID", f"resource type 非法: {resource['type']!r}"
                )
            if not _SHA256_RE.match(str(resource["sha256"])):
                raise ResourceImportError(
                    "MANIFEST_INVALID", "sha256 必须是 64 位十六进制"
                )
            if not _RELATIVE_PATH_RE.match(str(resource["relative_path"])):
                raise ResourceImportError(
                    "MANIFEST_INVALID",
                    f"relative_path 非法: {resource['relative_path']!r}",
                )
            if not _TIMESTAMP_RE.match(str(resource["imported_at"])):
                raise ResourceImportError(
                    "MANIFEST_INVALID", f"imported_at 非法: {resource['imported_at']!r}"
                )
        return data

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _unique_target(target: Path) -> Path:
        if not target.exists():
            return target
        stem, suffix = target.stem, target.suffix
        for index in range(1, 1000):
            candidate = target.with_name(f"{stem}({index}){suffix}")
            if not candidate.exists():
                return candidate
        raise ResourceImportError("COPY_FAILED", "目标文件名冲突过多")


def _token_hex(n: int) -> str:
    import secrets

    return secrets.token_hex(n)
