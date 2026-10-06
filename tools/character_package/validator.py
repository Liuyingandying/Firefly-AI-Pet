"""Validate Firefly character directories and .character archives."""

from __future__ import annotations

import argparse
import io
import stat
import sys
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from character_package.schema import (  # type: ignore[import-not-found]
        declared_package_files,
        load_json_document,
        path_safety_error,
        validate_manifest,
    )
else:
    from .schema import declared_package_files, load_json_document, path_safety_error, validate_manifest

MAX_ARCHIVE_ENTRIES = 128
MAX_SINGLE_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_COMPRESSION_RATIO = 250


@dataclass(slots=True)
class ValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    manifest: dict[str, Any] | None = None


def _validate_gif(raw: bytes, name: str, expected_size: tuple[int, int]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.format != "GIF":
                errors.append(f"{name} is not a GIF")
                return errors, warnings
            if image.size != expected_size:
                errors.append(f"{name} size is {image.size[0]}x{image.size[1]}, expected {expected_size[0]}x{expected_size[1]}")
            frames = getattr(image, "n_frames", 1)
            if frames < 1:
                errors.append(f"{name} contains no frames")
            if image.info.get("loop") not in (0, None):
                warnings.append(f"{name} is not configured for infinite looping")
            if "transparency" not in image.info:
                warnings.append(f"{name} does not declare GIF transparency")
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        errors.append(f"{name} is not a readable GIF: {exc}")
    return errors, warnings


def _validate_preview(raw: bytes) -> list[str]:
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.format != "PNG":
                return ["preview.png is not a PNG"]
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        return [f"preview.png is not readable: {exc}"]
    return []


def _validate_documents(files: dict[str, bytes]) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    manifest: dict[str, Any] | None = None

    if "character.json" not in files:
        return ValidationResult(False, ["missing character.json"])
    try:
        loaded = load_json_document(files["character.json"], "character.json")
    except ValueError as exc:
        return ValidationResult(False, [str(exc)])
    errors.extend(validate_manifest(loaded))
    if not isinstance(loaded, dict):
        return ValidationResult(False, errors)
    manifest = loaded

    expected = declared_package_files(manifest)
    actual = set(files)
    for missing in sorted(expected - actual):
        errors.append(f"missing package file: {missing}")
    for extra in sorted(actual - expected):
        errors.append(f"undeclared package file: {extra}")

    metadata = files.get("metadata.json")
    if metadata is not None:
        try:
            metadata_document = load_json_document(metadata, "metadata.json")
            if not isinstance(metadata_document, dict):
                errors.append("metadata.json root must be an object")
        except ValueError as exc:
            errors.append(str(exc))

    preview = files.get("preview.png")
    if preview is not None:
        errors.extend(_validate_preview(preview))

    size = manifest.get("size")
    if isinstance(size, dict) and all(isinstance(size.get(key), int) and not isinstance(size.get(key), bool) for key in ("width", "height")):
        expected_size = (size["width"], size["height"])
        animations = manifest.get("animations")
        if isinstance(animations, dict):
            for path in sorted(set(animations.values())):
                if isinstance(path, str) and path in files:
                    gif_errors, gif_warnings = _validate_gif(files[path], path, expected_size)
                    errors.extend(gif_errors)
                    warnings.extend(gif_warnings)

    return ValidationResult(not errors, errors, warnings, manifest)


def validate_directory(path: str | Path) -> ValidationResult:
    root = Path(path)
    if not root.is_dir():
        return ValidationResult(False, [f"not a directory: {root}"])

    manifest_path = root / "character.json"
    if not manifest_path.is_file():
        return ValidationResult(False, ["missing character.json"])
    try:
        manifest_raw = manifest_path.read_bytes()
        loaded = load_json_document(manifest_raw, "character.json")
    except (OSError, ValueError) as exc:
        return ValidationResult(False, [str(exc)])
    manifest_errors = validate_manifest(loaded)
    if not isinstance(loaded, dict):
        return ValidationResult(False, manifest_errors)

    files: dict[str, bytes] = {"character.json": manifest_raw}
    read_errors: list[str] = []
    for relative in sorted(declared_package_files(loaded) - {"character.json"}):
        safety_error = path_safety_error(relative)
        if safety_error:
            read_errors.append(f"unsafe declared path {relative!r}: {safety_error}")
            continue
        candidate = root.joinpath(*relative.split("/"))
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root.resolve())
            if not resolved.is_file():
                raise OSError("not a regular file")
            files[relative] = resolved.read_bytes()
        except (OSError, ValueError) as exc:
            read_errors.append(f"cannot read {relative}: {exc}")

    result = _validate_documents(files)
    result.errors[:0] = read_errors
    result.valid = not result.errors
    return result


def _zip_security_errors(infos: list[zipfile.ZipInfo]) -> tuple[list[str], list[zipfile.ZipInfo]]:
    errors: list[str] = []
    files: list[zipfile.ZipInfo] = []
    if len(infos) > MAX_ARCHIVE_ENTRIES:
        errors.append(f"archive has too many entries ({len(infos)} > {MAX_ARCHIVE_ENTRIES})")

    total_size = 0
    seen: set[str] = set()
    for info in infos:
        # Python on Windows normalizes backslashes and truncates NUL in
        # filename. Validate the original name too, before that sanitization.
        safety_error = (path_safety_error(info.orig_filename, allow_directory=True)
                        or path_safety_error(info.filename, allow_directory=True))
        if safety_error:
            errors.append(f"unsafe archive path {info.filename!r}: {safety_error}")
            continue
        folded = unicodedata.normalize("NFC", info.filename.rstrip("/")).casefold()
        if folded in seen:
            errors.append(f"duplicate or case-conflicting archive path: {info.filename}")
        seen.add(folded)
        if info.flag_bits & 0x1:
            errors.append(f"encrypted archive entry is not allowed: {info.filename}")
        mode = (info.external_attr >> 16) & 0xFFFF
        if stat.S_ISLNK(mode):
            errors.append(f"symbolic link entry is not allowed: {info.filename}")
        if info.is_dir():
            continue
        files.append(info)
        total_size += info.file_size
        if info.file_size > MAX_SINGLE_FILE_BYTES:
            errors.append(f"archive entry is too large: {info.filename}")
        if info.file_size > 1024 * 1024 and info.file_size / max(info.compress_size, 1) > MAX_COMPRESSION_RATIO:
            errors.append(f"suspicious compression ratio: {info.filename}")
    if total_size > MAX_TOTAL_UNCOMPRESSED_BYTES:
        errors.append("archive uncompressed size exceeds safety limit")
    return errors, files


def validate_archive(path: str | Path) -> ValidationResult:
    archive = Path(path)
    if not archive.is_file():
        return ValidationResult(False, [f"not a file: {archive}"])
    if not zipfile.is_zipfile(archive):
        return ValidationResult(False, ["file is not a ZIP archive"])

    try:
        with zipfile.ZipFile(archive, "r") as zf:
            errors, infos = _zip_security_errors(zf.infolist())
            if errors:
                return ValidationResult(False, errors)
            files: dict[str, bytes] = {}
            for info in infos:
                try:
                    files[info.filename] = zf.read(info)
                except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                    errors.append(f"cannot read {info.filename}: {exc}")
            if errors:
                return ValidationResult(False, errors)
    except (OSError, zipfile.BadZipFile, NotImplementedError) as exc:
        return ValidationResult(False, [f"cannot open archive: {exc}"])
    return _validate_documents(files)


def extract_validated_archive(zf: zipfile.ZipFile, destination: Path) -> None:
    """Shared archive boundary for YAML cards, skins and .character bundles.

    Validate the SAME open ZIP being read, before creating any member. The
    caller owns a fresh staging directory; existing files are never replaced.
    """
    errors, infos = _zip_security_errors(zf.infolist())
    if errors:
        raise ValueError("; ".join(errors))
    root = Path(destination).resolve(strict=True)
    if any(root.iterdir()):
        raise ValueError("archive destination must be an empty staging directory")
    total = 0
    for info in infos:
        target = root.joinpath(*info.filename.split("/"))
        target.resolve().relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with zf.open(info) as source, target.open("xb") as sink:
            while chunk := source.read(1024 * 1024):
                count += len(chunk)
                total += len(chunk)
                if count > min(info.file_size, MAX_SINGLE_FILE_BYTES) or total > MAX_TOTAL_UNCOMPRESSED_BYTES:
                    raise ValueError("archive expanded beyond declared safety limit")
                sink.write(chunk)
        if count != info.file_size:
            raise ValueError("archive entry size mismatch")


def validate_character_package(path: str | Path) -> ValidationResult:
    target = Path(path)
    return validate_directory(target) if target.is_dir() else validate_archive(target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a Firefly .character package or source directory.")
    parser.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    result = validate_character_package(args.path)
    for warning in result.warnings:
        print(f"WARNING: {warning}")
    if result.valid:
        print("Character package valid")
        return 0
    print("Character package invalid", file=sys.stderr)
    for error in result.errors:
        print(f"ERROR: {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
