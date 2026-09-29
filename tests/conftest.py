"""Project-level pytest configuration with isolated repo-local temp roots."""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
for _plug in ("firefly_vision", "firefly_learning", "firefly_voice_chat", "firefly_bili_video"):
    _plug_dir = str(PROJECT_DIR / "plugins" / _plug)
    if Path(_plug_dir).is_dir() and _plug_dir not in sys.path:
        sys.path.insert(0, _plug_dir)

# voice 插件自带的验收测试（voice_plugin\）依赖真实服务在线：默认跳过，
# 避免 8 个环境性失败与潜在段错误污染全量结果。
collect_ignore_glob: list[str] = ["voice_plugin/*"]

from urllib.parse import urlparse
import urllib.request

# 插件实现缺失时（如 classic 空 plugins），跳过依赖这些实现的测试，
# 而不是让收集阶段报 ImportError——插件缺失只是功能缺失。
collect_ignore_glob: list[str] = []
if not (PROJECT_DIR / "plugins" / "firefly_voice_chat").is_dir():
    collect_ignore_glob += ["test_voice_*.py"]
if not (PROJECT_DIR / "plugins" / "firefly_vision" / "screen_vision").is_dir():
    collect_ignore_glob += [
        "test_screen_vision_*.py", "test_glm_vision_error_regression.py",
        "test_document_vision.py", "test_document_summary_v2.py",
        "test_companion_image_attachment.py", "test_companion_attachment_limits_v2.py",
        "test_companion_document_attachment.py",
    ]
if not (PROJECT_DIR / "plugins" / "firefly_bili_video").is_dir():
    collect_ignore_glob += ["test_document_router.py", "test_ask_button_companion.py"]
    collect_ignore_glob += ["test_capture_semantics.py", "test_companion_attachment.py"]

import pytest
import requests
from PySide6.QtCore import QEvent, QEventLoop
from PySide6.QtWidgets import QApplication

PROJECT_DIR = Path(__file__).resolve().parent.parent


def _is_loopback_url(value) -> bool:
    url = getattr(value, "full_url", value)
    try:
        host = (urlparse(str(url)).hostname or "").lower()
    except ValueError:
        return False
    return host in {"127.0.0.1", "localhost", "::1"}


@pytest.fixture(autouse=True)
def _block_unstubbed_external_http(monkeypatch: pytest.MonkeyPatch):
    """Fail closed before an unstubbed pytest request can leave the machine.

    Provider tests must inject/monkeypatch their transport. Loopback remains
    available for tests that deliberately own a local test server.
    """
    original_request = requests.sessions.Session.request
    original_urlopen = urllib.request.urlopen

    def guarded_request(session, method, url, *args, **kwargs):
        if _is_loopback_url(url):
            return original_request(session, method, url, *args, **kwargs)
        raise RuntimeError(
            "pytest blocked an unstubbed external HTTP request; inject a fake transport"
        ) from None

    def guarded_urlopen(url, *args, **kwargs):
        if _is_loopback_url(url):
            return original_urlopen(url, *args, **kwargs)
        raise RuntimeError(
            "pytest blocked an unstubbed external HTTP request; inject a fake transport"
        ) from None

    monkeypatch.setattr(requests.sessions.Session, "request", guarded_request)
    monkeypatch.setattr(urllib.request, "urlopen", guarded_urlopen)


def pytest_configure(config: pytest.Config) -> None:
    """Choose a fresh basetemp before pytest creates ``tmp_path_factory``.

    A unique directory avoids re-opening stale pytest roots whose Windows ACLs
    may no longer be readable. An explicit command-line ``--basetemp`` remains
    respected for callers that already provide their own isolated directory.
    """
    if config.option.basetemp is None:
        token = f"{os.getpid()}-{uuid.uuid4().hex[:12]}"
        config.option.basetemp = str(PROJECT_DIR / f".pytest_tmp_{token}")


@pytest.fixture(scope="session", autouse=True)
def _pytest_local_temproot(
    tmp_path_factory: pytest.TempPathFactory,
):
    """Point child processes at this run's basetemp and restore each value."""
    base = tmp_path_factory.getbasetemp()
    previous = {
        name: os.environ.get(name)
        for name in ("PYTEST_DEBUG_TEMPROOT", "TEMP", "TMP")
    }
    for name in previous:
        os.environ[name] = str(base)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@pytest.fixture(scope="session")
def project_tmp_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Return the unique repo-local temp directory for this test run."""
    return tmp_path_factory.getbasetemp()


def teardown_qt_widget(widget) -> None:
    """Close a Qt widget and drain the event loop so closeEvent handlers run.

    This prevents ``QThread: Destroyed while thread is still running`` errors
    at pytest teardown by ensuring that:
      1. closeEvent() fires and runs (e.g. _stop_ocr_thread, _stop_explain_thread)
      2. All queued signals are processed
      3. QApplication processes pending events
    """
    if widget is None:
        return
    widget.close()
    # Process closeEvent and any queued signal callbacks
    QApplication.processEvents()
    # Small yield to let thread.quit() / thread.wait() wiring finish
    QApplication.processEvents()
