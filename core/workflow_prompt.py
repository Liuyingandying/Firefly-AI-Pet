"""Workflow-specific prompt wrappers for workflow steps (Phase 9D.2/9D.4/9D.5).

Qt-free and provider-free. This is the only place that wraps a user
:class:`TaskRequest` into a workflow step's prompt. ``build_plan_prompt``
(9D.2) is the managed Claude Plan call: the user's task text is preserved
verbatim and the instruction keeps the step read-only. ``build_implement_prompt``
(9D.4) is the Codex Implement call: the original task and the PLAN artifact
text are the only context sources, and the instruction keeps Codex on its own
native permission and sandbox policy. ``build_review_prompt`` (9D.5) is the
Claude Review call: artifact-first context (task + PLAN + CHANGED_FILES, with
an optional IMPLEMENTATION_SUMMARY) that asks the model to check the current
workspace read-only. None of these wrappers ever asks a model to auto-approve,
skip permissions, must-execute, force success, or bypass sandbox.
"""

from __future__ import annotations

from core.routing_models import TaskRequest

PLAN_STEP_PROMPT = """You are the planning step of a user-confirmed plan -> implement -> review workflow.

Plan the user request below. Do not modify project files: produce a plan only.

User request:
{task}

Produce a concise implementation plan in Markdown. Include:
- Goal
- Relevant files/components
- Planned changes
- Validation/tests
- Risks/unknowns
"""


def build_plan_prompt(request: TaskRequest) -> str:
    """Wrap a user task into the Plan step prompt, preserving the task verbatim."""
    if not isinstance(request, TaskRequest):
        raise ValueError("build_plan_prompt requires a TaskRequest")
    task = (request.text or "").strip()
    if not task:
        raise ValueError("a workflow requires a non-empty task")
    return PLAN_STEP_PROMPT.format(task=task)


IMPLEMENT_STEP_PROMPT = """You are the implementation step of a user-confirmed plan -> implement -> review workflow.

Work only in the current workspace.

Implement the user's task using the supplied plan.

Use your normal native permission and sandbox policy. Do not bypass approvals.

At the end, leave the workspace in a reviewable state.

User task:
{task}

Implementation plan:
{plan}
"""


def build_implement_prompt(request: TaskRequest, plan_text: str) -> str:
    """Wrap a user task + PLAN artifact into the Implement step prompt.

    Artifact-first: the only context sources are the original task request
    (verbatim) and the PLAN artifact text. No transcript, no chat history, no
    environment dump, no session id, and no API key is ever included.
    """
    if not isinstance(request, TaskRequest):
        raise ValueError("build_implement_prompt requires a TaskRequest")
    task = (request.text or "").strip()
    if not task:
        raise ValueError("a workflow requires a non-empty task")
    plan = (plan_text or "").strip()
    if not plan:
        raise ValueError("a workflow requires a non-empty plan")
    return IMPLEMENT_STEP_PROMPT.format(task=task, plan=plan)


REVIEW_STEP_PROMPT = """You are the review step of a user-confirmed plan -> implement -> review workflow.

Review the provided context in read-only mode. Compare:
- the original user task
- the implementation plan
- the changed-files evidence
- the current contents of the relevant workspace files

Do not modify project files. Stay read-only.

Some changed paths may be listed as Deleted and no longer exist in the
workspace. Base your review on the plan, the change evidence, and the remaining
workspace.

Produce a concise Markdown review. Include:

# Verdict
PASS / NEEDS_CHANGES / UNCERTAIN

# Summary

# Findings

# Validation

# Recommended Next Actions

User task:
{task}

Implementation plan:
{plan}

Changed files:
{changed_files}

Implementation summary:
{summary}

Current changed file contents:
{workspace_context}
"""


def build_review_prompt(
    request: TaskRequest,
    plan_text: str,
    changed_files_text: str,
    *,
    summary_text: str | None = None,
    workspace_context: str | None = None,
) -> str:
    """Wrap a user task + PLAN + CHANGED_FILES into the Review step prompt.

    Artifact-first and read-only: the context sources are the original task
    request (verbatim), the PLAN artifact, the CHANGED_FILES artifact, an
    optional IMPLEMENTATION_SUMMARY (auxiliary only), and an optional
    ``workspace_context`` (the current changed-file contents produced by the
    deterministic ReviewContext builder — the direct provider has no file tools,
    so the contents must be inlined). The instruction tells the model to review
    read-only and explicitly forbids modifying files. No transcript, no chat
    history, no environment dump, no session id, and no API key is ever
    included, and the prompt never demands a PASS verdict.
    """
    if not isinstance(request, TaskRequest):
        raise ValueError("build_review_prompt requires a TaskRequest")
    task = (request.text or "").strip()
    if not task:
        raise ValueError("a workflow requires a non-empty task")
    plan = (plan_text or "").strip()
    if not plan:
        raise ValueError("a workflow requires a non-empty plan")
    changed = (changed_files_text or "").strip()
    if not changed:
        raise ValueError("a workflow requires changed-files evidence")
    summary = (summary_text or "").strip() or "(no implementation summary provided)"
    context = (workspace_context or "").strip() or "(no changed file contents provided)"
    return REVIEW_STEP_PROMPT.format(
        task=task, plan=plan, changed_files=changed, summary=summary, workspace_context=context
    )
