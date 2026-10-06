"""Create deterministic Firefly .character archives."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import zipfile
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from character_package.schema import declared_package_files  # type: ignore[import-not-found]
    from character_package.validator import validate_archive, validate_directory  # type: ignore[import-not-found]
else:
    from .schema import declared_package_files
    from .validator import validate_archive, validate_directory

FIXED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def pack_character(input_dir: str | Path, output_path: str | Path) -> Path:
    source = Path(input_dir).resolve()
    output = Path(output_path).resolve()
    result = validate_directory(source)
    if not result.valid or result.manifest is None:
        raise ValueError("source directory is invalid: " + "; ".join(result.errors))
    if output.suffix.lower() != ".character":
        raise ValueError("output filename must end with .character")

    output.parent.mkdir(parents=True, exist_ok=True)
    files = sorted(declared_package_files(result.manifest), key=lambda value: (value.casefold(), value))
    fd, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            for relative in files:
                data = source.joinpath(*relative.split("/")).read_bytes()
                info = zipfile.ZipInfo(relative, FIXED_ZIP_TIMESTAMP)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                zf.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        archive_result = validate_archive(temporary)
        if not archive_result.valid:
            raise ValueError("created archive failed validation: " + "; ".join(archive_result.errors))
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pack a character directory into a deterministic .character archive.")
    parser.add_argument("--input", required=True, type=Path, dest="input_dir")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        output = pack_character(args.input_dir, args.output)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
