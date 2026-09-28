# -*- coding: utf-8 -*-
"""一次性：直连插件 failover 复现 ProviderHTTPError 逃逸。用后即删。"""
import sys
from pathlib import Path

sys.path.insert(0, r"C:\Users\LENOVO\Desktop\流萤\Firefly-AI-Pet（2）")
sys.path.insert(0, r"C:\Users\LENOVO\Desktop\流萤\Firefly-AI-Pet（2）\plugins\firefly_vision")

from core.screen_vision.provider_errors import ProviderHTTPError
from screen_vision.failover import FailoverVisionProvider, is_transient


class FakeVision:
    name = "primary-vision"

    def __init__(self, error):
        self._error = error

    def inspect(self, frame, instruction=None):
        raise self._error


class FallbackVision:
    name = "fallback-vision"

    def inspect(self, frame, instruction=None):
        class _O:
            scene_summary = "fallback scene"

        return _O()


e = ProviderHTTPError(500, "boom")
print("is_transient:", is_transient(e))

provider = FailoverVisionProvider(primary=FakeVision(e), fallback=FallbackVision())
try:
    r = provider.inspect(None)
    print("结果:", getattr(r, "scene_summary", r))
except Exception as exc:
    print("逃逸异常:", type(exc).__name__, str(exc)[:100])
