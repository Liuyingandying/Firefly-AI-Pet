"""CompanionPanel — right companion column of the console (UI V2).

ui美化版（「流萤」发光紫视觉语言 · 强化 Pet 宠物感）:

- 角色展示区: 半身「立绘」舞台 — 播放 assets/animations/ 下与桌面端同步的
  7 种状态动画 (idle/working/running/waiting/waving/failed/jumping),
  底部自绘淡紫色光晕渐变, 模拟萤火虫发光感, 成为右侧视觉锚点;
  无素材时静默回退为发光 orb
- 状态指示灯: thinking/working=紫呼吸, success=绿常亮, error=红闪烁
  (ui.v2.motion.StatusLamp)
- 状态短句: 随状态切换（待机「在等你哦」/ 思考「让我想想…」）, 搭配
  萤火虫 ✦ 图标置于角色下方
- 中部: ContextStatusCard 轻玻璃拟态（见 context_status_card.py）

No core imports, no business logic. All previous attribute names, setters
(``set_mode`` / ``set_task`` / ``set_companion_state``) and the constructor
signature are preserved for console compatibility; ``set_runtime_state``
(state id) additionally drives the character animation / lamp / mood line.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QMovie, QRadialGradient
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ui import theme
from ui.v2 import motion
from ui.v2.context_status_card import ContextStatusCard

COMPANION_WIDTH = 288

_COMPANION_SLOGAN = "With you, Always..."
AVATAR_SIZE = 140
# Documented asset location (repo-relative; no absolute paths).
AVATAR_ASSET = theme.repo_root() / "assets" / "ui" / "avatar" / "liuying.png"
ANIMATIONS_DIR = theme.repo_root() / "assets" / "animations"

_STAGE_HEIGHT = 236  # v8: 角色立绘舞台放大（原 200）

# 桌面端 7 种状态 → 动画素材映射（缺失文件静默回退）。
_STATE_GIFS = {
    "idle": "idle.gif",
    "thinking": "review.gif",
    "working": "review.gif",
    "tool_running": "running.gif",
    "waiting_input": "waiting.gif",
    "success": "waving.gif",
    "error": "failed.gif",
    "jumping": "jumping.gif",
}

# 状态 → 宠物感短句（slogan 随状态切换）。
_MOOD_LINES = {
    "idle": "在等你哦",
    "thinking": "让我想想…",
    "working": "让我想想…",
    "tool_running": "跑腿中…",
    "waiting_input": "等你回复",
    "success": "完成啦",
    "error": "呀，出错了",
    "jumping": "开心得跳起来",
}

# Shared circular-crop helper from the sibling chat module (same UI package).
from ui.v2.chat_view import _rounded_pixmap  # noqa: E402


def _circular_placeholder(size: int) -> QImage:
    """Soft flat disc used when no avatar asset exists (no neon gradient)."""
    image = QImage(size, size, QImage.Format_RGB32)
    image.fill(QColor(*theme.V2.CARD_BG_ASSISTANT[:3]))
    return image


class _CharacterStage(QWidget):
    """角色舞台: 底部淡紫色光晕 + 状态动画（QMovie）.

    paintEvent 在底部画一枚椭圆径向渐变光晕（萤火虫发光感）; 动画以
    透明 QLabel 叠加在光晕上方。素材缺失时回退为静态圆形头像。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(COMPANION_WIDTH - 24, _STAGE_HEIGHT)
        self._avatar_fallback: QImage | None = None
        if AVATAR_ASSET.is_file():
            try:
                candidate = QImage(str(AVATAR_ASSET))
                if not candidate.isNull():
                    self._avatar_fallback = candidate
            except Exception:  # asset problems never break the panel
                self._avatar_fallback = None
        if self._avatar_fallback is None:
            self._avatar_fallback = _circular_placeholder(AVATAR_SIZE)

        self._movie_label = QLabel(self)
        self._movie_label.setAttribute(Qt.WA_TranslucentBackground)
        self._movie_label.setAlignment(Qt.AlignHCenter | Qt.AlignBottom)
        self._movie_label.setGeometry(0, 24, self.width(), self.height() - 24)
        self._movie = QMovie(self._movie_label)
        self._movie.setCacheMode(QMovie.CacheAll)
        self._movie_label.setMovie(self._movie)
        self.set_state("idle")

    def set_state(self, state: str) -> None:
        """切换状态动画（素材缺失时静默保持现状）."""
        gif = ANIMATIONS_DIR / _STATE_GIFS.get(state, "idle.gif")
        if not gif.is_file():
            self._show_fallback()
            return
        try:
            self._movie.stop()
            self._movie.setFileName(str(gif))
            if self._movie.isValid():
                size = self._movie.frameRect().size() if not self._movie.frameRect().isNull() else QSize(160, 160)
                scale = min(
                    (self.height() - 32) / max(1, size.height()),
                    (self.width() - 32) / max(1, size.width()),
                    1.0,
                )
                self._movie.setScaledSize(
                    QSize(int(size.width() * scale), int(size.height() * scale))
                )
                self._movie_label.setMovie(self._movie)
                self._movie.start()
                return
        except Exception:  # animation problems never break the panel
            pass
        self._show_fallback()

    def _show_fallback(self) -> None:
        self._movie.stop()
        self._movie_label.setPixmap(
            _rounded_pixmap(self._avatar_fallback, AVATAR_SIZE)
        )

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        # 底部淡紫色光晕（模拟萤火虫发光）
        glow = QRadialGradient(
            self.width() / 2, self.height() - 18, self.width() * 0.52
        )
        glow.setColorAt(0.0, QColor(155, 109, 255, 110))
        glow.setColorAt(0.45, QColor(155, 109, 255, 38))
        glow.setColorAt(1.0, QColor(155, 109, 255, 0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(
            int(self.width() / 2 - self.width() * 0.52),
            int(self.height() - 18 - self.width() * 0.30),
            int(self.width() * 1.04),
            int(self.width() * 0.60),
        )


class CompanionPanel(QWidget):

    view_materials_requested = Signal()
    """Right column: character stage + glass status card + mood slogan."""

    def __init__(self, parent: QWidget | None = None, *, runner=None) -> None:
        super().__init__(parent)
        self.runner = runner  # reserved for live state wiring (future phase)
        self.setFixedWidth(COMPANION_WIDTH)

        # -- Top: character stage (立绘 + 光晕 + 状态动画) --------------------
        self.stage = _CharacterStage(self)
        self.name_label = QLabel("流萤", self)
        self.name_label.setAlignment(Qt.AlignCenter)
        self.name_label.setStyleSheet(
            f"color: rgba{theme.V2.TEXT_MAIN}; font-weight: 700;"
            f"font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_TITLE}pt;"
        )
        self.tag_label = QLabel("AI 智能伙伴", self)
        self.tag_label.setAlignment(Qt.AlignCenter)
        self.tag_label.setStyleSheet(
            f"color: rgba{theme.V2.TEXT_SECONDARY};"
            f"font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_BODY}pt;"
        )

        # 状态行: 指示灯 + 状态文字（呼吸/常亮/闪烁联动）
        online_row = QHBoxLayout()
        online_row.setSpacing(6)
        self._status_lamp = motion.StatusLamp("idle")
        self.companion_state_label = QLabel("在线", self)
        self.companion_state_label.setStyleSheet(
            f"color: rgba{theme.V2.TEXT_SECONDARY};"
            f"font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION}pt;"
        )
        online_row.addStretch(1)
        online_row.addWidget(self._status_lamp)
        online_row.addWidget(self.companion_state_label)
        online_row.addStretch(1)

        # 状态短句（宠物感）: 萤火虫 ✦ + 随状态切换的文案, 置于角色下方
        mood_row = QHBoxLayout()
        mood_row.setSpacing(6)
        mood_icon = QLabel(self)
        mood_icon.setPixmap(theme.vector_pixmap("sparkle", theme.V2.PRIMARY, 12))
        self._mood_label = QLabel(_MOOD_LINES["idle"], self)
        self._mood_label.setStyleSheet(
            f"color: rgba{theme.V2.PRIMARY};"
            f"font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION}pt;"
            f"font-style: italic; background: transparent;"
        )
        mood_row.addStretch(1)
        mood_row.addWidget(mood_icon)
        mood_row.addWidget(self._mood_label)
        mood_row.addStretch(1)

        self.slogan_label = QLabel(_COMPANION_SLOGAN, self)
        self.slogan_label.setAlignment(Qt.AlignCenter)
        self.slogan_label.setStyleSheet(
            f"color: rgba{theme.V2.TEXT_SECONDARY};"
            f"font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION}pt; font-style: italic;"
        )

        # -- Middle: dynamic context status card (real facts only) ----------
        # The card renders sections dynamically — no "—" / "等待指令" / "00:00"
        # placeholders are ever synthesized. Phase 7B learning resources stay
        # as a small block below it (fed by set_learning_status).
        self.context_status = ContextStatusCard(self)

        # Phase 7B: bound learning resources (display only — labels come
        # verbatim from LearningResource.label()).
        self.resources_label = QLabel("", self)
        self.resources_label.setWordWrap(True)
        self.resources_label.setStyleSheet(
            f"color: rgba{theme.V2.TEXT_SECONDARY};"
            f"font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION}pt; background: transparent;"
        )
        self.view_materials_btn = QPushButton("查看材料", self)
        self.view_materials_btn.setCursor(Qt.PointingHandCursor)
        self.view_materials_btn.setStyleSheet(
            f"QPushButton {{"
            f"  color: rgba{theme.V2.TEXT_SECONDARY};"
            f"  background: transparent;"
            f"  border: 1px dashed rgba{theme.V2.PRIMARY_GLOW};"
            f"  border-radius: {theme.V2.RADIUS_CARD - 2}px; padding: 3px 10px;"
            f"  font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION}pt;"
            f"}}"
            f"QPushButton:hover {{"
            f"  color: rgba{theme.V2.PRIMARY};"
            f"  border: 1px dashed rgba{theme.V2.PRIMARY};"
            f"}}"
        )
        self.view_materials_btn.clicked.connect(self._on_view_materials)
        self.resources_label.setVisible(False)
        self.view_materials_btn.setVisible(False)

        # 最近状态（如 camera observation）——真实值才显示，默认隐藏。
        self.task_label = QLabel("", self)
        self.task_label.setWordWrap(True)
        self.task_label.setStyleSheet(
            f"color: rgba{theme.V2.TEXT_MAIN};"
            f"font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION}pt; background: transparent;"
        )
        self.task_label.setVisible(False)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)
        root.addStretch(2)
        root.addWidget(self.stage, 0, Qt.AlignHCenter)
        root.addWidget(self.name_label)
        root.addWidget(self.tag_label)
        root.addLayout(online_row)
        root.addLayout(mood_row)
        root.addWidget(self.slogan_label)
        root.addStretch(1)
        root.addWidget(self.context_status)
        root.addWidget(self.resources_label)
        root.addWidget(self.view_materials_btn)
        root.addWidget(self.task_label)
        root.addStretch(2)

    # ---------------------------------------------------------------- hooks

    def set_mode(self, text: str) -> None:
        """Back-compat shim: mode is now driven by the context status card."""

    def set_project(self, text: str) -> None:
        """Back-compat shim: project is now driven by the context status card."""

    def set_runtime_state(self, state: str) -> None:
        """状态联动入口（state id）: 立绘动画 + 指示灯 + 状态短句."""
        state = (state or "").strip().lower()
        self.stage.set_state(state)
        self._status_lamp.set_state(state)
        self._mood_label.setText(_MOOD_LINES.get(state, _MOOD_LINES["idle"]))

    def set_learning_status(self, card) -> None:
        """Phase 7B: bound learning resources (display only).

        Labels come verbatim from LearningResource.label(); nothing here
        computes a chapter / focus / next step.
        """
        labels = card.resource_labels() if card is not None else ()
        self.resources_label.setText("\n".join(labels))
        has_resources = bool(labels)
        self.resources_label.setVisible(has_resources)
        self.view_materials_btn.setVisible(has_resources)

    def _on_view_materials(self) -> None:
        """Relay the button to the console (which owns the viewer action)."""
        self.view_materials_requested.emit()

    def set_task(self, text: str) -> None:
        """Live 最近状态 updates (camera observation etc.); hidden when empty."""
        text = (text or "").strip()
        self.task_label.setText(text)
        self.task_label.setVisible(bool(text))

    def set_companion_state(self, state: str) -> None:
        """Live 流萤状态 label from RuntimeState (read-only)."""
        self.companion_state_label.setText(state)


__all__ = ["CompanionPanel", "COMPANION_WIDTH"]
