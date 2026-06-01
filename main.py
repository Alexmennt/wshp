"""
main.py
───────
Entry point for wshp — Local Whisper Dictation for Windows.

Start-up sequence
─────────────────
1. Configure logging.
2. Create QApplication (no window shown yet).
3. Load persisted configuration.
4. Instantiate AppController (owns audio, VAD, transcription, hotkeys).
5. Instantiate TrayIcon and Overlay.
6. Wire all Qt signals together.
7. Start the AppController (audio stream + model loading in background).
8. Enter the Qt event loop.
"""

from __future__ import annotations

import logging
import sys
import traceback
from typing import Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import QApplication, QMessageBox

from app_controller import (
    STATE_ERROR,
    STATE_IDLE,
    STATE_LOADING,
    STATE_PAUSED,
    STATE_RECORDING,
    STATE_TRANSCRIBING,
    AppController,
)
from config import Config, load_config, save_config
from ui.overlay import Overlay
from ui.settings import SettingsDialog
from ui.tray import TrayIcon

# ── Logging setup ──────────────────────────────────────────────────────────────


def _configure_logging(level: int = logging.INFO) -> None:
    """
    Set up root logger to write to stdout and to a rolling log file in ~/.wshp/.
    """
    import os

    log_dir = os.path.join(os.path.expanduser("~"), ".wshp")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "wshp.log")

    fmt = "%(asctime)s [%(levelname)-8s] %(name)s — %(message)s"
    datefmt = "%H:%M:%S"

    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
    ]

    try:
        from logging.handlers import RotatingFileHandler

        fh = RotatingFileHandler(
            log_path,
            maxBytes=5 * 1024 * 1024,  # 5 MB
            backupCount=2,
            encoding="utf-8",
        )
        handlers.append(fh)
    except Exception:
        pass  # Non-fatal — console logging still works

    logging.basicConfig(level=level, format=fmt, datefmt=datefmt, handlers=handlers)

    # Quiet noisy third-party loggers
    for noisy in ("httpx", "urllib3", "filelock", "huggingface_hub", "numba"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


logger = logging.getLogger(__name__)


# ── Unhandled exception hook ───────────────────────────────────────────────────


def _install_exception_hook() -> None:
    """Log and show a dialog for any unhandled exception instead of crashing silently."""

    def hook(exc_type, exc_value, exc_tb):
        msg = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        logger.critical("Unhandled exception:\n%s", msg)
        # Try to show a Qt message box if the app is running
        try:
            app = QApplication.instance()
            if app is not None:
                dlg = QMessageBox()
                dlg.setWindowTitle("wshp — Unhandled Error")
                dlg.setIcon(QMessageBox.Icon.Critical)
                dlg.setText("An unexpected error occurred.")
                dlg.setDetailedText(msg)
                dlg.exec()
        except Exception:
            pass
        # Call the default hook so Python still prints to stderr
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = hook


# ── Main window-manager class ──────────────────────────────────────────────────


class WshpApp:
    """
    Top-level object that owns the AppController, TrayIcon, and Overlay.

    It wires all Qt signals together and manages the settings dialog lifecycle.
    There is deliberately no QMainWindow — the app lives entirely in the tray.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._settings_dialog: Optional[SettingsDialog] = None

        # ── Core controller ────────────────────────────────────────────────
        self._controller = AppController(config)

        # ── UI components ──────────────────────────────────────────────────
        self._tray = TrayIcon(config)
        self._overlay = Overlay(config)

        # ── Wire signals ───────────────────────────────────────────────────
        self._connect_signals()

        logger.debug("WshpApp constructed.")

    # ── Start / stop ──────────────────────────────────────────────────────────

    def start(self) -> None:
        """Show the tray icon, overlay, and kick off background work."""
        self._tray.show()

        # The overlay starts hidden; it fades in when recording begins.
        # (The Overlay constructor hides itself in idle state.)

        # Start the AppController — this opens the audio stream and begins
        # loading the Whisper model in a background QThread.
        self._controller.start()

        logger.info("wshp started — waiting for model to load.")

    def stop(self) -> None:
        """Clean shutdown: stop the controller, hide tray."""
        logger.info("Stopping wshp …")
        self._controller.stop()
        self._tray.hide()
        logger.info("wshp stopped.")

    # ── Signal wiring ─────────────────────────────────────────────────────────

    def _connect_signals(self) -> None:
        """Connect AppController → UI and TrayIcon → AppController signals."""

        ctrl = self._controller
        tray = self._tray
        overlay = self._overlay

        # ── AppController → Overlay ────────────────────────────────────────
        ctrl.state_changed.connect(overlay.set_state)
        ctrl.transcription_ready.connect(overlay.set_preview_text)
        ctrl.recording_tick.connect(overlay.set_recording_time)

        # ── AppController → TrayIcon ───────────────────────────────────────
        ctrl.state_changed.connect(self._on_state_changed_tray)
        ctrl.model_info.connect(self._on_model_info)
        ctrl.model_downloading.connect(self._on_model_downloading)
        ctrl.download_progress.connect(self._on_download_progress)
        ctrl.error_occurred.connect(self._on_error)

        # ── TrayIcon → AppController ───────────────────────────────────────
        tray.settings_requested.connect(self._open_settings)
        tray.quit_requested.connect(self._on_quit)
        tray.pause_toggled.connect(self._on_pause_toggled)
        tray.mode_changed.connect(self._on_mode_changed)
        tray.overlay_toggled.connect(overlay.toggle_visibility)

        # ── Overlay position → Config ──────────────────────────────────────
        overlay.position_changed.connect(self._on_overlay_moved)

    # ── Slots ─────────────────────────────────────────────────────────────────

    def _on_state_changed_tray(self, state: str) -> None:
        """Forward state changes to the tray icon with a helpful detail string."""
        detail = ""
        if state == STATE_IDLE:
            mode = self._config.mode
            ptt_key = self._config.hotkey_ptt.replace("_", " ").title()
            toggle_key = self._config.hotkey_toggle.replace("_", " ").title()

            if mode == "ptt":
                detail = f"Hold {ptt_key} to record"
            elif mode == "toggle":
                detail = f"Press {toggle_key} to start"
            elif mode == "vad":
                detail = "Auto-listening"

        elif state == STATE_LOADING:
            detail = f"Loading {self._config.model_size}…"

        self._tray.set_state(state, detail)

    def _on_model_info(self, info: str) -> None:
        logger.info("Model info: %s", info)
        self._tray.notify("wshp — Ready", info, duration_ms=4000)

    def _on_model_downloading(self, model_name: str, total_mb: int) -> None:
        """Show a toast the moment a download starts and update the tray tooltip."""
        if total_mb > 0:
            size_str = f"~{total_mb:,} MB"
        else:
            size_str = "unknown size"

        logger.info("Download started: %s (%s)", model_name, size_str)

        self._tray.set_state(
            STATE_LOADING,
            f"Downloading {model_name}…",
        )
        self._tray.notify(
            "wshp — Downloading model",
            f"Downloading {model_name} ({size_str}).\n"
            "The app will be ready when the download completes.",
            duration_ms=6000,
        )

    def _on_download_progress(self, downloaded_mb: int, total_mb: int) -> None:
        """Update the tray tooltip with live MB progress while downloading."""
        if total_mb > 0:
            pct = min(100, int(downloaded_mb * 100 / total_mb))
            detail = f"Downloading… {downloaded_mb:,} / {total_mb:,} MB  ({pct} %)"
        else:
            detail = f"Downloading… {downloaded_mb:,} MB"

        # Only update the tooltip text — no balloon so we don't spam the user.
        self._tray.setToolTip(f"wshp — {detail}")

    def _on_error(self, message: str) -> None:
        logger.error("App error: %s", message)
        self._tray.set_state(STATE_ERROR, message[:60])
        self._tray.notify("wshp — Error", message, duration_ms=6000)

    def _on_pause_toggled(self, paused: bool) -> None:
        if paused:
            self._controller.pause()
        else:
            self._controller.resume()

    def _on_mode_changed(self, mode: str) -> None:
        self._config.mode = mode
        save_config(self._config)
        # Reflect new idle hint in tray
        self._on_state_changed_tray(self._controller.current_state)

    def _on_overlay_moved(self, x: int, y: int) -> None:
        # Config already updated by the Overlay; just persist it.
        save_config(self._config)

    def _open_settings(self) -> None:
        """Open the settings dialog (singleton — don't open twice)."""
        if self._settings_dialog is not None and self._settings_dialog.isVisible():
            self._settings_dialog.raise_()
            self._settings_dialog.activateWindow()
            return

        # Pause hotkeys while settings are open so the user can type keys
        # into the hotkey capture buttons without triggering recording.
        self._controller.pause()

        dlg = SettingsDialog(self._config)
        dlg.settings_applied.connect(self._on_settings_applied)
        dlg.finished.connect(self._on_settings_closed)
        self._settings_dialog = dlg
        dlg.show()

    def _on_settings_applied(self, new_config: Config) -> None:
        """Handle settings applied (OK or Apply was clicked)."""
        self._config = new_config
        self._controller.apply_config(new_config)

        # Re-snap the overlay to the (potentially new) corner
        self._overlay._config = new_config
        self._overlay.snap_to_corner()

        logger.info("Settings applied.")

    def _on_settings_closed(self) -> None:
        """Resume hotkeys after the settings dialog closes."""
        if not self._controller.is_paused:
            return  # Already resumed (e.g. model reload triggered state change)
        self._controller.resume()
        self._settings_dialog = None

    def _on_quit(self) -> None:
        """Handle quit request from tray menu."""
        self.stop()
        QApplication.instance().quit()


# ── Application entry point ────────────────────────────────────────────────────


def main() -> int:
    # ── Windows App User Model ID ─────────────────────────────────────────────
    # Must be set before QApplication is created.
    # This makes Windows group the app's taskbar button and toast notifications
    # under "wshp" rather than the host executable ("python.exe" or "wshp.exe").
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "wshp.WhisperDictation"
        )
    except Exception:
        pass  # Non-Windows or ctypes unavailable — safe to ignore

    # ── Logging ───────────────────────────────────────────────────────────────
    # Use DEBUG level if --debug flag is passed
    log_level = logging.DEBUG if "--debug" in sys.argv else logging.INFO
    _configure_logging(log_level)
    _install_exception_hook()

    logger.info("=" * 60)
    logger.info("wshp starting")
    logger.info("=" * 60)

    # ── Qt application ────────────────────────────────────────────────────────
    # Enable high-DPI scaling (important for 4K / scaled displays)
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("wshp")
    app.setApplicationDisplayName("wshp — Whisper Dictation")
    app.setOrganizationName("wshp")

    # Set the application-wide icon so it appears in:
    #   • Windows toast notifications (replaces the Python icon)
    #   • Alt-Tab switcher
    #   • Any QDialog / QWidget that inherits the app icon
    from assets import get_qicon

    app.setWindowIcon(get_qicon())

    # Never quit the event loop when the last window is closed —
    # the app lives in the system tray.
    app.setQuitOnLastWindowClosed(False)

    # ── Check system tray availability ────────────────────────────────────────
    from PyQt6.QtWidgets import QSystemTrayIcon

    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.critical(
            None,
            "wshp",
            "System tray is not available on this system.\n"
            "wshp requires a system tray to run.",
        )
        return 1

    # ── Config ────────────────────────────────────────────────────────────────
    config = load_config()
    logger.info(
        "Config loaded — model=%s  device=%s  mode=%s",
        config.model_size,
        config.device,
        config.mode,
    )

    # ── Build and start the app ───────────────────────────────────────────────
    wshp = WshpApp(config)
    wshp.start()

    # ── Run event loop ────────────────────────────────────────────────────────
    exit_code = app.exec()
    logger.info("Qt event loop exited with code %d.", exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
