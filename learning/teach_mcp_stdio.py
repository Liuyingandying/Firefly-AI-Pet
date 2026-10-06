"""Synchronous stdio MCP transport for the Firefly TutorTurn adapter.

This owns only a subprocess and JSON-RPC request IDs. Learning facts remain in
teach-mcp; use it as ``TeachMcpClient(transport.call_tool)``. The caller must
close the transport (or use a context manager) when its work is done.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from core.crash_diagnostics import log_thread
from learning.diagnostics import log_marker


class McpTransportError(RuntimeError):
    """The MCP process or JSON-RPC exchange failed."""


class StdioMcpTransport:
    """One live MCP session with serialized, bounded tool calls."""

    def __init__(
        self,
        command: str | Path,
        args: Sequence[str | Path] = (),
        *,
        env: Mapping[str, str] | None = None,
        cwd: str | Path | None = None,
        timeout_s: float = 60.0,
    ) -> None:
        if not str(command).strip() or timeout_s <= 0:
            raise ValueError("MCP command and a positive timeout are required")
        self._argv = [str(command), *(str(arg) for arg in args)]
        self._env = dict(os.environ) if env is None else {**os.environ, **env}
        self._cwd = str(cwd) if cwd is not None else None
        self._timeout_s = timeout_s
        self._process: subprocess.Popen[bytes] | None = None
        self._messages: queue.Queue[dict[str, Any] | BaseException] = queue.Queue()
        self._lock = threading.RLock()
        self._next_id = 0
        self._registered_tools: frozenset[str] = frozenset()

    def __enter__(self) -> StdioMcpTransport:
        self.start()
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    def start(self) -> None:
        with self._lock:
            if self._process is not None:
                raise McpTransportError("MCP transport is already started")
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            self._process = subprocess.Popen(
                self._argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=self._cwd,
                env=self._env,
                creationflags=flags,
            )
            threading.Thread(target=self._read_stdout, daemon=True).start()
            try:
                initialized = self._request("initialize", {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "firefly-learning", "version": "0.2"},
                })
                if not isinstance(initialized.get("protocolVersion"), str):
                    raise McpTransportError("MCP initialize returned no protocol version")
                self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                names, cursors = set(), set()
                cursor = None
                for _ in range(32):
                    page = self._request("tools/list", {"cursor": cursor} if cursor else {})
                    entries = page.get("tools")
                    if not isinstance(entries, list) or any(not isinstance(t, dict) or not isinstance(t.get("name"), str) for t in entries):
                        raise McpTransportError("MCP tool discovery returned an invalid registry")
                    names.update(t["name"] for t in entries)
                    cursor = page.get("nextCursor")
                    if not cursor:
                        break
                    if not isinstance(cursor, str) or cursor in cursors:
                        raise McpTransportError("MCP tool discovery cursor is invalid")
                    cursors.add(cursor)
                else:
                    raise McpTransportError("MCP tool discovery exceeded page limit")
                self._registered_tools = frozenset(names)
            except BaseException:
                self.close()
                raise

    def _read_stdout(self) -> None:
        log_thread("mcp_stdout_started")
        process = self._process
        assert process is not None and process.stdout is not None
        try:
            for line in process.stdout:
                message = json.loads(line.decode("utf-8", errors="strict"))
                if not isinstance(message, dict):
                    raise McpTransportError("MCP output was not a JSON-RPC object")
                self._messages.put(message)
        except (UnicodeError, ValueError, McpTransportError) as exc:
            self._messages.put(McpTransportError("MCP emitted invalid JSON-RPC output"))
        finally:
            self._messages.put(McpTransportError("MCP process closed its output"))

    def _send(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            raise McpTransportError("MCP process is unavailable")
        try:
            process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
            process.stdin.flush()
        except OSError as exc:
            raise McpTransportError("MCP request could not be sent") from exc

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        tool_name = params.get("name", "") if method == "tools/call" else ""
        log_marker("LEARNING_MCP", event="request", pid=self._process.pid,
                   request_id=request_id, method=method, tool=tool_name)
        self._send({
            "jsonrpc": "2.0", "id": request_id,
            "method": method, "params": params,
        })
        deadline = time.monotonic() + self._timeout_s
        while True:
            try:
                message = self._messages.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty as exc:
                raise McpTransportError(f"MCP {method} timed out") from exc
            if isinstance(message, BaseException):
                raise message
            if "method" in message:
                if "id" in message:
                    raise McpTransportError("server-initiated MCP requests are unsupported")
                continue  # notifications do not complete the request
            if message.get("id") != request_id:
                raise McpTransportError("MCP response ID did not match the request")
            if "error" in message:
                raise McpTransportError(f"MCP {method} returned a JSON-RPC error")
            result = message.get("result")
            if not isinstance(result, dict):
                raise McpTransportError("MCP response had no object result")
            log_marker("LEARNING_MCP", event="returned", pid=self._process.pid,
                       request_id=request_id, method=method, tool=tool_name,
                       is_error=bool(result.get("isError", False)))
            return result

    def call_tool(self, name: str, arguments: dict[str, str]) -> dict[str, Any]:
        with self._lock:
            if self._process is None:
                raise McpTransportError("MCP transport must be started first")
            if name not in self._registered_tools:
                raise McpTransportError("MCP tool is not registered by this server")
            return self._request("tools/call", {
                "name": name, "arguments": arguments,
            })

    def close(self) -> None:
        with self._lock:
            process = self._process
            self._process = None
            self._registered_tools = frozenset()
            if process is None:
                return
            if process.stdin is not None:
                process.stdin.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            if process.stdout is not None:
                process.stdout.close()
