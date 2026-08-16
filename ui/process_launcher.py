"""Launch Claude/Codex safely from the Firefly Companion panel."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices

from core.agent_adapters import make_adapter
from core.agent_events import (
    STATUS_RECONNECTING,
    AgentEvent,
    AgentEventType,
    ErrorCategory,
    WORKING_STATUSES,
)
from core.handoff import validate_handoff_prompt
from core.quick_ask_metrics import (
    AskTelemetry,
    error_category,
    session_hash_prefix,
    workspace_token,
)
from core.session_manager import SessionManager, classify_stale_resume

from .quick_chat_protocol import (
    build_claude_args,
    build_codex_args,
)


PROJECT_DIR = Path(__file__).resolve().parent.parent
CLI_WRAPPER = PROJECT_DIR / "tools" / "run_cli.ps1"
CHATGPT_URL = QUrl("https://chatgpt.com/")

# Agents whose native interactive CLI supports a safe positional initial
# prompt (audited Phase 9C against claude 2.1.233 / codex-cli 0.147.0). Only
# these get a "Send to <agent>" handoff on the recommendation card; everything
# else keeps "Open only".
HANDOFF_TRANSPORT_VERIFIED = frozenset({"claude", "codex"})


def find_executable(name: str) -> str | None:
    """Resolve a CLI from PATH without guessing installation directories."""
    return shutil.which(name)


def _is_windows() -> bool:
    return os.name == "nt"


def _powershell_program() -> str:
    return shutil.which("powershell.exe") or shutil.which("powershell") or "powershell.exe"


def _wrapped_cli_args(executable: str, cli_args: list[str], *, interactive: bool) -> tuple[str, list[str]]:
    """Return a program/argv pair that safely runs .cmd/.bat CLIs on Windows.

    No user text is concatenated into a shell program. The fixed PowerShell
    wrapper receives every value as an argv item and invokes the CLI with
    argument splatting.
    """
    if not _is_windows():
        return executable, list(cli_args)

    args = ["-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass"]
    if interactive:
        args.append("-NoExit")
    else:
        args.append("-NonInteractive")
    args.extend(["-File", str(CLI_WRAPPER), executable, *cli_args])
    return _powershell_program(), args


def _claude_settings_env() -> dict[str, str]:
    """Read the user's global settings.json `env` block (proxy/auth config).

    Reuse-only: this mirrors the config Claude Code would load from the user
    source anyway, so an isolated child still routes through the local proxy
    even when the user source is not loaded. Values are config, not secrets
    (the proxy owns the real credential). Unreadable settings yield nothing.
    """
    try:
        settings_path = Path(os.path.expanduser("~")) / ".claude" / "settings.json"
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        env = data.get("env")
        if not isinstance(env, dict):
            return {}
        return {str(k): str(v) for k, v in env.items() if isinstance(v, (str, int, float))}
    except (OSError, ValueError):
        return {}


AGENT_DISPLAY_NAME = {"claude": "Claude", "codex": "Codex"}

# Semantic STATUS tokens from core.agent_events -> UI display text. The
# adapter layer never generates display language; this mapping is UI-owned.
STATUS_DISPLAY = {
    "connecting": "已连接，正在准备…",
    "thinking": "正在思考…",
    "generating": "正在生成回答…",
    "organizing": "正在整理回答…",
    "reading": "正在读取 / 检查项目…",
    "processing": "正在处理上下文…",
    "running_tool": "正在执行工具…",
    STATUS_RECONNECTING: "网络重试中…",
    "completed": "完成。",
    "error": "发生错误。",
}


class ProcessLauncher:
    @staticmethod
    def launch_agent(
        agent: str,
        workspace: Path | str,
        initial_prompt: str | None = None,
    ) -> tuple[bool, str]:
        """Open (or hand a task to) a native Agent in the given workspace.

        ``initial_prompt`` is optional. When present it is delivered as the
        native CLI's positional initial prompt (``claude <prompt>`` /
        ``codex <prompt>``) by launching the real native executable directly
        with a real argument vector — no PowerShell, no cmd.exe, no npm shim.
        That is the only transport on Windows that preserves embedded double
        quotes losslessly (PowerShell 5.1 and cmd.exe batch ``%*`` both corrupt
        them). ``launch_agent(agent, workspace)`` keeps the old Open-only
        behavior unchanged.
        """
        workspace = Path(workspace)
        if not workspace.exists() or not workspace.is_dir():
            return False, f"工作区不存在：{workspace}"

        agent = agent.lower()
        if agent not in {"codex", "claude"}:
            return False, f"不支持的 Agent：{agent}"

        exe = find_executable(agent)
        if not exe:
            display = "Codex" if agent == "codex" else "Claude Code"
            return False, f"找不到 {display} CLI。请确认它已安装并在 PATH 中。"

        if initial_prompt is not None:
            error = validate_handoff_prompt(agent, initial_prompt)
            if error is not None:
                return False, error
            program, argv = ProcessLauncher._handoff_command(agent, exe, initial_prompt)
            if Path(program).suffix.lower() in {".cmd", ".bat", ".ps1"}:
                # The npm shim could not be resolved to a real native target:
                # keep Open only rather than risk corrupting the prompt.
                return False, (
                    f"无法以原生方式安全交接给 {agent}（未找到原生 CLI）。请改用 Open only。"
                )
            ok, _pid = ProcessLauncher._spawn_native(program, argv, workspace)
        else:
            program, args = _wrapped_cli_args(exe, [], interactive=True)
            ok, _pid = ProcessLauncher._spawn_detached(program, args, workspace, use_wt=True)

        if not ok:
            return False, f"无法启动 {agent}。"
        if initial_prompt is not None:
            return True, f"已把任务发送给 {agent}。"
        return True, f"已在 {workspace} 打开 {agent}。"

    @staticmethod
    def _handoff_command(agent: str, exe: str, prompt: str) -> tuple[str, list[str]]:
        """Return (program, argv) that delivers ``prompt`` as one native argv item."""
        program, extra = ProcessLauncher._resolve_native(agent, exe)
        return program, [*extra, str(prompt)]

    @staticmethod
    def _resolve_native(agent: str, exe: str) -> tuple[str, list[str]]:
        """Resolve the real native CLI target for a prompt-bearing handoff.

        ``find_executable`` usually returns an npm shim (``.CMD``/``.ps1``).
        Neither PowerShell 5.1 nor cmd.exe ``%*`` can forward an argument
        containing embedded double quotes to a native process losslessly, so the
        handoff resolves the underlying ``claude.exe`` / ``node + codex.js``
        from the npm layout and passes argv to it directly. A real native
        executable is returned unchanged.
        """
        path = Path(exe)
        if path.suffix.lower() not in {".cmd", ".bat", ".ps1", ".psm1"}:
            return exe, []
        basedir = path.parent
        if agent == "claude":
            real = basedir / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
            if real.exists():
                return str(real), []
        elif agent == "codex":
            js = basedir / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
            if js.exists():
                node = basedir / "node.exe"
                if not node.exists():
                    node_path = shutil.which("node")
                    node = Path(node_path) if node_path else node
                if node.exists():
                    return str(node), [str(js)]
        return exe, []

    @staticmethod
    def _spawn_native(
        program: str, argv: list[str], workspace: Path
    ) -> tuple[bool, int]:
        """Launch a native executable detached with a real argv (no shell)."""
        return QProcess.startDetached(program, argv, str(workspace))

    @staticmethod
    def _spawn_detached(
        program: str, args: list[str], workspace: Path, *, use_wt: bool
    ) -> tuple[bool, int]:
        """Start ``program`` detached with the given argv (no shell string)."""
        if _is_windows() and use_wt:
            wt = find_executable("wt.exe") or find_executable("wt")
            if wt:
                return QProcess.startDetached(
                    wt, ["-d", str(workspace), program, *args], str(workspace)
                )
        return QProcess.startDetached(program, args, str(workspace))

    @staticmethod
    def open_chatgpt() -> tuple[bool, str]:
        ok = QDesktopServices.openUrl(CHATGPT_URL)
        return (ok, "已打开 ChatGPT。" if ok else "无法打开 ChatGPT。")


class QuickAskRunner(QObject):
    """Async Quick Ask runner with resumable per-workspace sessions.

    The runner still uses the user's authenticated local CLIs. No API key is
    requested or stored. Codex uses JSONL events; Claude uses stream-json.
    """

    started = Signal(str)
    partial = Signal(str)
    status = Signal(str)
    finished = Signal(str, int)
    failed = Signal(str)
    session_changed = Signal(str, str)
    telemetry = Signal(object)
    agent_event = Signal(object)

    def __init__(self, session_manager: SessionManager | None = None, parent: QObject | None = None):
        super().__init__(parent)
        self._process: QProcess | None = None
        self._agent: str | None = None
        self._workspace: Path | None = None
        self._persistent = True
        self._isolated = False
        self._sessions = session_manager if session_manager is not None else SessionManager()
        self._adapter = None
        self._last_status_display = ""
        self._stdout_buffer = ""
        self._raw_stdout: list[str] = []
        self._stderr_tail: list[str] = []
        self._streamed_text: list[str] = []
        self._final_text = ""
        self._cancelled = False
        self._last_protocol_error = ""
        self._resume_attempted = False
        self._stale_cleared = False
        self._telemetry: AskTelemetry | None = None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.state() != QProcess.NotRunning

    @property
    def resume_attempted(self) -> bool:
        """This turn tried to resume a native session (telemetry/handling)."""
        return self._resume_attempted

    @property
    def stale_cleared(self) -> bool:
        """This turn failed because the resumed id is invalid; record cleared."""
        return self._stale_cleared

    def session_id(self, agent: str, workspace: Path | str) -> str | None:
        return self._sessions.get_native_id(agent, workspace)

    def clear_session(self, agent: str | None = None, workspace: Path | str | None = None) -> None:
        self._sessions.clear(agent, workspace)

    def ask(
        self,
        agent: str,
        prompt: str,
        workspace: Path | str,
        *,
        effort: str = "low",
        persistent: bool = True,
        isolated: bool = False,
        model: str | None = None,
        sandbox: str = "read-only",
        reasoning_effort: bool = True,
        read_only_tools: bool = False,
        include_effort: bool = True,
    ) -> bool:
        if self.running:
            self.failed.emit("已有一个 Quick Ask 正在运行。")
            return False

        prompt = prompt.strip()
        workspace = Path(workspace)
        if not prompt:
            self.failed.emit("请输入问题。")
            return False
        if not workspace.exists() or not workspace.is_dir():
            self.failed.emit(f"工作区不存在：{workspace}")
            return False

        agent = agent.lower()
        exe = find_executable(agent)
        if not exe:
            display = "Codex" if agent == "codex" else "Claude Code" if agent == "claude" else agent
            self.failed.emit(f"找不到 {display} CLI。请确认它已安装并在 PATH 中。")
            return False

        session_id = self._sessions.get_native_id(agent, workspace) if persistent else None
        self._resume_attempted = bool(session_id)
        self._stale_cleared = False
        session_source = self._sessions.source(agent, workspace) if persistent else None
        self._telemetry = AskTelemetry(
            agent=agent,
            workspace=workspace_token(workspace),
            created_at=int(time.time() * 1000),
            session_exists=bool(session_id),
            session_hash=session_hash_prefix(session_id),
            isolated=isolated,
            session_source=session_source or ("new" if persistent else None),
            resume_attempted=bool(session_id),
        )
        self._telemetry.milestone_t0 = self._telemetry.created_at
        try:
            if agent == "codex":
                cli_args = build_codex_args(
                    prompt,
                    workspace,
                    effort=effort,
                    session_id=session_id,
                    persistent=persistent,
                    sandbox=sandbox,
                    reasoning_effort=reasoning_effort,
                )
            elif agent == "claude":
                cli_args = build_claude_args(
                    prompt,
                    effort=effort,
                    session_id=session_id,
                    persistent=persistent,
                    isolated=isolated,
                    model=model,
                    read_only_tools=read_only_tools,
                    include_effort=include_effort,
                )
            else:
                raise ValueError(f"Unsupported Quick Ask agent: {agent}")
            program, args = _wrapped_cli_args(exe, cli_args, interactive=False)
        except ValueError as exc:
            self.failed.emit(str(exc))
            self._finish_telemetry(None, exc_type="error")
            return False

        process = QProcess(self)
        process.setProgram(program)
        process.setArguments(args)
        process.setWorkingDirectory(str(workspace))
        if isolated and agent == "claude":
            merged = QProcessEnvironment.systemEnvironment()
            for key, value in _claude_settings_env().items():
                merged.insert(key, value)
            process.setProcessEnvironment(merged)
        process.setProcessChannelMode(QProcess.SeparateChannels)
        process.readyReadStandardOutput.connect(self._read_stdout)
        process.readyReadStandardError.connect(self._read_stderr)
        process.errorOccurred.connect(self._on_error)
        process.finished.connect(self._on_finished)
        process.started.connect(self._on_process_started)

        self._process = process
        self._agent = agent
        self._workspace = workspace
        self._persistent = persistent
        self._isolated = isolated
        self._adapter = make_adapter(agent)
        self._last_status_display = ""
        self._stdout_buffer = ""
        self._raw_stdout = []
        self._stderr_tail = []
        self._streamed_text = []
        self._final_text = ""
        self._last_protocol_error = ""
        self._cancelled = False

        self.started.emit(agent)
        if session_id:
            self.status.emit(f"正在继续 {agent.title()} 对话…")
        else:
            self.status.emit(f"正在连接 {agent.title()}…")
        process.start()
        return True

    def _record_milestone(self, which: str) -> None:
        if self._telemetry is None:
            return
        setattr(self._telemetry, f"milestone_{which}", int(time.time() * 1000))

    def _on_process_started(self) -> None:
        self._record_milestone("t1")
        if self._agent is not None:
            self.agent_event.emit(AgentEvent.make(self._agent, AgentEventType.STARTED))
        # `codex exec` takes its prompt positionally; stdin must reach EOF so
        # the CLI never blocks waiting for more input (9D.6-H1 managed exec).
        if self._agent == "codex" and self._process is not None:
            self._process.closeWriteChannel()

    def _finish_telemetry(self, exit_code: int | None, *, exc_type: str | None = None) -> None:
        if self._telemetry is None:
            return
        self._telemetry.milestone_t6 = int(time.time() * 1000)
        self._telemetry.process_exit = exit_code
        if exc_type is not None:
            self._telemetry.error_category = exc_type
        payload = self._telemetry.to_payload()
        self.telemetry.emit(self._telemetry)

    def _save_session(self, session_id: str | None) -> None:
        if not (self._persistent and session_id and self._agent and self._workspace):
            return
        previous = self._sessions.get_native_id(self._agent, self._workspace)
        self._sessions.set(self._agent, self._workspace, session_id)
        if previous != session_id:
            self.session_changed.emit(self._agent, session_id)

    def _consume_json_line(self, line: str) -> None:
        adapter = self._adapter
        if adapter is None or (self._agent is not None and adapter.agent_id != self._agent):
            # Compatibility shim: allow tests / legacy callers that set
            # _agent directly (without ask()) to still route through the
            # per-agent adapter. Re-create if the agent changed.
            adapter = make_adapter(self._agent)
            self._adapter = adapter
        if adapter is None:
            return
        events = adapter.feed_line(line)
        if not events:
            if line.strip():
                self._raw_stdout.append(line)
                self._raw_stdout = self._raw_stdout[-50:]
            return

        if self._telemetry is not None and self._telemetry.milestone_t2 is None:
            self._record_milestone("t2")
        for ev in events:
            self._handle_event(ev)

    def _handle_event(self, ev: AgentEvent) -> None:
        self.agent_event.emit(ev)
        tele = self._telemetry

        if ev.type == AgentEventType.SESSION:
            if tele is not None and tele.milestone_t3 is None:
                self._record_milestone("t3")
            self._save_session(ev.session_id)
        elif ev.type == AgentEventType.STATUS:
            display = f"{self._agent_display()} {STATUS_DISPLAY.get(ev.status, ev.status)}"
            if display != self._last_status_display:
                self._last_status_display = display
                self.status.emit(display)
        elif ev.type in (AgentEventType.TEXT_DELTA, AgentEventType.FINAL):
            if tele is not None and tele.milestone_t5 is None:
                self._record_milestone("t5")
            if ev.type == AgentEventType.TEXT_DELTA:
                if ev.text:
                    self._streamed_text.append(ev.text)
                    self.partial.emit(ev.text)
            else:
                self._final_text = ev.text or ""
        elif ev.type == AgentEventType.ERROR:
            if ev.error_code == ErrorCategory.PROTOCOL:
                # Tolerate malformed-line noise; never fail a turn on it.
                return
            self._last_protocol_error = ev.text or ev.error_code or "provider error"

        if (
            tele is not None
            and tele.milestone_t4 is None
            and (
                ev.type in (AgentEventType.TOOL, AgentEventType.TEXT_DELTA, AgentEventType.FINAL)
                or (ev.type == AgentEventType.STATUS and ev.status in WORKING_STATUSES)
            )
        ):
            self._record_milestone("t4")

    def _agent_display(self) -> str:
        return AGENT_DISPLAY_NAME.get(self._agent or "", (self._agent or "agent").title())

    def _read_stdout(self) -> None:
        if self._process is None:
            return
        text = bytes(self._process.readAllStandardOutput()).decode("utf-8", "replace")
        if not text:
            return
        self._stdout_buffer += text
        while "\n" in self._stdout_buffer:
            line, self._stdout_buffer = self._stdout_buffer.split("\n", 1)
            self._consume_json_line(line)

    def _read_stderr(self) -> None:
        if self._process is None:
            return
        text = bytes(self._process.readAllStandardError()).decode("utf-8", "replace")
        if text:
            self._stderr_tail.append(text)
            self._stderr_tail = self._stderr_tail[-8:]
            lowered = text.lower()
            if "reconnecting" in lowered or "timed out" in lowered:
                self._handle_event(
                    AgentEvent.make(
                        self._agent or "agent", AgentEventType.STATUS, status=STATUS_RECONNECTING
                    )
                )

    def _on_error(self, _error) -> None:
        if self._process is None:
            return
        detail = self._process.errorString() or "Quick Ask 进程启动失败。"
        self.agent_event.emit(
            AgentEvent.make(
                self._agent or "agent",
                AgentEventType.ERROR,
                text=detail,
                error_code=ErrorCategory.PROCESS_START,
            )
        )
        self.failed.emit(detail)

    def _on_finished(self, exit_code: int, _exit_status) -> None:
        if self._stdout_buffer.strip():
            self._consume_json_line(self._stdout_buffer)
        self._stdout_buffer = ""

        agent = self._agent or "agent"
        streamed = "".join(self._streamed_text).strip()
        raw = "\n".join(self._raw_stdout).strip()
        stdout = (self._final_text or streamed or raw).strip()
        stderr = "".join(self._stderr_tail).strip()

        if self._cancelled:
            self.agent_event.emit(
                AgentEvent.make(agent, AgentEventType.CANCELLED, error_code=ErrorCategory.CANCELLED)
            )
            self.status.emit("已取消。")
            stdout = ""
        else:
            if self._adapter is not None:
                for ev in self._adapter.finalize(exit_code):
                    self.agent_event.emit(ev)
            if exit_code == 0 and not self._last_protocol_error:
                self.status.emit("完成。")
                if not stdout:
                    stdout = "（Agent 已完成，但没有返回文本输出。）"
            else:
                message = f"{agent.title()} 返回错误（exit {exit_code}）。"
                if self._last_protocol_error:
                    message += "\n" + self._last_protocol_error[-1000:]
                elif stderr:
                    message += "\n" + stderr[-1200:]
                # Only a provably-invalid resume id clears the persisted
                # record. Network / auth / transport failures must not.
                # The provider's specific message may arrive via the
                # structured error OR stderr, so check both.
                if self._resume_attempted and classify_stale_resume(
                    " ".join(x for x in (self._last_protocol_error, stderr) if x)
                ):
                    self._stale_cleared = True
                    if self._telemetry is not None:
                        self._telemetry.resume_fallback = True
                    if self._agent is not None and self._workspace is not None:
                        self._sessions.clear(self._agent, self._workspace)
                self.failed.emit(message)

        self._finish_telemetry(exit_code, exc_type=error_category(self._last_protocol_error))
        self.finished.emit(stdout, exit_code)
        if self._process is not None:
            self._process.deleteLater()
        self._process = None
        self._agent = None
        self._workspace = None
        self._adapter = None

    def stop(self) -> None:
        if not self.running or self._process is None:
            return
        self._cancelled = True
        if self._telemetry is not None:
            self._telemetry.cancel_requested = True
        pid = int(self._process.processId())
        if _is_windows() and pid > 0:
            QProcess.startDetached("taskkill.exe", ["/PID", str(pid), "/T", "/F"])
        else:
            self._process.terminate()
            QTimer.singleShot(1500, self._kill_if_running)
        self.status.emit("正在停止…")

    def _kill_if_running(self) -> None:
        if self.running and self._process is not None:
            self._process.kill()

    def shutdown(self) -> None:
        if self.running:
            self.stop()
