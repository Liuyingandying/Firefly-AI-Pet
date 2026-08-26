"""Interactive CLI for reviewing migration preview candidates."""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from .review import ReviewDecision, apply_review, load_preview, write_reviewed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Review migration preview candidates (accept/reject/edit)"
    )
    parser.add_argument("input", help="path to migration_preview.json")
    parser.add_argument(
        "-o", "--output", default="reviewed_migration.json", help="output path"
    )
    args = parser.parse_args(argv)

    preview = load_preview(args.input)
    decisions = _collect_decisions(preview)
    reviewed = apply_review(preview, decisions)
    write_reviewed(reviewed, args.output)

    total = (
        len(reviewed["memory"]) + len(reviewed["bond"]) + len(reviewed["style"])
    )
    print(
        f"reviewed: {total} accepted, {len(reviewed['rejected'])} rejected -> "
        f"{args.output}"
    )
    return 0


def _collect_decisions(preview: dict) -> list[ReviewDecision]:
    decisions: list[ReviewDecision] = []
    for group in ("memory", "bond", "style"):
        for item in preview.get("groups", {}).get(group, []):
            decision = _prompt(item)
            if decision is not None:
                decisions.append(decision)
    return decisions


def _prompt(item: dict) -> ReviewDecision | None:
    preview_text = item.get("preview") or item.get("content") or item.get("detail")
    print(f"\n[{item.get('id')}] {item.get('kind')} ({item.get('band')}) - {preview_text}")
    risks = item.get("risk")
    if risks:
        print(f"  risk: {', '.join(risks)}")
    print("  (a)ccept / (r)eject / (e)dit")
    while True:
        choice = input("> ").strip().lower()
        if choice in ("a", "accept", ""):
            return ReviewDecision(item["id"], "accept")
        if choice in ("r", "reject"):
            return ReviewDecision(item["id"], "reject")
        if choice in ("e", "edit"):
            new_content = input("new content: ").strip()
            return ReviewDecision(item["id"], "edit", new_content or None)
        print("  choose a / r / e")


if __name__ == "__main__":
    sys.exit(main())
