"""CompanionPanel — right companion column of the console (UI V2).

Phase UI-4B-4: "AI 陪伴展示空间" — Anthropic 简洁工作区 + 星铁角色菜单展示
+ 流萤陪伴感. Replaces the old HUD-style status panel:

- top: companion avatar (assets/ui/avatar/liuying.png preferred, circular
  crop 140px, soft neutral shadow; silent fallback to a round placeholder) +
  流萤 + AI 智能伙伴 + ● 在线
- middle: three-line status card (当前模式 / 任务状态 / 陪伴时间)
- bottom: "With you, Always..." in small italic — no neon

No core imports, no business logic. All previous attribute names, setters
(``set_mode`` / ``set_task`` / ``set_companion_state``) and the constructor
signature are preserved for console compatibility.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from character import CharacterDisplayNames
from ui import theme
from ui.v2.context_status_card import ContextStatusCard

COMPANION_WIDTH = 300

_COMPANION_SLOGAN = "With you, Always..."
AVATAR_SIZE = 140
# Documented asset location (repo-relative; no absolute paths).
AVATAR_ASSET = (
    Path(__file__).resolve().parents[2] / "assets" / "ui" / "avatar" / "liuying.png"
)

# Shared circular-crop helper from the sibling chat module (same UI package).
from ui.v2.chat_view import _rounded_pixmap  # noqa: E402


def _circular_placeholder(size: int) -> QImage:
    """Soft flat disc used when no avatar asset exists (no neon gradient)."""
    image = QImage(size, size, QImage.Format_RGB32)
    image.fill(QColor(*theme.V2.CARD_BG_ASSISTANT[:3]))
    return image


class _CompanionAvatar(QFrame):
    """Circular companion avatar: real asset preferred, placeholder fallback.

    Soft neutral shadow only — no glow, no neon gradient.
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        size: int = AVATAR_SIZE,
        image_path: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)
        image_path = image_path if image_path is not None else AVATAR_ASSET

        image = None
        if Path(image_path).is_file():
            try:
                candidate = QImage(str(image_path))
                if not candidate.isNull():
                    image = candidate
            except Exception:  # asset problems never break the panel
                image = None
        if image is None:
            image = _circular_placeholder(size)

        picture = QLabel(self)
        picture.setGeometry(0, 0, size, size)
        picture.setPixmap(_rounded_pixmap(image, size))

        # Soft neutral shadow only (explicitly not a glow).
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(18)
        shadow.setOffset(0, 4)
        shadow.setColor(QColor(90, 80, 60, 34))
        self.setGraphicsEffect(shadow)


class CompanionPanel(QWidget):

    view_materials_requested = Signal()
    """Right column: companion showcase + status card + slogan."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        runner=None,
        display_names: CharacterDisplayNames | None = None,
    ) -> None:
        super().__init__(parent)
        self.runner = runner  # reserved for live state wiring (future phase)
        self._display_names = display_names or CharacterDisplayNames()
        self.setFixedWidth(COMPANION_WIDTH)

        # -- Top: companion showcase ---------------------------------------
        self.avatar = _CompanionAvatar(self)
        self.name_label = QLabel(self._display_names.display_name, self)
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
        online_row = QHBoxLayout()
        online_row.setSpacing(6)
        self._status_dot = QLabel("●", self)
        self._status_dot.setStyleSheet(
            f"color: {theme.css_color(theme.MINT_STATUS)}; background: transparent;"
            f"font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION}pt;"
        )
        self.companion_state_label = QLabel("在线", self)
        self.companion_state_label.setStyleSheet(
            f"color: rgba{theme.V2.TEXT_SECONDARY};"
            f"font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION}pt;"
        )
        online_row.addStretch(1)
        online_row.addWidget(self._status_dot)
        online_row.addWidget(self.companion_state_label)
        online_row.addStretch(1)

        # -- Middle: dynamic context status card (real facts only) ----------
        # The card renders sections dynamically — no "—" / "等待指令" / "00:00"
        # placeholders are ever synthesized. Phase 7B learning resources stay
        # as a small block below it (fed by set_learning_status).
        self.context_status = ContextStatusCard(
            self, display_names=self._display_names
        )

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
            f"  color: {theme.qcolor(theme.TEXT_SECONDARY)};"
            f"  background: transparent;"
            f"  border: 1px dashed rgba{theme.GLASS_BORDER};"
            f"  border-radius: 8px; padding: 3px 10px;"
            f"  font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION}pt;"
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

        # -- Bottom: quiet companion slogan --------------------------------
        self.slogan_label = QLabel(_COMPANION_SLOGAN, self)
        self.slogan_label.setAlignment(Qt.AlignCenter)
        self.slogan_label.setStyleSheet(
            f"color: rgba{theme.V2.TEXT_SECONDARY};"
            f"font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION}pt; font-style: italic;"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(10)
        root.addStretch(3)
        root.addWidget(self.avatar, 0, Qt.AlignHCenter)
        root.addWidget(self.name_label)
        root.addWidget(self.tag_label)
        root.addLayout(online_row)
        root.addStretch(1)
        root.addWidget(self.context_status)
        root.addWidget(self.resources_label)
        root.addWidget(self.view_materials_btn)
        root.addWidget(self.task_label)
        root.addStretch(2)
        root.addWidget(self.slogan_label)
        root.addStretch(1)

    # ---------------------------------------------------------------- hooks

    def set_mode(self, text: str) -> None:
        """Back-compat shim: mode is now driven by the context status card."""

    def set_project(self, text: str) -> None:
        """Back-compat shim: project is now driven by the context status card."""

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
        """Live 流萤状态 from RuntimeState (read-only)."""
        self.companion_state_label.setText(state)


__all__ = ["CompanionPanel", "COMPANION_WIDTH"]
