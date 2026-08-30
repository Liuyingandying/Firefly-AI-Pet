"""VisionProvider abstraction.

Any implementation receives an in-memory ScreenFrame plus a vision
instruction and must return a ScreenObservation. Only vision providers are
allowed to touch image bytes.
"""

from abc import ABC, abstractmethod

from core.screen_vision.models import ScreenFrame, ScreenObservation

DEFAULT_VISION_INSTRUCTION = """你是视觉感知模块。
只描述屏幕上真实可见的信息。
不要给操作建议。
不要猜测看不清的内容。
尽量识别：
- 当前应用/窗口
- 页面大致结构
- 可见文本
- 错误/警告
- UI 控件
- 代码/终端状态
- 不确定内容

只返回一个 JSON 对象，不要返回其它文字，结构如下：
{
  "scene_summary": "...",
  "active_application": "...",
  "window_title": "...",
  "visible_text": [],
  "ui_elements": [],
  "warnings": [],
  "errors": [],
  "uncertain": []
}"""


class VisionProvider(ABC):
    """A multimodal model that turns a ScreenFrame into a ScreenObservation."""

    @abstractmethod
    def inspect(
        self,
        frame: ScreenFrame,
        instruction: str = DEFAULT_VISION_INSTRUCTION,
    ) -> ScreenObservation:
        """Describe what is visible in the frame. Never persist image data."""
        raise NotImplementedError
