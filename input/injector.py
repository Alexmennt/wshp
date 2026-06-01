"""
input/injector.py
─────────────────
Injects transcribed text into the previously-active Windows application.

Two injection strategies are supported (configurable):

  "clipboard"  (default)
      1. Saves the current clipboard contents.
      2. Puts the transcription text on the clipboard.
      3. Brings the target window to the foreground.
      4. Sends Ctrl+V.
      5. Restores the original clipboard contents.
      This approach handles Unicode, emoji, and long texts correctly.

  "keyboard"
      Simulates individual key presses via pynput.  Reliable in apps that
      intercept Ctrl+V (e.g. some terminals, games).  Slower for long text.

The target HWND is captured the moment recording starts so that even if the
user clicks elsewhere during the (brief) transcription phase the text still
lands in the right window.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# ── Optional win32 imports ────────────────────────────────────────────────────
try:
    import win32api
    import win32con
    import win32gui

    _WIN32_AVAILABLE = True
except ImportError:
    _WIN32_AVAILABLE = False
    logger.warning(
        "pywin32 not found – window focus management will be limited. "
        "Run: pip install pywin32"
    )

# ── Optional pyperclip import ─────────────────────────────────────────────────
try:
    import pyperclip

    _PYPERCLIP_AVAILABLE = True
except ImportError:
    _PYPERCLIP_AVAILABLE = False
    logger.warning(
        "pyperclip not found – clipboard injection unavailable. "
        "Run: pip install pyperclip"
    )

# ── pynput for keyboard-mode fallback ─────────────────────────────────────────
try:
    from pynput.keyboard import Controller as KeyboardController
    from pynput.keyboard import Key

    _PYNPUT_AVAILABLE = True
except ImportError:
    _PYNPUT_AVAILABLE = False
    logger.warning("pynput not found – keyboard injection unavailable.")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _get_foreground_hwnd() -> Optional[int]:
    """Return the HWND of the currently active window (or None)."""
    if not _WIN32_AVAILABLE:
        return None
    try:
        hwnd = win32gui.GetForegroundWindow()
        return hwnd if hwnd else None
    except Exception:
        return None


def _window_title(hwnd: Optional[int]) -> str:
    """Return the window title for debugging, or an empty string."""
    if not _WIN32_AVAILABLE or not hwnd:
        return ""
    try:
        return win32gui.GetWindowText(hwnd)
    except Exception:
        return ""


def _bring_to_front(hwnd: int, delay_ms: int = 80) -> bool:
    """
    Attempt to bring *hwnd* to the foreground.

    Windows restricts SetForegroundWindow to the currently active thread
    unless the calling process has the foreground lock.  We use a documented
    workaround: attach to the foreground thread momentarily.

    Returns True on apparent success.
    """
    if not _WIN32_AVAILABLE:
        return False
    try:
        if not win32gui.IsWindow(hwnd):
            return False

        # If the window is minimised, restore it first.
        placement = win32gui.GetWindowPlacement(hwnd)
        if placement[1] == win32con.SW_SHOWMINIMIZED:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

        # Attach input threads so SetForegroundWindow is allowed.
        fg_hwnd = win32gui.GetForegroundWindow()
        fg_tid = win32api.GetWindowThreadProcessId(fg_hwnd)[0]
        our_tid = win32api.GetCurrentThreadId()

        if fg_tid != our_tid:
            win32api.AttachThreadInput(our_tid, fg_tid, True)

        win32gui.SetForegroundWindow(hwnd)

        if fg_tid != our_tid:
            win32api.AttachThreadInput(our_tid, fg_tid, False)

        # Give the OS a moment to process the focus change.
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)

        return True
    except Exception as exc:
        logger.debug("_bring_to_front failed: %s", exc)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# TextInjector
# ─────────────────────────────────────────────────────────────────────────────


class TextInjector:
    """
    Injects text into a Windows application.

    Typical usage
    -------------
    ::

        injector = TextInjector(config)

        # Call this the instant the user starts recording:
        injector.capture_target()

        # Call this after Whisper returns the transcription:
        injector.inject("Hello, world!")
    """

    def __init__(self, config) -> None:
        self._cfg = config
        self._target_hwnd: Optional[int] = None
        self._kb: Optional[KeyboardController] = (
            KeyboardController() if _PYNPUT_AVAILABLE else None
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def capture_target(self) -> None:
        """
        Snapshot the currently-focused window so text lands there even if the
        user clicks something else while waiting for transcription.

        Call this at the very start of a recording session (e.g. on key-down).
        """
        hwnd = _get_foreground_hwnd()
        if hwnd:
            self._target_hwnd = hwnd
            logger.debug(
                "Captured target HWND=%s  title=%r",
                hwnd,
                _window_title(hwnd),
            )
        else:
            self._target_hwnd = None
            logger.debug("No foreground window found – will inject to current focus.")

    def inject(self, text: str) -> bool:
        """
        Inject *text* into the target window.

        Returns True if injection probably succeeded.
        """
        text = text.strip()
        if not text:
            logger.debug("inject() called with empty text – skipping.")
            return False

        logger.info("Injecting %d chars: %r…", len(text), text[:60])

        method = self._cfg.injection_method

        if method == "clipboard" and _PYPERCLIP_AVAILABLE:
            return self._inject_clipboard(text)
        elif _PYNPUT_AVAILABLE:
            if method == "clipboard" and not _PYPERCLIP_AVAILABLE:
                logger.warning(
                    "Clipboard injection requested but pyperclip is not available. "
                    "Falling back to keyboard mode."
                )
            return self._inject_keyboard(text)
        else:
            logger.error(
                "No injection method available. "
                "Install pyperclip (clipboard mode) or pynput (keyboard mode)."
            )
            return False

    def clear_target(self) -> None:
        """Release the stored target window handle."""
        self._target_hwnd = None

    # ── Injection strategies ──────────────────────────────────────────────────

    def _inject_clipboard(self, text: str) -> bool:
        """
        1. Save old clipboard.
        2. Set clipboard to *text*.
        3. Focus target window.
        4. Send Ctrl+V.
        5. Restore clipboard (if configured).
        """
        # ── Save existing clipboard ──────────────────────────────────────────
        old_clipboard: Optional[str] = None
        if self._cfg.restore_clipboard:
            try:
                old_clipboard = pyperclip.paste()
            except Exception:
                old_clipboard = None

        # ── Put text on clipboard ────────────────────────────────────────────
        try:
            pyperclip.copy(text)
        except Exception as exc:
            logger.error("Failed to set clipboard: %s", exc)
            return self._inject_keyboard(text)

        # ── Focus target window ──────────────────────────────────────────────
        focused = False
        if self._target_hwnd:
            focused = _bring_to_front(
                self._target_hwnd, delay_ms=self._cfg.focus_delay_ms
            )
            if not focused:
                logger.debug(
                    "Could not focus target HWND=%s; sending Ctrl+V to current focus.",
                    self._target_hwnd,
                )
        else:
            # No captured target → trust whatever has focus right now.
            time.sleep(self._cfg.focus_delay_ms / 1000.0)

        # ── Send Ctrl+V ──────────────────────────────────────────────────────
        ok = self._send_paste()

        # ── Restore clipboard ────────────────────────────────────────────────
        if self._cfg.restore_clipboard and old_clipboard is not None:
            # Small delay so the target app finishes processing the paste
            # before we overwrite the clipboard again.
            time.sleep(0.15)
            try:
                pyperclip.copy(old_clipboard)
            except Exception:
                pass

        return ok

    def _send_paste(self) -> bool:
        """Send Ctrl+V using pynput (preferred) or a win32 PostMessage fallback."""
        # pynput path – works in most apps
        if _PYNPUT_AVAILABLE and self._kb:
            try:
                with self._kb.pressed(Key.ctrl):
                    self._kb.press("v")
                    self._kb.release("v")
                return True
            except Exception as exc:
                logger.warning("pynput Ctrl+V failed: %s – trying win32 fallback", exc)

        # win32 PostMessage fallback
        if _WIN32_AVAILABLE and self._target_hwnd:
            try:
                hwnd = self._target_hwnd
                win32api.PostMessage(hwnd, win32con.WM_KEYDOWN, win32con.VK_CONTROL, 0)
                win32api.PostMessage(hwnd, win32con.WM_KEYDOWN, ord("V"), 0)
                time.sleep(0.04)
                win32api.PostMessage(hwnd, win32con.WM_KEYUP, ord("V"), 0)
                win32api.PostMessage(hwnd, win32con.WM_KEYUP, win32con.VK_CONTROL, 0)
                return True
            except Exception as exc:
                logger.error("win32 PostMessage Ctrl+V failed: %s", exc)

        return False

    def _inject_keyboard(self, text: str) -> bool:
        """
        Simulate individual key presses.

        Handles Unicode via pynput's type() method which uses the OS-level
        virtual-key infrastructure on Windows.
        """
        if not _PYNPUT_AVAILABLE or not self._kb:
            logger.error("pynput keyboard controller not available.")
            return False

        # Focus target window first
        if self._target_hwnd:
            _bring_to_front(self._target_hwnd, delay_ms=self._cfg.focus_delay_ms)
        else:
            time.sleep(self._cfg.focus_delay_ms / 1000.0)

        delay = self._cfg.keyboard_injection_delay

        try:
            for char in text:
                self._kb.type(char)
                if delay > 0:
                    time.sleep(delay)
            return True
        except Exception as exc:
            logger.error("Keyboard injection failed: %s", exc)
            return False
