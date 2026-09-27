# -*- coding: utf-8 -*-
"""VoiceServiceManager — 外部 Voice Module 服务的检测与显式启停。

职责边界：
- **只检测与启停**，不参与任何语音合成/播放链路；
- **绝不自动启动**（资源控制策略：服务由用户显式动作启动）；
- 停止优先作用于"本会话由本管理器启动的 PID"；回退路径按端口反查 PID，
  且校验目标确为 python 进程后才终止，避免误伤。

服务路径（root/python/script）来自语音配置的 service.* 字段
（用户插件配置，机器相关值不进仓库代码）；未配置时启动请求返回明确提示。
"""

from __future__ import annotations

import logging
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("firefly.voice.plugin")

_CHECK_CACHE_TTL_S = 3.0


class VoiceServiceManager:
    """Detect / start / stop the external voice service (127.0.0.1:8300)."""

    def __init__(self, config_loader: Callable[[], Any] | None = None):
        if config_loader is None:
            from voice_client.config import load_voice_config

            config_loader = load_voice_config
        self._config_loader = config_loader
        self._started_pid: int | None = None
        self._cache: dict[str, Any] = {"at": 0.0, "result": None}

    # -- detection -----------------------------------------------------------

    def _endpoint(self) -> tuple[str, int, str]:
        cfg = self._config_loader()
        url = str(getattr(cfg, "url", "http://127.0.0.1:8300"))
        host, port = "127.0.0.1", 8300
        try:
            from urllib.parse import urlparse

            parsed = urlparse(url)
            host = parsed.hostname or host
            port = int(parsed.port or port)
        except Exception:  # noqa: BLE001 - 解析失败用默认端点
            pass
        return host, port, url

    def check(self, *, force: bool = False) -> dict[str, Any]:
        """TCP 探测服务是否在线（3s 缓存；永不抛异常）。"""
        now = time.monotonic()
        if not force and self._cache["result"] is not None and now - self._cache["at"] < _CHECK_CACHE_TTL_S:
            return self._cache["result"]

        host, port, url = self._endpoint()
        started = time.monotonic()
        result: dict[str, Any]
        try:
            with socket.create_connection((host, port), timeout=0.5):
                latency_ms = int((time.monotonic() - started) * 1000)
                result = {"online": True, "url": url, "latency_ms": latency_ms, "detail": "online"}
        except OSError as exc:
            result = {"online": False, "url": url, "latency_ms": None,
                      "detail": f"offline ({type(exc).__name__})"}
        self._cache = {"at": now, "result": result}
        return result

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> dict[str, Any]:
        """显式启动服务（非阻塞）。未配置路径时返回 service_not_configured。"""
        if self.check(force=True)["online"]:
            return {"ok": True, "detail": "already_online", "pid": self._started_pid}

        cfg = self._config_loader()
        root = str(getattr(cfg, "service_root", "") or "").strip()
        python = str(getattr(cfg, "service_python", "") or "").strip()
        script = str(getattr(cfg, "service_script", "") or "api/server.py").strip()

        if not root or not python:
            return {
                "ok": False,
                "detail": "service_not_configured",
                "hint": "在用户插件配置 firefly_voice/config.yaml 的 voice.service 中填写 root/python",
            }
        if not Path(root).is_dir() or not Path(python).is_file():
            return {"ok": False, "detail": "service_path_invalid",
                    "hint": f"检查 service.root={root!r} / service.python={python!r}"}

        try:
            flags = 0
            if sys.platform == "win32":
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.Popen(
                [python, script],
                cwd=root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
            self._started_pid = proc.pid
            self._cache = {"at": 0.0, "result": None}
            log.info("voice service starting pid=%s cwd=%s", proc.pid, root)
            return {"ok": True, "detail": "starting", "pid": proc.pid,
                    "hint": "冷启动模型加载约 20-50s，稍后刷新状态"}
        except Exception as exc:  # noqa: BLE001 - 启动失败不影响主程序
            log.warning("voice service start failed: %s", exc)
            return {"ok": False, "detail": f"start_failed:{type(exc).__name__}"}

    def stop(self) -> dict[str, Any]:
        """停止服务：先本会话 PID，后端口反查（校验 python 进程）。"""
        pid = self._started_pid if self._alive(self._started_pid) else None
        source = "managed" if pid else None
        if pid is None:
            pid = self._pid_on_port()
            source = "port_lookup" if pid else None
        if pid is None:
            return {"ok": False, "detail": "no_service_found"}

        if source == "port_lookup" and not self._looks_like_python(pid):
            return {"ok": False, "detail": "refused_non_python_pid", "pid": pid,
                    "hint": "端口被非 python 进程占用，未执行终止"}

        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                               capture_output=True, timeout=10)
            else:
                import os

                os.kill(pid, 15)
            self._started_pid = None
            self._cache = {"at": 0.0, "result": None}
            log.info("voice service stopped pid=%s source=%s", pid, source)
            return {"ok": True, "detail": "stopped", "pid": pid, "source": source}
        except Exception as exc:  # noqa: BLE001
            log.warning("voice service stop failed pid=%s: %s", pid, exc)
            return {"ok": False, "detail": f"stop_failed:{type(exc).__name__}", "pid": pid}

    def restart(self) -> dict[str, Any]:
        """停 + 启（用户显式动作）。"""
        stopped = self.stop()
        started = self.start()
        return {"ok": bool(started.get("ok")), "stopped": stopped, "started": started}

    # -- internals -------------------------------------------------------------

    @staticmethod
    def _alive(pid: int | None) -> bool:
        if not pid:
            return False
        try:
            if sys.platform == "win32":
                out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                                     capture_output=True, text=True, timeout=5)
                return str(pid) in (out.stdout or "")
            import os

            os.kill(pid, 0)
            return True
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _looks_like_python(pid: int) -> bool:
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5)
            return "python" in (out.stdout or "").lower()
        except Exception:  # noqa: BLE001
            return False

    def _pid_on_port(self) -> int | None:
        """按端口反查监听 PID（仅 Windows netstat 路径）。"""
        if sys.platform != "win32":
            return None
        _, port, _ = self._endpoint()
        try:
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                                 capture_output=True, text=True, timeout=10)
            for line in (out.stdout or "").splitlines():
                if f":{port}" in line and "LISTENING" in line.upper():
                    parts = line.split()
                    if parts and parts[-1].isdigit():
                        return int(parts[-1])
        except Exception:  # noqa: BLE001
            return None
        return None
