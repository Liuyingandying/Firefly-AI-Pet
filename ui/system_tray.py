"""Firefly Windows system tray resident control.

Responsibilities:
- tray icon (``assets/firefly.ico``)
- left click -> ``controller.toggle_firefly()``
- context menu:
    显示/隐藏流萤 -> controller.toggle_firefly()
    设置        -> controller.show_settings()
    常用插件 >  -> dynamic submenu from the QuickToolsRegistry
    退出        -> controller.exit_application()  (the only real-exit path)

The tray never quits QApplication itself and never touches plugins: it only
calls back into the shell controller, and the plugin submenu reuses the
registry's existing ``open(id)``. Plugin windows are never hidden.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject
from PySide6.QtGui import QIcon
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from character import CharacterDisplayNames

_EMPTY_PLUGINS_ITEM = "（暂无已安装插件）"


class FireflySystemTray(QSystemTrayIcon):
    """The resident tray icon. Wired to a controller with the Firefly lifecycle.

    ``controller`` must expose:
        toggle_firefly()  show_firefly()  hide_firefly()
        show_settings()   exit_application()
    ``registry`` is a QuickToolsRegistry (dynamic 常用插件 source).
    """

    def __init__(
        self,
        controller: Any,
        icon_path: str,
        registry: Any | None = None,
        parent: QObject | None = None,
        *,
        display_names: CharacterDisplayNames | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._registry = registry
        self._display_names = display_names or CharacterDisplayNames()

        self.setIcon(QIcon(icon_path))
        self.setToolTip(self._display_names.brand_name)

        self._plugins_menu = QMenu("常用插件")
        self._menu = QMenu()

        self._characters: list = []
        self._current_character_id: str = ""
        self._on_switch = None

        self._build_menu()
        self.setContextMenu(self._menu)

        self.activated.connect(self._on_activated)
        self._menu.aboutToShow.connect(self._refresh_plugins_menu)

    # ------------------------------------------------------------ menu

    def _build_menu(self) -> None:
        toggle_action = self._menu.addAction(
            f"显示/隐藏{self._display_names.display_name}"
        )
        toggle_action.triggered.connect(lambda: self._controller.toggle_firefly())

        settings_action = self._menu.addAction("设置")
        settings_action.triggered.connect(lambda: self._controller.show_settings())

        self._menu.addMenu(self._plugins_menu)
        import_action = self._menu.addAction("导入角色卡")
        import_action.triggered.connect(
            lambda: self._controller.import_character_card()
        )
        manage_action = self._menu.addAction("角色管理")
        manage_action.triggered.connect(
            lambda: self._controller.open_character_manager()
        )

        if self._characters:
            self._character_menu = self._menu.addMenu("切换角色")
            self._populate_character_menu()
        self._menu.addSeparator()

        quit_action = self._menu.addAction("退出")
        quit_action.triggered.connect(lambda: self._controller.exit_application())

    # -------------------------------------------------------- character

    def set_characters(self, characters, current_character_id="", on_switch=None) -> None:
        """Supply the character picker.

        Updates the submenu in place when it already exists (safe inside a
        menu action handler); otherwise rebuilds the whole menu (startup).
        """
        self._characters = list(characters)
        self._current_character_id = str(current_character_id or "")
        if on_switch is not None:
            self._on_switch = on_switch
        if getattr(self, "_character_menu", None) is not None:
            self._populate_character_menu()
        else:
            self._rebuild_menu()

    def set_display_names(self, display_names) -> None:
        """Refresh character labels (tooltip + toggle item text)."""
        from character import CharacterDisplayNames

        self._display_names = display_names or CharacterDisplayNames()
        self.setToolTip(self._display_names.brand_name)
        for action in self._menu.actions():
            if str(action.text()).startswith("显示/隐藏"):
                action.setText(f"显示/隐藏{self._display_names.display_name}")
                break

    def _rebuild_menu(self) -> None:
        for action in list(self._menu.actions()):
            self._menu.removeAction(action)
        self._character_menu = None
        self._build_menu()

    def _populate_character_menu(self) -> None:
        menu = getattr(self, "_character_menu", None)
        if menu is None or not self._characters:
            return
        for action in list(menu.actions()):
            menu.removeAction(action)
        group = QActionGroup(menu)
        for meta in self._characters:
            label = (
                str(meta.display_name).strip()
                if str(getattr(meta, "display_name", "")).strip()
                else meta.character_id
            )
            action = QAction(f"{label}（{meta.character_id}）", menu, checkable=True)
            action.setChecked(meta.character_id == self._current_character_id)
            group.addAction(action)
            menu.addAction(action)
            if self._on_switch is not None:
                action.triggered.connect(
                    lambda _checked=False, cid=meta.character_id: self._on_switch(cid)
                )

    # ------------------------------------------------------------ plugins

    def _refresh_plugins_menu(self) -> None:
        """Rebuild 常用插件 from the existing registry (never hardcoded).

        The registry remains optional because the tray lifecycle must not own
        or initialize the plugin system. When the application provides a
        registry later, this reads it dynamically from the controller.
        """
        self._plugins_menu.clear()
        registry = self._registry or getattr(self._controller, "quick_tools_registry", None)
        if registry is None:
            registrations = ()
        else:
            registrations = registry.all()
        if not registrations:
            empty = self._plugins_menu.addAction(_EMPTY_PLUGINS_ITEM)
            empty.setEnabled(False)
            return
        for registration in registrations:
            manifest = registration.manifest
            action = self._plugins_menu.addAction(manifest.name)
            action.triggered.connect(
                lambda _checked=False, tool_id=manifest.id, current=registry: current.open(tool_id)
            )

    # ------------------------------------------------------------ activation

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # Left-click on Windows is ActivationReason.Trigger.
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._controller.toggle_firefly()

    # ------------------------------------------------------------ helpers

    @property
    def menu(self) -> QMenu:
        return self._menu

    @property
    def plugins_menu(self) -> QMenu:
        return self._plugins_menu
