"""Install the firefly-learning Z Code skill (Phase 7).

The skill source lives in the repo (learning/skill_source/) and is copied to
the user's Z Code skills directory (~/.zcode/skills/firefly-learning/). The
installer is plain file copying so tests can target a tmp directory.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

SKILL_NAME = "firefly-learning"
SKILL_SOURCE_DIR = Path(__file__).resolve().parent / "skill_source"
DEFAULT_SKILLS_ROOT = Path.home() / ".zcode" / "skills"


class SkillInstallError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def skill_version() -> str:
    text = (SKILL_SOURCE_DIR / "SKILL.md").read_text(encoding="utf-8")
    match = re.search(r'^version:\s*"?([^"\n]+)"?', text, re.MULTILINE)
    return match.group(1).strip() if match else "0"


def install_skill(dest_root: Path | str | None = None) -> Path:
    """Copy the skill source into ``dest_root/firefly-learning/``."""
    target = Path(dest_root) if dest_root is not None else DEFAULT_SKILLS_ROOT / SKILL_NAME
    if not SKILL_SOURCE_DIR.is_dir():
        raise SkillInstallError("SKILL_SOURCE_MISSING", f"skill 源缺失: {SKILL_SOURCE_DIR}")
    target.mkdir(parents=True, exist_ok=True)
    for source_file in SKILL_SOURCE_DIR.rglob("*"):
        if source_file.is_file():
            relative = source_file.relative_to(SKILL_SOURCE_DIR)
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, destination)
    installed = target / "SKILL.md"
    if not installed.is_file():
        raise SkillInstallError("SKILL_INSTALL_FAILED", f"SKILL.md 未落盘: {installed}")
    text = installed.read_text(encoding="utf-8")
    if not re.search(rf"^name:\s*{SKILL_NAME}\s*$", text, re.MULTILINE):
        raise SkillInstallError(
            "SKILL_INSTALL_FAILED", f"安装后的 skill name 不匹配: {installed}"
        )
    return target
