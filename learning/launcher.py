"""Firefly -> Z Code handoff (Phase 10).

``launch_learning_mode(course_id, action)`` resolves identity, (re)builds the
Learning Context atomically, and starts Z Code with the course workspace as
cwd. Z Code CLI parameters were verified by audit (zcode 0.16.9 --help):
``--prompt`` runs headless, ``--cwd`` sets the working directory, and the
shared config loads teach-mcp. The context file path is additionally exported
as ``FIREFLY_LEARNING_CONTEXT`` and always materialized at
``<course>/workspace/.firefly/learning_context.json`` (skill discovery
order: env -> cwd/.firefly/learning_context.json). No clipboard involved.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from core.settings_manager import SettingsManager
from learning._storage import utc_now_iso
from learning.identity import ensure_learner_id
from learning.learning_context import ACTIONS, build_learning_context, write_learning_context
from learning.resource_manager import ResourceManager

CONTEXT_ENV_VAR = "FIREFLY_LEARNING_CONTEXT"
ZCODE_CLI_ENV_VAR = "FIREFLY_ZCODE_CLI"
ZCODE_RUNTIME_ENV_VAR = "FIREFLY_ZCODE_RUNTIME"
NODE_ENV_VAR = "FIREFLY_NODE"

DEFAULT_WORKSPACE = Path("workspace")


class LaunchError(Exception):
    """Structured launch failure; ``code`` is stable for tests and UI."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


@dataclass
class LaunchPlan:
    course_id: str
    action: str
    learner_id: str
    workspace: Path
    context_file: Path
    argv: list[str]
    cwd: Path
    env: dict = field(default_factory=dict)
    mode: str = "task-injected"
    task_id: str = ""

    @property
    def uses_wt(self) -> bool:
        return bool(self.argv) and Path(self.argv[0]).name.lower() == "wt.exe"


@dataclass
class LaunchResult:
    plan: LaunchPlan
    spawned: bool
    pid: int | None = None


def resolve_zcode_cli() -> Path:
    """Locate zcode.cjs: env override, then the default desktop install.

    Note: ui/agent_launcher.launch_zcode only opens the desktop GUI (no
    workspace/prompt semantics); the bridge needs the CLI for --prompt/--cwd.
    """
    override = os.environ.get(ZCODE_CLI_ENV_VAR, "").strip()
    if override:
        path = Path(override)
        if not path.is_file():
            raise LaunchError("ZCODE_CLI_NOT_FOUND", f"FIREFLY_ZCODE_CLI 指向的文件不存在: {path}")
        return path
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidate = Path(local) / "Programs" / "ZCode" / "resources" / "glm" / "zcode.cjs"
        if candidate.is_file():
            return candidate
    raise LaunchError(
        "ZCODE_CLI_NOT_FOUND",
        "未找到 Z Code CLI（zcode.cjs）。请安装 Z Code 桌面版，"
        f"或设置环境变量 {ZCODE_CLI_ENV_VAR} 指向 zcode.cjs。",
    )


def resolve_node() -> str:
    override = os.environ.get(NODE_ENV_VAR, "").strip()
    if override:
        return override
    node = shutil_which("node")
    if node:
        return node
    raise LaunchError("NODE_NOT_FOUND", "未找到 node 运行时，无法启动 Z Code CLI。")


def find_zcode_runtime(*, interactive: bool = True) -> Path:
    """Locate the official standalone distribution's unified TUI entry.

    A PATH command wins when it is a CLI wrapper. Windows ``ZCode.exe`` is
    Electron, so an exe match must never be accepted as the Agent CLI.
    """
    def is_standalone(entry: Path) -> bool:
        if entry.name.lower() not in {"zcode.mjs", "zcode.cmd"}:
            return False
        return (
            (entry.parent / "zcode.mjs").is_file()
            and (entry.parent.parent / "agent" / "zcode.cjs").is_file()
        )

    command = shutil_which("zcode.cmd") if sys.platform == "win32" else shutil_which("zcode")
    if command and is_standalone(Path(command)):
        return Path(command)
    configured = os.environ.get(ZCODE_RUNTIME_ENV_VAR, "").strip()
    if not configured and sys.platform == "win32":
        # Explorer can retain an old environment after a user-scoped install.
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                configured = str(winreg.QueryValueEx(key, ZCODE_RUNTIME_ENV_VAR)[0]).strip()
        except OSError:
            pass
    if configured:
        entry = Path(configured).expanduser()
        if entry.is_file() and is_standalone(entry):
            return entry
    if not interactive:
        return resolve_zcode_cli()  # legacy headless compatibility only
    raise LaunchError(
        "ZCODE_TUI_RUNTIME_MISSING",
        "未安装 Z Code 命令行学习环境。请安装官方 standalone Z Code，"
        f"并设置 {ZCODE_RUNTIME_ENV_VAR} 指向其 bin/zcode.mjs 或 Windows 启动脚本。",
    )


def _runtime_argv(runtime: Path) -> list[str]:
    if runtime.suffix.lower() in {".mjs", ".cjs"}:
        bundled_node = runtime.parent.parent / "node.exe"
        node = str(bundled_node) if bundled_node.is_file() else resolve_node()
        return [node, str(runtime)]
    return [str(runtime)]


def shutil_which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


BOOTSTRAP_TEMPLATE = """你正在处理来自 Firefly AI Pet 的学习任务。

必须使用 firefly-learning Skill。

Learning Context：
{context_path}

结果文件（本轮结束前必须写入这个绝对路径）：
{result_path}

本次 action：{action}

执行规则：

0. 输出的第一行必须原样是：
   [FIREFLY_SKILL_START] course_id=<course_id> action=<action>
   结束时最后一行必须原样是：
   [LEARNING_ACTION_RESULT] status=<ok|failed> detail=<一句话结果>
1. 读取并验证 Learning Context（路径如上）。
2. 使用其中的 learner_id、course_id、action。
3. 不得自行生成 learner_id/course_id/session_id。
4. 学习状态只从 teach-mcp 获取。
5. action=new：按 firefly-learning new 协议执行。
6. action=review：按人工审核协议执行。
7. action=resume：调 learning_status / resume_learning 恢复现有学习；
   恢复后直接继续教学（出下一题/讲下一步），不要等待用户确认。
8. 不得根据当前 Z Code 对话历史猜测学习状态。
9. action=answer：读取 <课程目录>/bridge/learning_action.json 中的
   question_id 与 student_answer，调 teach-mcp evaluate_answer + record_result
   判分记录，然后继续教学（讲解 + 下一题）。
10. 每当你停下来把内容交给用户时（出题等待回答、讲完一段、到达审核门、
    或发生错误），本轮的最后一步必须先把当前结果写入上面给出的
    结果文件绝对路径（原子写：先写 result.json.tmp 再改名）。
    特别注意：出题之后、等待用户回答之前，必须已经写好
    message_type=question 的 result——否则 Firefly 用户永远看不到题目。
    结构：
    {"schema_version":1,"task_id":"<本次task_id>","course_id":"...","action":"...",
     "status":"ok|waiting_review|error","message_type":"lesson|question|review|info|error",
     "display_text":"<给用户看的最终文本>",
     "question":{"question_id":"<teach-mcp原值>","text":"...","options":["A选项","B选项",...]},
     "opaque_refs":{"session_id":"<teach-mcp原值>"},"created_at":"<ISO时间>"}
    message_type=question 时必须带 question（question_id 用 teach-mcp 原值）。
    result.json 是 Firefly 展示的唯一正式通道；stdout/launch.log 只是调试。
    Firefly 只展示你的结果，不会再有对话，因此 display_text 必须完整。
    每一轮交付都以写好 result.json 结束；不写 = Firefly 用户什么都看不到。

开始执行。"""


def build_learning_bootstrap_prompt(context_path: Path | str, action: str) -> str:
    """Deterministic, short, fixed bootstrap prompt attached to EVERY launch.

    Firefly must never merely "open Z Code" — every handoff injects an
    explicit learning task pointing at the Learning Context.
    """
    context_path = Path(context_path)
    # context lives at <course>/workspace/.firefly/learning_context.json ->
    # the course dir (where bridge/result.json belongs) is three levels up.
    result_path = context_path.parent.parent.parent / "bridge" / "result.json"
    return (
        BOOTSTRAP_TEMPLATE
        .replace("{context_path}", str(context_path))
        .replace("{result_path}", str(result_path))
        .replace("{action}", action)
    )


def find_official_wrapper(cli_path: Path | None = None) -> Path | None:
    """Return an official CLI wrapper from PATH, if one ever exists.

    Audit (2026-09-27, task-injection fix): the desktop install ships NO
    wrapper. The earlier probe list included ``zcode.exe``, which on
    Windows (case-insensitive) matched the **Electron desktop shell**
    ``ZCode.exe`` — spawning it just opened the GUI's most recent
    conversation instead of running a learning task. The desktop shell is
    NOT a CLI wrapper; only a PATH-resolved dedicated wrapper counts.
    """
    for name in ("zcode-cli", "zcode-cli.exe", "zcode-cli.cmd"):
        found = shutil_which(name)
        if found and Path(found).is_file():
            return Path(found)
    return None


def _ensure_zcode_provider_env(env: dict, cli_path: Path) -> None:
    """Inject the CLI's OFFICIAL provider-config env vars when absent.

    Root cause (audit 2026-09-27): ``zcode.cjs`` resolves its bundled
    provider config relative to its own entry-point directory, which does
    NOT match the desktop install layout. When the desktop app spawns the
    CLI it sets ``ZCODE_BUILTIN_PROVIDER_CONFIG_FILE`` +
    ``ZCODE_PERSONAL_PROVIDER_CONFIG_FILE`` and the CLI short-circuits its
    search; a standalone spawn (Firefly) inherits neither, so the CLI exits
    1 with "无法定位 CLI ZCode Built-in Provider Config". We derive both from
    the install layout at runtime — official env vars only, no file copies,
    no machine-specific paths in code.

    Layout:  <installRoot>/resources/glm/zcode.cjs
             <installRoot>/resources/config/provider/zcode-builtin.json
             <dataBase>/.zcode/v2/provider_config.json
    """
    builtin_var = "ZCODE_BUILTIN_PROVIDER_CONFIG_FILE"
    personal_var = "ZCODE_PERSONAL_PROVIDER_CONFIG_FILE"
    if env.get(builtin_var) and env.get(personal_var):
        return  # already provided (e.g. ZCode-aware parent shell) — pass through
    if cli_path.suffix.lower() in {".mjs", ".cmd"}:
        builtin = cli_path.parent.parent / "agent" / "provider" / "zcode-builtin.json"
    else:
        resources_dir = cli_path.parent.parent  # <installRoot>/resources
        builtin = resources_dir / "config" / "provider" / "zcode-builtin.json"
    data_base = env.get("ZCODE_DATA_BASE_DIR") or str(Path.home())
    personal = Path(data_base) / ".zcode" / "v2" / "provider_config.json"
    if builtin.is_file() and personal.is_file():
        env.setdefault(builtin_var, str(builtin))
        env.setdefault(personal_var, str(personal))
    # else: leave untouched — the CLI reports its own structured error
    # (provider config missing) instead of us guessing further.


def default_session_db() -> Path:
    """Path of the Z Code CLI session database (audit: ~/.zcode/cli/db)."""
    return Path.home() / ".zcode" / "cli" / "db" / "db.sqlite"


def zcode_session_exists(session_id: str, *, course_workspace: Path | None = None) -> bool:
    """Zero-side-effect existence probe against Z Code's own session db.

    Read-only. On ANY failure to query (db missing/locked/moved) this
    returns True — optimistic reuse — because the reference was written by
    our own init run and the CLI itself will surface a broken session.
    """
    if not session_id:
        return False
    db = default_session_db()
    if not db.is_file():
        return True  # cannot verify; be optimistic
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=2)
        try:
            row = con.execute(
                "SELECT 1 FROM session WHERE id = ? LIMIT 1", (session_id,)
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return True  # optimistic on query failure
    return row is not None


def _run_rollover_init(
    course_dir: Path,
    workspace: Path,
    action: str,
    learner_id: str,
    task_holder: dict,
    old_session_id: str,
    old_usage: int | None,
    runtime: Path,
) -> None:
    """Start the ROLLOVER init run (Phase 4/5).

    Writes a minimal resume snapshot (references only — never mastery/
    progress copies), then spawns the bootstrap headlessly with --json so
    bridge_session can bind the NEW zcode_session_id when it lands. The old
    conversation history is deliberately not carried over; teach-mcp
    resume_learning restores the real position (topic/stage/pending
    question) inside the new session.
    """
    import json as _json

    from learning._storage import atomic_write_json, utc_now_iso
    from learning.course_binding import load_binding
    from learning.diagnostics import log_marker
    from learning.task_state import write_task

    task_id = "task-" + secrets.token_hex(5)
    task_holder["task_id"] = task_id

    binding = load_binding(course_dir) or {}
    teach_refs = {
        k: binding[k] for k in ("pipeline_id", "curriculum_id") if binding.get(k)
    }
    # pending question ref from the last delivered result, if any
    pending_question = None
    teach_session = None
    result_file = course_dir / "bridge" / "result.json"
    if result_file.is_file():
        try:
            last = _json.loads(result_file.read_text(encoding="utf-8"))
            teach_session = str((last.get("opaque_refs") or {}).get("session_id", "") or "") or None
            q = last.get("question") or {}
            if last.get("message_type") == "question" and q.get("question_id"):
                pending_question = str(q["question_id"])
        except (OSError, ValueError):
            pass

    snapshot = {
        "schema_version": 1,
        "reason": "context_budget",
        "old_zcode_session_id": old_session_id,
        "old_context_usage": old_usage,
        "learner_id": learner_id,
        "course_id": course_dir.name,
        "teach_mcp_session_id": teach_session,
        "pending_question_id": pending_question,
        "teach_refs": teach_refs,
        "created_at": utc_now_iso(),
    }
    atomic_write_json(course_dir / "bridge" / "rollover_snapshot.json", snapshot)

    workspace.mkdir(parents=True, exist_ok=True)
    context = {
        "schema_version": 1,
        "source": "firefly",
        "learner_id": learner_id,
        "course_id": course_dir.name,
        "manifest_path": str((course_dir / "manifest.json").resolve()),
        "action": "resume",
        "created_at": utc_now_iso(),
        "task_id": task_id,
        "delivery_mode": "interactive",
    }
    for key, value in teach_refs.items():
        context[key] = value
    context_file = course_dir / DEFAULT_WORKSPACE / ".firefly" / "learning_context.json"
    atomic_write_json(context_file, context)

    bootstrap = build_learning_bootstrap_prompt(context_file, "resume")
    rollover_note = (
        "\n\n【会话整理】本会话因上下文整理而新建。上面的最小引用是全部衔接信息："
        "先 learning_status / resume_learning 校准真实状态；"
    )
    if pending_question:
        rollover_note += (
            f"待作答题 question_id={pending_question}，沿用原题继续，"
            "不得另出一题、不得重复计分。"
        )
    else:
        rollover_note += "若无待作答题目，按真实进度继续教学。"
    rollover_note += "对话历史无需复述，用户在 Z Code 中继续学习。"
    prompt = bootstrap + rollover_note
    argv = [*_runtime_argv(runtime), "--prompt", prompt, "--cwd", str(workspace), "--json"]
    env = dict(os.environ)
    env[CONTEXT_ENV_VAR] = str(context_file)
    _ensure_zcode_provider_env(env, runtime if runtime.suffix.lower() in {".cmd", ".exe"} else resolve_zcode_cli())

    for stale in course_dir.glob("bridge/init-*.json"):
        try:
            stale.unlink()
        except OSError:
            pass
    init_json_path = course_dir / "bridge" / f"init-{task_id}.json"
    pid = _spawn_detached(argv, workspace, env, _DETACHED_FLAGS, stdout_path=init_json_path)
    write_task(course_dir, task_id=task_id, status="running", action="rollover", pid=pid)
    task_holder["pid"] = pid
    log_marker(
        "ZCODE_SESSION_ROLLOVER",
        course_id=course_dir.name,
        old_session_id=old_session_id,
        new_session_id="(pending)",
        context_usage=old_usage,
        reason="context_budget",
    )


def launch_learning_mode(
    course_id: str,
    action: str,
    *,
    settings: SettingsManager | None = None,
    courses_root: Path | str | None = None,
    prompt_extra: str = "",
    answer: dict | None = None,
    review_approved: bool = False,
    delivery_mode: str = "interactive",
    dry_run: bool = False,
    interactive: bool = True,
    spawner=None,
    now=None,
) -> LaunchResult:
    """Resolve identity, write context, and INJECT a learning task into Z Code.

    Every launch attaches the deterministic bootstrap prompt (``-p``) so a
    fresh Z Code task starts working immediately — launching the process
    alone is NOT considered a handoff. Headless by design (CLI semantics:
    ``-p`` runs the task and exits; the TUI cannot accept an initial
    prompt), so there is deliberately no "open the desktop GUI" path — that
    was the bug where users landed in an unrelated recent conversation.

    ``spawner(argv, cwd, env, creationflags) -> pid`` is injectable for tests;
    ``dry_run=True`` returns the plan without spawning anything.
    """
    from learning.learning_context import ACTIONS as _ACTIONS

    if action not in _ACTIONS:
        raise LaunchError("ACTION_INVALID", f"action 必须是 {ACTIONS} 之一，收到: {action!r}")
    settings = settings or SettingsManager()
    learner_id = ensure_learner_id(settings)

    manager = ResourceManager(courses_root) if courses_root is not None else ResourceManager()
    try:
        manifest = manager.load_manifest(course_id)
    except Exception as exc:
        code = getattr(exc, "code", "COURSE_NOT_FOUND")
        raise LaunchError(code, f"课程不可用: {course_id}: {exc}") from exc
    manifest_learner = str(manifest.get("learner_id", "")).strip()
    if manifest_learner != learner_id:
        # learner isolation (Phase 13 #17): a course belongs to the learner
        # who imported it; another learner must never launch into it.
        raise LaunchError(
            "LEARNER_MISMATCH",
            f"课程 {course_id} 属于学习者 {manifest_learner}，"
            f"当前学习者是 {learner_id}，拒绝跨学习者启动。",
        )
    course_dir = manager.course_dir(course_id)
    runtime = find_zcode_runtime(interactive=interactive)
    if interactive:
        _ensure_tui_runtime(_runtime_argv(runtime)[0], runtime, dict(os.environ))
    workspace = course_dir / DEFAULT_WORKSPACE
    workspace.mkdir(parents=True, exist_ok=True)

    task_holder: dict = {}

    # ---- Session Reuse Gate (Interactive v0.1 freeze) ----------------------
    # resume -> existing & resumable session is reopened via --resume
    # directly (no new -p init); missing/corrupted session falls through to
    # the bootstrap init run, which rebuilds a fresh session (binding
    # updated when it completes). new/review/answer are AGENT-WORK turns
    # (process material / promote draft / judge answer) and always run the
    # bootstrap headlessly.
    if action == "resume":
        from learning.course_binding import load_binding
        from learning.diagnostics import log_marker as _log_gate

        binding = load_binding(course_dir) or {}
        existing_session = str(binding.get("zcode_session_id", "") or "")
        if existing_session:
            # Context Budget (v0.1.1): a real session measured beyond the
            # rollover threshold is NOT reopened — a fresh bootstrap init
            # (below) starts a new session; teach-mcp restores everything.
            from learning.context_budget import (
                NORMAL, ROLLOVER_REQUIRED, WARNING, budget_state, latest_context_usage,
            )

            usage = latest_context_usage(existing_session)
            state = budget_state(usage)
            if state == ROLLOVER_REQUIRED:
                _log_gate(
                    "ZCODE_SESSION_ROLLOVER_PRE",
                    course_id=course_id, session_id=existing_session,
                    context_usage=usage, state=state,
                )
                _run_rollover_init(
                    course_dir, workspace, action, learner_id, task_holder,
                    existing_session, usage, runtime,
                )
                settings.set_learning_last_course_id(course_id)
                rollover_plan = LaunchPlan(
                    course_id=course_id,
                    action=action,
                    learner_id=learner_id,
                    workspace=workspace,
                    context_file=course_dir / DEFAULT_WORKSPACE / ".firefly" / "learning_context.json",
                    argv=[],
                    cwd=workspace,
                    mode="session-rollover",
                    task_id=task_holder["task_id"],
                )
                return LaunchResult(
                    plan=rollover_plan,
                    spawned=not dry_run,
                    pid=task_holder.get("pid"),
                )
            if state == WARNING:
                _log_gate("ZCODE_SESSION_WARNING", session_id=existing_session, context_usage=usage)
            if zcode_session_exists(existing_session, course_workspace=workspace):
                _log_gate("ZCODE_SESSION_REUSE", course_id=course_id, session_id=existing_session)
                # Runtime Delivery: resume 轮也重写 context（带
                # delivery_mode=interactive），使 TUI 内的 skill 始终读到
                # 当前交付模式，而不是上次 init 的旧文件。
                context = build_learning_context(
                    course_dir, action, learner_id, manifest=manifest,
                    now=now() if callable(now) else None,
                )
                context["task_id"] = existing_session
                context["delivery_mode"] = "interactive"
                write_learning_context(course_dir, context)
                tui_pid = None
                if not dry_run:
                    tui_pid = open_learning_session(course_dir, existing_session)
                settings.set_learning_last_course_id(course_id)
                return LaunchResult(
                    plan=LaunchPlan(
                        course_id=course_id,
                        action=action,
                        learner_id=learner_id,
                        workspace=workspace,
                        context_file=course_dir / DEFAULT_WORKSPACE / ".firefly" / "learning_context.json",
                        argv=[],
                        cwd=workspace,
                        mode="session-reuse",
                        task_id=existing_session,
                    ),
                    spawned=not dry_run,
                    pid=tui_pid,
                )
            _log_gate(
                "ZCODE_SESSION_REBUILD",
                old_session_id=existing_session,
                reason="resume_failed",
            )

    from learning.task_state import new_task_id, write_task

    if action == "answer":
        # Return channel answer payload: Firefly records WHAT the user said,
        # teach-mcp alone decides what it means (evaluate + record).
        if not isinstance(answer, dict) or not str(
            answer.get("question_id", "")
        ).strip() or not str(answer.get("student_answer", "")).strip():
            raise LaunchError(
                "ANSWER_PAYLOAD_INVALID",
                "action=answer 需要 question_id 与 student_answer（原样引用，不得自造）",
            )
    task_id = new_task_id()

    context = build_learning_context(
        course_dir,
        action,
        learner_id,
        manifest=manifest,
        now=now() if callable(now) else None,
    )
    context["task_id"] = task_id
    if delivery_mode in ("interactive", "embedded"):
        context["delivery_mode"] = delivery_mode
    context_file = write_learning_context(course_dir, context)

    if action == "answer":
        from learning._storage import atomic_write_json

        atomic_write_json(
            course_dir / "bridge" / "learning_action.json",
            {
                "schema_version": 1,
                "task_id": task_id,
                "course_id": course_id,
                "action": "answer",
                "question_id": str(answer["question_id"]),
                "student_answer": str(answer["student_answer"]),
                "created_at": utc_now_iso(),
            },
        )

    from learning.diagnostics import log_marker

    log_marker(
        "LEARNING_CONTEXT_WRITTEN",
        course_id=course_id,
        action=action,
        context_path=str(context_file),
    )

    cli = runtime
    env = dict(os.environ)
    env[CONTEXT_ENV_VAR] = str(context_file)
    _ensure_zcode_provider_env(env, cli)

    if action == "review" and review_approved:
        # Deterministic approval channel: a file protocol beats a prompt
        # tail note — the skill reads bridge/review_approval.json.
        from learning._storage import atomic_write_json

        atomic_write_json(
            course_dir / "bridge" / "review_approval.json",
            {
                "schema_version": 1,
                "task_id": task_id,
                "course_id": course_id,
                "approved": True,
                "created_at": utc_now_iso(),
            },
        )

    prompt = build_learning_bootstrap_prompt(context_file, action)
    if action == "review" and review_approved:
        prompt += (
            "\n\n【用户裁决】用户已批准晋级——批准文件已写在 "
            "bridge/review_approval.json（approved=true）。"
            "按 skill 的 action=review 协议直接执行晋级后续步骤，无需再次询问。"
        )
    if prompt_extra:
        prompt = f"{prompt}\n\n补充信息：{prompt_extra}"
    # Interactive Z Code Learning (Phase 2 audit): the init run executes the
    # bootstrap headlessly with --json so the Z Code sessionId can be read
    # back officially and bound to the course (binding.zcode_session_id);
    # the interactive TUI is then opened via --resume (open_learning_session).
    argv = [*_runtime_argv(runtime), "--prompt", prompt, "--cwd", str(workspace), "--json"]
    creationflags = _DETACHED_FLAGS
    mode = "task-injected"

    plan = LaunchPlan(
        course_id=course_id,
        action=action,
        learner_id=learner_id,
        workspace=workspace,
        context_file=context_file,
        argv=argv,
        cwd=workspace,
        env={CONTEXT_ENV_VAR: str(context_file)},
        mode=mode,
        task_id=task_id,
    )

    settings.set_learning_last_course_id(course_id)

    write_task(
        course_dir, task_id=task_id, status="pending", action=action
    )

    if dry_run:
        log_marker(
            "ZCODE_TASK_INJECT",
            cwd=str(workspace),
            context_path=str(context_file),
            action=action,
            task_id=task_id,
        )
        return LaunchResult(plan=plan, spawned=False)

    log_marker(
        "ZCODE_TASK_INJECT",
        cwd=str(workspace),
        context_path=str(context_file),
        action=action,
        task_id=task_id,
    )
    spawner = spawner or _spawn_detached
    # per-task init output: a lingering previous CLI process may still hold
    # its own init-*.json handle, so every run gets a unique file
    for stale in course_dir.glob("bridge/init-*.json"):
        try:
            stale.unlink()
        except OSError:
            pass  # held by a lingering process; read_init_session_id picks
                  # the newest parseable file instead
    init_json_path = course_dir / "bridge" / f"init-{task_id}.json"
    pid = spawner(argv, workspace, env, creationflags, stdout_path=init_json_path)
    write_task(
        course_dir,
        task_id=task_id,
        status="running",
        action=action,
        pid=pid,
    )
    log_marker("ZCODE_PROCESS_STARTED", pid=pid, mode=mode, task_id=task_id)
    return LaunchResult(plan=plan, spawned=True, pid=pid)


_DETACHED_FLAGS = 0
_NEW_CONSOLE_FLAGS = 0
if sys.platform == "win32":
    _DETACHED_FLAGS = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    _NEW_CONSOLE_FLAGS = subprocess.CREATE_NEW_CONSOLE


def _spawn_detached(
    argv: list[str],
    cwd: Path,
    env: dict,
    creationflags: int,
    stdout_path: Path | None = None,
) -> int:
    """Start Z Code detached; stdout/stderr go to the course bridge log, or
    to ``stdout_path`` (the interactive init run captures the --json
    document there for sessionId extraction)."""
    course_dir = Path(cwd).parent
    log_path = stdout_path or (course_dir / "bridge" / "launch.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "ab")
    try:
        process = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            close_fds=True,
        )
    except OSError as exc:
        raise LaunchError("SPAWN_FAILED", f"启动 Z Code 失败: {exc}") from exc
    return process.pid


def read_init_session_id(course_dir: Path | str) -> str | None:
    """Return the Z Code sessionId of the course's latest init run.

    Primary source: ``bridge/init.json`` (the init run's official --json
    document). Fallback: Z Code's own session db, matched by the session's
    ``directory`` == course workspace — the db row exists as soon as the
    session is created, even when the CLI process lingers without flushing
    stdout (observed with delegated standalone wrappers). Read-only.
    """
    course_dir = Path(course_dir)
    workspace = course_dir / DEFAULT_WORKSPACE
    init_files = sorted(
        course_dir.glob("bridge/init-*.json"), key=lambda p: p.stat().st_mtime
    )
    for init_file in reversed(init_files):  # newest first
        try:
            data = json.loads(init_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # partial/locked file from a running or killed process
        session_id = str(data.get("sessionId", "")).strip()
        if session_id:
            return session_id
    db = default_session_db()
    if not db.is_file():
        return None
    candidates = [str(workspace), workspace.as_posix()]
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=2)
        try:
            for candidate in candidates:
                row = con.execute(
                    "SELECT id FROM session WHERE directory = ?"
                    " ORDER BY time_created DESC LIMIT 1",
                    (candidate,),
                ).fetchone()
                if row:
                    return str(row[0])
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return None


def open_learning_session(course_dir: Path | str, session_id: str) -> int:
    """Open the course's learning session as an INTERACTIVE Z Code TUI.

    Audit-verified semantics (zcode 0.16.9): ``--resume <sessionId>``
    restores that exact persisted session in the full-screen TUI — the user
    answers questions there and the skill keeps calling teach-mcp. Spawned
    in a fresh terminal window (wt.exe when available).
    """
    cli = find_zcode_runtime()
    env = dict(os.environ)
    _ensure_zcode_provider_env(env, cli)
    workspace = Path(course_dir) / DEFAULT_WORKSPACE
    workspace.mkdir(parents=True, exist_ok=True)
    wt = shutil_which("wt.exe")
    runtime_argv = _runtime_argv(cli)
    _ensure_tui_runtime(runtime_argv[0], cli, env)
    final_argv = (
        [wt, "-d", str(workspace), *runtime_argv, "--resume", session_id]
        if wt
        else [*runtime_argv, "--resume", session_id]
    )
    creationflags = _DETACHED_FLAGS if wt else _NEW_CONSOLE_FLAGS
    from learning.diagnostics import log_marker

    log_marker(
        "ZCODE_TUI_SPAWN_ATTEMPT",
        course_id=Path(course_dir).resolve().name,
        session_id=session_id,
        argv=json.dumps(final_argv, ensure_ascii=False),
        provider_env=bool(env.get("ZCODE_BUILTIN_PROVIDER_CONFIG_FILE") and env.get("ZCODE_PERSONAL_PROVIDER_CONFIG_FILE")),
    )
    pid = _spawn_detached(final_argv, workspace, env, creationflags)
    log_marker("ZCODE_TUI_PROCESS_STARTED", pid=pid, kind="terminal_launcher")
    return pid


def _ensure_tui_runtime(node: str, cli: Path, env: dict) -> None:
    """Check the standalone package layout before opening a terminal."""
    if cli.suffix.lower() not in {".mjs", ".cmd"}:
        return  # an official zcode command supplies its own runtime
    tui_entry = cli.parent.parent / "agent" / "node_modules" / "@zcode" / "tui" / "dist" / "index.js"
    native = cli.parent.parent / "agent" / "node_modules" / "@mbears" / "opentui-core-win32-x64"
    if not tui_entry.is_file() or (sys.platform == "win32" and not native.is_dir()):
        from learning.diagnostics import log_marker

        log_marker("ZCODE_TUI_PREFLIGHT_FAILED", dependency="@zcode/tui")
        raise LaunchError(
            "ZCODE_TUI_RUNTIME_MISSING",
            "当前 Z Code 安装缺少交互式 TUI 依赖 @zcode/tui；课程会话已保存，但无法打开终端学习空间。",
        )
