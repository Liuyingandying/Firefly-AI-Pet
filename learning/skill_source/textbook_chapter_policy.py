"""Read-only PDF bookmark evidence for chapter-discovery ambiguity decisions.

This helper never writes a course, discovery draft, or curriculum.  The caller
must submit each suggested decision through teach-mcp and check its result.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from pypdf import PdfReader


CHAPTER = re.compile(r"^第\s*[一二三四五六七八九十百\d]+\s*章")
SECTION = re.compile(r"^(\d+)\s*[-–]\s*(\d+)\s*(.+)$")
EXERCISES = re.compile(r"^习\s*题$")


def _plain(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def bookmark_policy(document_path: str | Path) -> dict:
    """Return verified chapter/exercise boundaries from PDF bookmarks only."""
    reader = PdfReader(str(document_path))
    entries: list[tuple[int, str, int]] = []

    def visit(items: list, depth: int = 0) -> None:
        for item in items:
            if isinstance(item, list):
                visit(item, depth + 1)
                continue
            try:
                page = reader.get_destination_page_number(item) + 1
                if 1 <= page <= len(reader.pages):
                    entries.append((page, str(item.title).strip(), depth))
            except (AttributeError, KeyError, ValueError):
                continue

    visit(reader.outline)
    chapters = [(page, title, depth) for page, title, depth in entries
                if CHAPTER.match(title)]
    chapters.sort(key=lambda x: x[0])
    ranges = []
    sections = []
    for start, _, depth in chapters:
        peer_starts = [page for page, _, entry_depth in entries
                       if page > start and entry_depth <= depth]
        end = min(peer_starts) - 1 if peer_starts else len(reader.pages)
        children = [(page, name) for page, name, _ in entries if start <= page <= end]
        exercise_pages = [page for page, name in children if EXERCISES.fullmatch(name)]
        if len(exercise_pages) == 1 and start < exercise_pages[0] <= end:
            ranges.append({"start_page": exercise_pages[0], "end_page": end,
                           "chapter_page": start})
        for page, name in children:
            match = SECTION.match(name)
            if match:
                sections.append({"page": page, "chapter": int(match.group(1)),
                                 "section": int(match.group(2)), "title": match.group(3)})
    return {"document_path": str(Path(document_path).resolve()),
            "page_count": len(reader.pages), "chapter_count": len(chapters),
            "first_chapter_page": chapters[0][0] if chapters else None,
            "exercise_ranges": ranges, "section_bookmarks": sections}


def recommend_decision(item: dict, policy: dict) -> dict | None:
    """Suggest only decisions supported by a bookmark; unknown means review."""
    page = item.get("page")
    candidates = item.get("candidates") or []
    if not isinstance(page, int) or not candidates:
        return None
    first = policy.get("first_chapter_page")
    if (item.get("reason") == "first_unit_title_normalization"
            and isinstance(first, int) and page < first):
        return {"candidate_id": candidates[0]["candidate_id"],
                "decision": "ignore", "normalized_title": "",
                "evidence": "front_matter_before_first_bookmarked_chapter"}
    for span in policy.get("exercise_ranges", []):
        if span["start_page"] <= page <= span["end_page"]:
            return {"candidate_id": candidates[0]["candidate_id"],
                    "decision": "ignore", "normalized_title": "",
                    "evidence": "bookmarked_exercise_range"}
    matches = []
    for bookmark in policy.get("section_bookmarks", []):
        if bookmark["page"] != page:
            continue
        for candidate in candidates:
            if (candidate.get("ch") == bookmark["chapter"]
                    and candidate.get("sec") == bookmark["section"]
                    and _plain(candidate.get("title", "")) == _plain(bookmark["title"])):
                matches.append((candidate, bookmark))
    if len(matches) == 1:
        candidate, bookmark = matches[0]
        return {"candidate_id": candidate["candidate_id"],
                "decision": "chapter", "normalized_title": bookmark["title"],
                "evidence": "matching_section_bookmark"}
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only chapter bookmark policy")
    parser.add_argument("document_path", type=Path)
    args = parser.parse_args()
    print(json.dumps(bookmark_policy(args.document_path), ensure_ascii=False))


if __name__ == "__main__":
    main()
