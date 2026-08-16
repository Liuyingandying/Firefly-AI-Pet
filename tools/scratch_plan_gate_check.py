"""Scratch: deterministic Plan validity gate draft — self-check against real artifacts.
Run only for design verification in Phase 9D.6-H1. Not production code.
"""
import re

REASON_EMPTY = "EMPTY"
REASON_TOO_SHORT = "TOO_SHORT"
REASON_NO_TASK_REFERENCE = "NO_TASK_REFERENCE"
REASON_GENERIC_NO_TASK = "GENERIC_NO_TASK_RESPONSE"
REASON_VALID = "VALID"

MIN_PLAN_LEN = 80

NO_TASK_PATTERNS = (
    "i don't see a specific task",
    "i don't see a task",
    "no task or request",
    "don't see any task",
    "what would you like me to",
    "please provide a task",
    "please provide the task",
    "please provide more details",
    "i'll need more information",
    "could you provide",
    "can you provide a task",
    "no request yet",
    "specific request yet",
    "i'm not sure what you'd like",
    "i am not sure what you'd like",
    "as an ai language model",
    "i can't help",
)

FILE_TOKEN_RE = re.compile(r"(?<![\w./\\])([A-Za-z0-9_][A-Za-z0-9_.\-]*\.[A-Za-z0-9]{1,10})(?![\w./\\])")


def file_terms(task_text: str) -> list[str]:
    seen, out = set(), []
    for m in FILE_TOKEN_RE.finditer(task_text or ""):
        t = m.group(1)
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def validate_plan_text(plan_text: str, task_text: str) -> dict:
    text = (plan_text or "").strip()
    if not text:
        return {"valid": False, "reason_codes": [REASON_EMPTY], "matched_task_terms": [], "length": 0}
    codes = []
    if len(text) < MIN_PLAN_LEN:
        codes.append(REASON_TOO_SHORT)
    lowered = text.lower()
    if any(p in lowered for p in NO_TASK_PATTERNS):
        codes.append(REASON_GENERIC_NO_TASK)
    terms = file_terms(task_text)
    matched = [t for t in terms if t in text]
    if terms and not matched:
        codes.append(REASON_NO_TASK_REFERENCE)
    if not codes:
        codes.append(REASON_VALID)
    return {"valid": codes == [REASON_VALID], "reason_codes": codes, "matched_task_terms": matched, "length": len(text)}


TASK = "Fix greeting.py so the existing test passes. Do not modify test_greeting.py."

REAL_9D6 = """I don't see a specific task or request yet. What would you like me to plan? Let me know what you're trying to build, fix, or change, and I'll investigate the codebase and put together a plan."""

GOOD_PLAN = """# Plan: Fix greeting.py

## Goal
Make `greet(name)` return `"Hello, {name}!"` so the existing test passes.

## Relevant files
- greeting.py: change `return "Hello"` to `return f"Hello, {name}!"`
- test_greeting.py: read-only reference; must not be modified

## Planned changes
1. Update greeting.py to interpolate the passed name.

## Validation
- Run `python test_greeting.py` — must pass.

## Risks
- None significant; single-function change."""

for label, plan in (("REAL_9D6", REAL_9D6), ("GOOD_PLAN", GOOD_PLAN), ("EMPTY", ""), ("SHORT", "Make it work.")):
    r = validate_plan_text(plan, TASK)
    print(f"{label}: valid={r['valid']} codes={r['reason_codes']} matched={r['matched_task_terms']} len={r['length']}")

# A plan that mentions the file but is still clearly a no-task response:
r = validate_plan_text("I don't see a specific task or request yet. But greeting.py exists here.", TASK)
print("EDGE both-generic-and-file:", r["valid"], r["reason_codes"])
