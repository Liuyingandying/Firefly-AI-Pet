"""Safely extract a Firefly .character archive."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from character_package.schema import declared_package_files, path_safety_error  # type: ignore[import-not-found]
    from character_package.validator import validate_archive, validate_directory, extract_validated_archive  # type: ignore[import-not-found]
else:
    from .schema import declared_package_files, path_safety_error
    from .validator import validate_archive, validate_directory, extract_validated_archive


def unpack_character(package_path: str | Path, output_dir: str | Path) -> Path:
    package = Path(package_path).resolve()
    destination = Path(output_dir).resolve()
    result = validate_archive(package)
    if not result.valid or result.manifest is None:
        raise ValueError("character package is invalid: " + "; ".join(result.errors))
    if destination.exists():
        raise FileExistsError(f"destination already exists: {destination}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent)).resolve()
    expected = declared_package_files(result.manifest)
    try:
        with zipfile.ZipFile(package, "r") as zf:
            by_name = {info.filename: info for info in zf.infolist() if not info.is_dir()}
            if set(by_name) != expected:
                raise ValueError("archive contents changed after validation")
            extract_validated_archive(zf, temporary)

        extracted_result = validate_directory(temporary)
        if not extracted_result.valid:
            raise ValueError("extracted directory failed validation: " + "; ".join(extracted_result.errors))
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Safely unpack a Firefly .character archive.")
    parser.add_argument("package", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        output = unpack_character(args.package, args.output)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
