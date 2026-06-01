"""
ui/tray.py
──────────
System tray icon for wshp.

Icons are built by loading assets/icon.png (the waveform logo) and
compositing a small coloured state-badge dot onto the bottom-right
corner for non-idle states.  This preserves the logo at all times
while still giving a clear at-a-glance status indicator.

Badge colours
─────────────
  idle          – no badge (plain logo)
  loading       – steel blue   #4A90D9
  recording     – crimson      #E53935
  transcribing  – amber        #F9A825
  error         – deep orange  #BF360C
  paused        – blue-grey    #78909C
"""

from __future__ import annotations

import logging
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QMenu, QSystemTrayIcon

from assets import get_qicon, get_tray_icon

logger = logging.getLogger(__name__)


class TrayIcon(QSystemTrayIcon):
    """
    System tray icon for wshp.

    Usage
    -----
    ::

        tray = TrayIcon(config)
        tray.show()
    """

    # ── Signals ───────────────────────────────────────────────────────────────
    settings_requested = pyqtSignal()
    quit_requested = pyqtSignal()
    pause_toggled = pyqtSignal(bool)  # True = now paused
    mode_changed = pyqtSignal(str)  # "ptt" | "toggle" | "vad"
    overlay_toggled = pyqtSignal()

    def __init__(self, config, parent=None) -> None:
        super().__init__(parent)
        self._config = config
        self._state = "loading"
        self._paused = False

        # Build context menu
        self._menu = self._build_menu()
        self.setContextMenu(self._menu)

        # Initial icon + tooltip
        self.setIcon(get_tray_icon("loading"))
        self.setToolTip("wshp — Loading model…")

        self.activated.connect(self._on_activated)

    # ── Public API ─────────────────────────────────────────────────────────────

    def set_state(self, state: str, detail: str = "") -> None:
        """Update the tray icon and tooltip for *state*."""
        self._state = state
        self.setIcon(get_tray_icon(state))

        label_map = {
            "idle": "wshp — Ready",
            "loading": "wshp — Loading model…",
            "recording": "wshp — Recording…",
            "transcribing": "wshp — Transcribing…",
            "error": "wshp — Error",
            "paused": "wshp — Paused",
        }
        tip = label_map.get(state, "wshp")
        if detail:
            tip = f"{tip}  ({detail})"
        self.setToolTip(tip)
        self._sync_menu()

    def set_mode(self, mode: str) -> None:
        self._config.mode = mode
        self._sync_menu()

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        self._sync_menu()
        self.set_state("paused" if paused else "idle")

    def notify(self, title: str, message: str, duration_ms: int = 3000) -> None:
        """
        Show a Windows toast notification with the wshp logo as the icon.

        Passing a ``QIcon`` to ``showMessage`` (Qt 5.9 + / PyQt6) causes
        Windows to use that image in the notification balloon instead of
        the generic blue ℹ circle.
        """
        if not self.supportsMessages():
            return
        # Use the plain logo icon so the notification shows our branding,
        # not Python's default icon.
        self.showMessage(title, message, get_qicon(), duration_ms)

    # ── Context menu ───────────────────────────────────────────────────────────

    def _build_menu(self) -> QMenu:
        menu = QMenu()

        # Non-interactive header
        header = menu.addAction("wshp  –  Whisper Dictation")
        header.setEnabled(False)
        font = header.font()
        font.setBold(True)
        header.setFont(font)

        menu.addSeparator()

        self._status_action = menu.addAction("Status: Loading…")
        self._status_action.setEnabled(False)

        menu.addSeparator()

        # Mode sub-menu
        mode_menu = menu.addMenu("Mode")

        self._mode_ptt_action = mode_menu.addAction("Push-to-Talk  (hold key)")
        self._mode_ptt_action.setCheckable(True)
        self._mode_ptt_action.triggered.connect(lambda: self._on_mode_selected("ptt"))

        self._mode_toggle_action = mode_menu.addAction(
            "Toggle  (press once to start / again to stop)"
        )
        self._mode_toggle_action.setCheckable(True)
        self._mode_toggle_action.triggered.connect(
            lambda: self._on_mode_selected("toggle")
        )

        self._mode_vad_action = mode_menu.addAction("Auto-VAD  (always listening)")
        self._mode_vad_action.setCheckable(True)
        self._mode_vad_action.triggered.connect(lambda: self._on_mode_selected("vad"))

        menu.addSeparator()

        self._overlay_action = menu.addAction("Hide Overlay")
        self._overlay_action.triggered.connect(self._on_overlay_toggled)

        menu.addSeparator()

        self._pause_action = menu.addAction("Pause Hotkeys")
        self._pause_action.triggered.connect(self._on_pause_toggled)

        settings_action = menu.addAction("⚙  Settings…")
        settings_action.triggered.connect(self.settings_requested)

        menu.addSeparator()

        quit_action = menu.addAction("Quit wshp")
        quit_action.triggered.connect(self.quit_requested)

        return menu

    def _sync_menu(self) -> None:
        state_labels = {
            "idle": "Ready",
            "loading": "Loading model…",
            "recording": "Recording…",
            "transcribing": "Transcribing…",
            "error": "Error",
            "paused": "Paused",
        }
        self._status_action.setText(
            f"Status: {state_labels.get(self._state, self._state)}"
        )

        mode = self._config.mode
        self._mode_ptt_action.setChecked(mode == "ptt")
        self._mode_toggle_action.setChecked(mode == "toggle")
        self._mode_vad_action.setChecked(mode == "vad")

        self._pause_action.setText(
            "Resume Hotkeys" if self._paused else "Pause Hotkeys"
        )

        show_overlay = getattr(self._config, "show_overlay", True)
        self._overlay_action.setText("Hide Overlay" if show_overlay else "Show Overlay")

    # ── Slots ─────────────────────────────────────────────────────────────────

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.overlay_toggled.emit()

    def _on_mode_selected(self, mode: str) -> None:
        self._config.mode = mode
        self._sync_menu()
        self.mode_changed.emit(mode)
        logger.info("Mode changed to: %s", mode)

    def _on_pause_toggled(self) -> None:
        self._paused = not self._paused
        self._sync_menu()
        self.pause_toggled.emit(self._paused)

    def _on_overlay_toggled(self) -> None:
        self.overlay_toggled.emit()
        self._sync_menu()
