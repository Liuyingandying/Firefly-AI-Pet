"""AI 模型设置 — Provider Manager window (Provider Manager Phase 3B).

User-facing window over :class:`core.provider_manager.ProviderManager` and
:class:`core.credential_store.CredentialStore`:

- one card per credential-bearing provider (catalog-driven, via the manager);
- status dot + source label from the live status layer;
- key values are NEVER displayed or prefilled — a configured key renders as
  ``************`` and edits always start from an empty input;
- saving/deleting goes through the store, then ``reload_default_routers()``
  (hot provider rebuild), then a UI refresh — the runtime bus
  ``providers.updated`` event refreshes the window too, and opening the
  window always refreshes proactively (events are an optimization, never
  the source of truth).

This window never touches ``.env`` and never sees key values: it only calls
``CredentialStore.save/delete`` and the Phase 3A read-only query layer.
"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core.ai_router import reload_default_routers
from core.credential_store import default_store
from core.custom_providers import (
    CustomProviderError,
    delete_custom_provider,
    get_custom_provider,
    list_custom_providers,
    save_custom_provider,
)
from core.provider_manager import (
    SOURCE_CREDENTIAL_STORE,
    SOURCE_ENVIRONMENT,
    SOURCE_MISSING,
    ProviderManager,
)
from ui import theme

KEY_MASK = "************"

# 模型名称统一使用亮蓝紫色（深色窗口上清晰醒目，与品牌紫呼应）。
PROVIDER_NAME_COLOR = "#A78BFA"

_SOURCE_HINTS = {
    SOURCE_ENVIRONMENT: "环境变量优先级最高，本窗口修改不生效",
    SOURCE_CREDENTIAL_STORE: "本机存储",
    "dotenv": ".env 文件回退",
    SOURCE_MISSING: "未设置",
}


def _source_text(source: str) -> str:
    hint = _SOURCE_HINTS.get(source)
    return f"来源：{source}" + (f"（{hint}）" if hint else "")


class ProviderCard(QFrame):
    """One provider row: name, status dot, source, masked key, actions."""

    def __init__(
        self,
        *,
        provider_id: str,
        display_name: str,
        credential_key: str,
        configured: bool,
        source: str,
        enabled: bool,
        on_save: Callable[[str, str, str], None],
        on_delete: Callable[[str, str], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.provider_id = provider_id
        self.display_name = display_name
        self.credential_key = credential_key
        self.enabled = enabled

        self.setObjectName("providerCard")
        self.setStyleSheet(
            "QFrame#providerCard { border: 1px solid "
            f"{theme.css_color(theme.SEPARATOR_COLOR)}; border-radius: 8px; }}"
        )
        self._on_save = on_save
        self._on_delete = on_delete

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)

        header = QHBoxLayout()
        name_label = QLabel(display_name)
        name_label.setStyleSheet(
            f"color: {PROVIDER_NAME_COLOR}; "
            f"font-family: '{theme.FONT_FAMILY}'; font-weight: 600;"
        )
        header.addWidget(name_label)
        header.addStretch(1)
        self.status_label = QLabel()
        self.status_label.setObjectName("providerStatus")
        header.addWidget(self.status_label)
        layout.addLayout(header)

        self.source_label = QLabel()
        self.source_label.setObjectName("providerSource")
        self.source_label.setStyleSheet(
            f"color: {theme.css_color(theme.TEXT_SECONDARY)}; "
            f"font-family: '{theme.FONT_FAMILY}'; "
            f"font-size: {theme.FONT_SIZE_SMALL}pt;"
        )
        layout.addWidget(self.source_label)

        self.key_label = QLabel()
        self.key_label.setObjectName("providerKeyMask")
        self.key_label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        layout.addWidget(self.key_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.add_button = QPushButton("添加密钥")
        self.add_button.setObjectName(f"addKey-{provider_id}")
        self.replace_button = QPushButton("替换密钥")
        self.replace_button.setObjectName(f"replaceKey-{provider_id}")
        self.delete_button = QPushButton("删除密钥")
        self.delete_button.setObjectName(f"deleteKey-{provider_id}")
        self.add_button.clicked.connect(self._on_add_or_replace)
        self.replace_button.clicked.connect(self._on_add_or_replace)
        self.delete_button.clicked.connect(
            lambda: on_delete(self.provider_id, self.credential_key)
        )
        for button in (self.add_button, self.replace_button, self.delete_button):
            button.setCursor(Qt.PointingHandCursor)
            button.setStyleSheet(theme.link_button_style(button.objectName()))
            buttons.addWidget(button)
        layout.addLayout(buttons)

        self.update_state(configured, source, enabled)

    def update_state(self, configured: bool, source: str, enabled: bool) -> None:
        self._configured = configured
        self.enabled = enabled
        if configured:
            self.status_label.setText("● 已配置")
            self.status_label.setStyleSheet(
                f"color: {theme.css_color(theme.DOCK_STATUS_GREEN)}; font-weight: 600;"
            )
        else:
            self.status_label.setText("○ 未配置")
            self.status_label.setStyleSheet(
                f"color: {theme.css_color(theme.TEXT_SECONDARY)}; font-weight: 600;"
            )
        self.source_label.setText(_source_text(source))
        self.key_label.setText(KEY_MASK if configured else "")
        self.add_button.setVisible(not configured)
        self.replace_button.setVisible(configured)
        self.delete_button.setVisible(configured)
        self.setEnabled(enabled or True)  # deprecated rows stay visible for review
        if not enabled:
            self.status_label.setToolTip("该 Provider 已在目录中标记为弃用")

    # -- actions ---------------------------------------------------------
    def _on_add_or_replace(self) -> None:
        value = self._prompt_new_key()
        if value is None:
            return
        self._on_save(self.provider_id, self.credential_key, value)

    def _prompt_new_key(self) -> str | None:
        """Ask for a new key. Never prefills — stored values stay hidden."""
        title = "替换密钥" if self._configured else "添加密钥"
        text, ok = QInputDialog.getText(
            self,
            title,
            f"输入 {self.display_name} 的 API Key（{self.credential_key}）：",
            QLineEdit.Normal,
        )
        text = (text or "").strip()
        return text if ok and text else None


class CustomProviderDialog(QDialog):
    """新建/编辑一个自定义 OpenAI 兼容 Provider。

    编辑已保存条目时 API Key 留空表示保持不变；返回 ``entry()`` 供调用方
    写入凭据库（校验失败时由调用方弹窗提示并保留窗口数据）。
    """

    def __init__(self, entry: dict[str, Any] | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        entry = entry or {}
        self._provider_id = str(entry.get("id") or "")
        self._original_key = str(entry.get("api_key") or "")
        self.setWindowTitle("编辑自定义 Provider" if self._provider_id else "添加自定义 Provider")
        self.setModal(True)
        self.resize(440, 240)

        form = QFormLayout(self)
        form.setSpacing(8)

        self._name_edit = QLineEdit(str(entry.get("name") or ""))
        self._name_edit.setPlaceholderText("例如：硅基流动 / DeepSeek / 公司内网")
        form.addRow("显示名称", self._name_edit)

        self._url_edit = QLineEdit(str(entry.get("base_url") or ""))
        self._url_edit.setPlaceholderText("https://api.example.com/v1")
        form.addRow("Base URL", self._url_edit)

        self._model_edit = QLineEdit(str(entry.get("model") or ""))
        self._model_edit.setPlaceholderText("例如：deepseek-chat")
        form.addRow("模型名", self._model_edit)

        self._key_edit = QLineEdit()
        self._key_edit.setEchoMode(QLineEdit.Password)
        if self._provider_id:
            self._key_edit.setPlaceholderText("留空 = 保持已保存的 Key 不变")
        else:
            self._key_edit.setPlaceholderText("sk-...")
        form.addRow("API Key", self._key_edit)

        self._enabled_check = QCheckBox("启用（参与对话回退）")
        self._enabled_check.setChecked(bool(entry.get("enabled", True)))
        form.addRow(self._enabled_check)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("保存")
        ok.setDefault(True)
        ok.clicked.connect(self._on_save)
        buttons.addWidget(cancel)
        buttons.addWidget(ok)
        form.addRow(buttons)

    def _on_save(self) -> None:
        name = self._name_edit.text().strip()
        base_url = self._url_edit.text().strip()
        model = self._model_edit.text().strip()
        if not name or not base_url.startswith(("http://", "https://")) or not model:
            QMessageBox.warning(
                self, "信息不完整",
                "显示名称、Base URL（http/https 开头）与模型名都不能为空。",
            )
            return
        self.accept()

    def entry(self) -> dict[str, Any]:
        api_key = self._key_edit.text().strip()
        if not api_key and self._provider_id:
            api_key = self._original_key  # 编辑时留空 = 保持原 Key
        return {
            "id": self._provider_id or "",
            "name": self._name_edit.text().strip(),
            "base_url": self._url_edit.text().strip(),
            "model": self._model_edit.text().strip(),
            "api_key": api_key,
            "enabled": self._enabled_check.isChecked(),
        }


class CustomProviderCard(QFrame):
    """一条自定义 Provider：名称 / Base URL / 模型 / 启用状态 + 编辑/删除。"""

    def __init__(self, entry: dict[str, Any], *, on_edit, on_delete, parent=None) -> None:
        super().__init__(parent)
        self.provider_id = str(entry["id"])
        self.setObjectName("customProviderCard")
        self.setStyleSheet(
            "QFrame#customProviderCard { border: 1px solid "
            f"{theme.css_color(theme.SEPARATOR_COLOR)}; border-radius: 8px; }}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)

        header = QHBoxLayout()
        name_label = QLabel(
            f"{entry['name']}" + ("" if entry.get("enabled") else "（已停用）")
        )
        self._entry_name = entry["name"]
        name_label.setStyleSheet(
            f"color: {PROVIDER_NAME_COLOR}; "
            f"font-family: '{theme.FONT_FAMILY}'; font-weight: 600;"
        )
        header.addWidget(name_label)
        header.addStretch(1)
        state = QLabel("● 已启用" if entry.get("enabled") else "○ 已停用")
        state.setStyleSheet(
            f"color: {theme.css_color(theme.DOCK_STATUS_GREEN if entry.get('enabled') else theme.TEXT_SECONDARY)}; "
            "font-weight: 600;"
        )
        header.addWidget(state)
        layout.addLayout(header)

        detail = QLabel(f"{entry['base_url']}  ·  模型 {entry['model']}")
        detail.setWordWrap(True)
        detail.setStyleSheet(
            f"color: {theme.css_color(theme.TEXT_SECONDARY)}; "
            f"font-family: '{theme.FONT_FAMILY}'; "
            f"font-size: {theme.FONT_SIZE_SMALL}pt;"
        )
        layout.addWidget(detail)
        self._detail_text = detail.text()

        key_mask = QLabel(KEY_MASK)
        key_mask.setStyleSheet(
            f"color: {theme.css_color(theme.TEXT_SECONDARY)}; "
            f"font-size: {theme.FONT_SIZE_SMALL}pt;"
        )
        layout.addWidget(key_mask)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        edit_button = QPushButton("编辑")
        edit_button.clicked.connect(lambda: on_edit(self.provider_id))
        delete_button = QPushButton("删除")
        delete_button.clicked.connect(lambda: on_delete(self.provider_id))
        for button in (edit_button, delete_button):
            button.setCursor(Qt.PointingHandCursor)
            button.setStyleSheet(theme.link_button_style(button.objectName()))
            buttons.addWidget(button)
        layout.addLayout(buttons)

    def label_text(self) -> str:
        """Human-readable summary (name + endpoint + model) for tests."""
        return f"{self._entry_name} {self._detail_text}"


class ProviderManagerWindow(QDialog):
    """AI 模型设置 — credential cards over the Phase 3A status layer."""

    def __init__(
        self,
        manager: ProviderManager | None = None,
        store: Any | None = None,
        runtime_bus: Any | None = None,
        reload_fn: Callable[[], int] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("AI 模型设置")
        self.setModal(False)
        self.resize(520, 640)

        self._manager = manager if manager is not None else ProviderManager()
        self._store = store if store is not None else default_store()
        self._reload_fn = reload_fn if reload_fn is not None else reload_default_routers
        self._bus_unsubscribe: Callable[[], None] | None = None
        if runtime_bus is not None:
            self._bus_unsubscribe = runtime_bus.subscribe_event(self._on_bus_event)

        layout = QVBoxLayout(self)
        header = QLabel("AI 模型设置")
        header.setStyleSheet(
            f"color: {theme.css_color(theme.TEXT_PRIMARY)}; "
            f"font-family: '{theme.FONT_FAMILY}'; "
            f"font-size: 12pt; font-weight: 600;"
        )
        layout.addWidget(header)

        hint = QLabel(
            "密钥保存在本机用户数据目录，不会进入 Git；"
            "保存后自动热更新，无需重启。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(
            f"color: {theme.css_color(theme.TEXT_SECONDARY)}; "
            f"font-family: '{theme.FONT_FAMILY}'; "
            f"font-size: {theme.FONT_SIZE_SMALL}pt;"
        )
        layout.addWidget(hint)

        self._cards_layout = QVBoxLayout()
        self._cards_layout.setSpacing(8)

        body = QWidget()
        body.setLayout(self._cards_layout)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)

        self._cards: list[ProviderCard] = []
        self._custom_cards: list[CustomProviderCard] = []
        self.refresh()

    # ------------------------------------------------------------------
    # rendering
    # ------------------------------------------------------------------
    def refresh(self) -> None:
        """Rebuild all cards from the live status layer + custom entries."""
        for card in self._cards:
            card.setParent(None)
            card.deleteLater()
        self._cards = []
        self._custom_cards = []
        # 清空整个卡片布局（含历史 stretch），随后按固定顺序重建。
        while self._cards_layout.count():
            item = self._cards_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        for row in self._manager.list_providers():
            card = ProviderCard(
                provider_id=row["id"],
                display_name=row["display_name"],
                credential_key=row["credential_key"],
                configured=row["configured"],
                source=row["source"],
                enabled=row["enabled"],
                on_save=self._save_key,
                on_delete=self._delete_key,
            )
            self._cards_layout.addWidget(card)
            self._cards.append(card)

        # ---- 自定义 Provider（用户自配 OpenAI 兼容端点） ----
        divider = QFrame()
        divider.setFrameShape(QFrame.HLine)
        divider.setStyleSheet(
            f"color: {theme.css_color(theme.SEPARATOR_COLOR)};"
        )
        self._cards_layout.addWidget(divider)
        section = QLabel("自定义 Provider（任意 OpenAI 兼容端点）")
        section.setStyleSheet(
            f"color: {theme.css_color(theme.TEXT_SECONDARY)}; "
            f"font-family: '{theme.FONT_FAMILY}'; "
            f"font-size: {theme.FONT_SIZE_SMALL}pt;"
        )
        self._cards_layout.addWidget(section)

        for entry in list_custom_providers(self._store):
            card = CustomProviderCard(
                entry,
                on_edit=self._edit_custom_provider,
                on_delete=self._delete_custom_provider,
            )
            self._cards_layout.addWidget(card)
            self._custom_cards.append(card)

        add_button = QPushButton("＋ 添加自定义 Provider")
        add_button.setObjectName("addCustomProvider")
        add_button.setCursor(Qt.PointingHandCursor)
        add_button.setStyleSheet(theme.link_button_style(add_button.objectName()))
        add_button.clicked.connect(self._add_custom_provider)
        self._cards_layout.addWidget(add_button)

        self._cards_layout.addStretch(1)

    def cards(self) -> list[ProviderCard]:
        """Card handles for tests/tooling (id / status / labels)."""
        return list(self._cards)

    def custom_cards(self) -> list[CustomProviderCard]:
        """Custom-provider card handles for tests/tooling."""
        return list(self._custom_cards)

    # ------------------------------------------------------------------
    # flows
    # ------------------------------------------------------------------
    def _save_key(self, provider_id: str, credential_key: str, value: str) -> None:
        if not (value or "").strip():
            return  # cancelled / empty input must never clear a stored key
        self._store.save(credential_key, value.strip())
        self._reload_fn()
        self.refresh()

    def _delete_key(self, provider_id: str, credential_key: str) -> None:
        self._store.delete(credential_key)
        self._reload_fn()
        self.refresh()

    # ------------------------------------------------------------------
    # 自定义 Provider（用户自配 OpenAI 兼容端点）
    # ------------------------------------------------------------------
    def _add_custom_provider(self) -> None:
        dialog = CustomProviderDialog(parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            save_custom_provider(self._store, dialog.entry())
        except CustomProviderError as exc:
            QMessageBox.warning(self, "无法保存", str(exc))
            return
        self._reload_fn()
        self.refresh()

    def _edit_custom_provider(self, provider_id: str) -> None:
        entry = get_custom_provider(self._store, provider_id)
        if entry is None:
            self.refresh()
            return
        dialog = CustomProviderDialog(entry, parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            save_custom_provider(self._store, dialog.entry())
        except CustomProviderError as exc:
            QMessageBox.warning(self, "无法保存", str(exc))
            return
        self._reload_fn()
        self.refresh()

    def _delete_custom_provider(self, provider_id: str) -> None:
        confirm = QMessageBox.question(
            self,
            "删除自定义 Provider",
            "确定删除该自定义 Provider？删除后对话回退链不再使用它。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        delete_custom_provider(self._store, provider_id)
        self._reload_fn()
        self.refresh()

    # ------------------------------------------------------------------
    # runtime bus
    # ------------------------------------------------------------------
    def _on_bus_event(self, event: Any) -> None:
        if getattr(event, "kind", "") == "providers.updated":
            # deliver in the GUI thread event loop, never during fan-out
            QTimer.singleShot(0, self.refresh)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        if self._bus_unsubscribe is not None:
            self._bus_unsubscribe()
            self._bus_unsubscribe = None
        super().closeEvent(event)
