from __future__ import annotations

import hashlib
import json
import stat
import sys
import zipfile
from pathlib import Path

import pytest
from PIL import Image

PACKAGE_DIR = Path(__file__).resolve().parents[1]
TOOLS_DIR = PACKAGE_DIR.parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from character_package.export_character import export_character
from character_package.packer import pack_character
from character_package.schema import declared_package_files
from character_package.unpacker import unpack_character
from character_package.validator import validate_archive, validate_directory


def _write_gif(path: Path, size: tuple[int, int] = (192, 208), colors: tuple[str, str] = ("red", "blue")) -> None:
    frames = []
    for color in colors:
        frame = Image.new("RGBA", size, (0, 0, 0, 0))
        opaque = Image.new("RGBA", (size[0] // 2, size[1] // 2), color)
        frame.alpha_composite(opaque, (size[0] // 4, size[1] // 4))
        frames.append(frame)
    frames[0].save(
        path,
        save_all=True,
        append_images=frames[1:],
        duration=100,
        loop=0,
        disposal=2,
        transparency=0,
        format="GIF",
    )


def _make_character(root: Path, *, size: tuple[int, int] = (192, 208)) -> Path:
    (root / "animations").mkdir(parents=True)
    _write_gif(root / "animations" / "idle.gif", size=size)
    _write_gif(root / "animations" / "happy.gif", size=size, colors=("green", "yellow"))
    Image.new("RGBA", size, (255, 0, 0, 128)).save(root / "preview.png")
    manifest = {
        "schema_version": "1.0",
        "character_id": "test_pet",
        "display_name": "Test Pet",
        "author": "tests",
        "description": "fixture",
        "version": "1.0",
        "animations": {
            "idle": "animations/idle.gif",
            "happy": "animations/happy.gif",
        },
        "default_animation": "idle",
        "size": {"width": size[0], "height": size[1]},
    }
    (root / "character.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (root / "metadata.json").write_text(json.dumps({"generator": "test", "actions": ["idle", "happy"]}), encoding="utf-8")
    return root


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_pack_validate_unpack_round_trip_and_deterministic_hash(tmp_path: Path) -> None:
    source = _make_character(tmp_path / "source")
    package_a = pack_character(source, tmp_path / "a.character")
    package_b = pack_character(source, tmp_path / "b.character")

    assert _sha256(package_a) == _sha256(package_b)
    result = validate_archive(package_a)
    assert result.valid, result.errors
    extracted = unpack_character(package_a, tmp_path / "extracted")

    assert result.manifest is not None
    for relative in declared_package_files(result.manifest):
        assert source.joinpath(*relative.split("/")).read_bytes() == extracted.joinpath(*relative.split("/")).read_bytes()


def test_rejects_path_traversal_without_writing_outside(tmp_path: Path) -> None:
    package = tmp_path / "traversal.character"
    with zipfile.ZipFile(package, "w") as zf:
        zf.writestr("../escape.txt", "unsafe")

    result = validate_archive(package)
    assert not result.valid
    assert any("unsafe archive path" in error for error in result.errors)
    with pytest.raises(ValueError):
        unpack_character(package, tmp_path / "output")
    assert not (tmp_path / "escape.txt").exists()


def test_rejects_zip_symbolic_link(tmp_path: Path) -> None:
    package = tmp_path / "symlink.character"
    info = zipfile.ZipInfo("character.json")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(package, "w") as zf:
        zf.writestr(info, "metadata.json")
    result = validate_archive(package)
    assert not result.valid
    assert any("symbolic link" in error for error in result.errors)


def test_missing_manifest_field_is_rejected(tmp_path: Path) -> None:
    source = _make_character(tmp_path / "source")
    manifest_path = source / "character.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["schema_version"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = validate_directory(source)
    assert not result.valid
    assert any("schema_version" in error for error in result.errors)


def test_wrong_gif_size_is_rejected(tmp_path: Path) -> None:
    source = _make_character(tmp_path / "source")
    _write_gif(source / "animations" / "happy.gif", size=(96, 104))
    result = validate_directory(source)
    assert not result.valid
    assert any("happy.gif size is 96x104" in error for error in result.errors)


def test_missing_metadata_is_rejected(tmp_path: Path) -> None:
    source = _make_character(tmp_path / "source")
    (source / "metadata.json").unlink()
    result = validate_directory(source)
    assert not result.valid
    assert any("metadata.json" in error for error in result.errors)


def test_case_conflicting_zip_paths_are_rejected(tmp_path: Path) -> None:
    package = tmp_path / "collision.character"
    with zipfile.ZipFile(package, "w") as zf:
        zf.writestr("metadata.json", "{}")
        zf.writestr("Metadata.json", "{}")
    result = validate_archive(package)
    assert not result.valid
    assert any("case-conflicting" in error for error in result.errors)


def test_studio_export_sanitizes_metadata(tmp_path: Path) -> None:
    studio = tmp_path / "studio_pet"
    studio.mkdir()
    _write_gif(studio / "idle.gif")
    private_root = r"C:\\Users\\Private\\Firefly"
    studio_metadata = {
        "character": "studio_pet",
        "generator": "IP-Adapter + AnimateDiff-Lightning",
        "size": "192x208",
        "reference_image": private_root + r"\\reference.png",
        "actions": {
            "idle": {
                "gif": private_root + r"\\idle.gif",
                "frames": private_root + r"\\frames",
                "backend_metadata": private_root + r"\\idle.json",
                "action_prompt": "gentle breathing",
                "created_time": private_root + r"\\not-public.txt",
                "total_seconds": 12.5,
            }
        },
    }
    (studio / "metadata.json").write_text(json.dumps(studio_metadata), encoding="utf-8")
    package = export_character(studio, tmp_path / "studio_pet.character")

    assert validate_archive(package).valid
    with zipfile.ZipFile(package) as zf:
        exported = json.loads(zf.read("metadata.json"))
        raw = json.dumps(exported)
    assert private_root not in raw
    assert "reference_image" not in raw
    assert '"gif"' not in raw
    assert "backend_metadata" not in raw
    assert "not-public.txt" not in raw
    assert exported["generator"] == "IP-Adapter + AnimateDiff-Lightning"
    assert exported["actions"]["idle"]["frames"] == 2


def test_export_without_idle_requires_explicit_default(tmp_path: Path) -> None:
    studio = tmp_path / "studio_pet"
    studio.mkdir()
    _write_gif(studio / "happy.gif")
    (studio / "metadata.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="default-animation"):
        export_character(studio, tmp_path / "studio_pet.character")

    package = export_character(
        studio,
        tmp_path / "studio_pet.character",
        default_animation="happy",
    )
    assert validate_archive(package).valid
