#!/usr/bin/env python
"""Static verification of the Firefly AI Pet Windows bundle (no app launch).

Usage (from repo root):
    .venv/Scripts/python.exe packaging/verify_dist.py [--dist dist/Firefly_AI_Pet]

Checks:
  1. entry points exist (Firefly_AI_Pet.exe, _internal/)
  2. runtime resources collected (animations, icon, pdfjs, character assets)
  3. config directory contains EXACTLY the four whitelisted templates and the
     hardware example is sanitized
  4. no user data / secrets / local databases shipped anywhere in the bundle
     (runtime, logs, backups dirs; memory/conversation/scratchpad data files;
     .env; real hardware_devices.json; provider state; *.sqlite/*.db)

Prints one line per check and a final "VERIFY: PASS" / "VERIFY: FAIL".
Exit code 0 = PASS, 1 = FAIL.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

TEMPLATES = {
    "companion.json",
    "voice_config.yaml",
    "path_config.yaml",
    "hardware_devices.example.json",
}
ANIMATIONS = {
    "failed.gif", "idle.gif", "jumping.gif",
    "review.gif", "running.gif", "waiting.gif", "waving.gif",
}
FORBIDDEN_FILENAMES = {
    "memory_records.json", "memory_suggestions.json", "conversation.json",
    "bond_state.json", "sessions.json", "ui_settings.json",
    "pet_preferences.json", "provider_state.json",
    "hardware_devices.json", ".env",
}
FORBIDDEN_SUFFIXES = (".sqlite", ".sqlite3", ".db")
FORBIDDEN_DIRS = {"runtime", "logs", "backups"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", default="dist/Firefly_AI_Pet")
    args = ap.parse_args()
    root = Path(args.dist).resolve()
    internal = root / "_internal"
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        line = ("PASS  " if ok else "FAIL  ") + name + (f" - {detail}" if detail else "")
        print(line)
        if not ok:
            failures.append(name)

    if not root.is_dir():
        print(f"FAIL  dist root not found: {root}")
        print("\nVERIFY: FAIL")
        return 1

    check("entry exe exists", (root / "Firefly_AI_Pet.exe").is_file())
    check("_internal exists", internal.is_dir())

    assets = internal / "assets"
    anim_dir = assets / "animations"
    got_anim = {p.name for p in anim_dir.glob("*.gif")} if anim_dir.is_dir() else set()
    check(
        "animations collected (7 gifs)",
        got_anim == ANIMATIONS,
        f"missing={sorted(ANIMATIONS - got_anim)} extra={sorted(got_anim - ANIMATIONS)}",
    )
    check("app icon collected", (assets / "firefly.ico").is_file())
    check(
        "pdfjs collected",
        (assets / "paper_reader" / "pdfjs" / "build" / "pdf.mjs").is_file(),
    )
    check(
        "character assets collected",
        (internal / "character" / "firefly" / "identity.yaml").is_file(),
    )

    conf = internal / "config"
    conf_files = {p.name for p in conf.iterdir()} if conf.is_dir() else set()
    check(
        "config contains exactly the 4 templates",
        conf_files == TEMPLATES,
        f"got={sorted(conf_files)}",
    )
    hw = conf / "hardware_devices.example.json"
    if hw.is_file():
        text = hw.read_text(encoding="utf-8", errors="replace")
        check(
            "hardware_devices.example.json sanitized",
            ('"serial": ""' in text)
            and ('"port": ""' in text)
            and ('"vid_pid": ""' in text),
        )

    hits: list[str] = []
    if root.exists():
        for p in root.rglob("*"):
            rel = p.relative_to(root).as_posix().lower()
            name = p.name.lower()
            if p.is_dir():
                if name in FORBIDDEN_DIRS:
                    hits.append(rel + "/")
                continue
            if name in FORBIDDEN_FILENAMES:
                hits.append(rel)
            elif name == "items.json" and "scratchpad" in rel:
                hits.append(rel)
            elif "scratchpad" in rel and p.parent.name.lower() == "assets":
                hits.append(rel)
            elif name.endswith(FORBIDDEN_SUFFIXES):
                hits.append(rel)
    check("no user data / secrets / local db in bundle", not hits, f"hits={hits[:10]}")

    check(
        "browser extension staged next to exe",
        (root / "extensions" / "pagelens_bridge" / "manifest.json").is_file(),
    )
    check(
        "env template staged next to exe",
        (root / ".env.example").is_file(),
    )

    print()
    if failures:
        print(f"VERIFY: FAIL ({len(failures)} failed checks)")
        return 1
    print("VERIFY: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
