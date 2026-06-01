"""
Global hotkey manager using pynput.

Runs a keyboard listener in a daemon thread and pushes typed events into a
thread-safe Queue that the AppController polls via a QTimer.  This avoids any
Qt / pynput thread-interaction issues because we never touch Qt objects here.

Supported event types (dict with 'type' key):
    {'type': 'ptt_press'}    – PTT key was pressed
    {'type': 'ptt_release'}  – PTT key was released
    {'type': 'toggle'}       – toggle hotkey was pressed
    {'type': 'cancel'}       – cancel hotkey was pressed
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Optional

from pynput import keyboard

logger = logging.getLogger(__name__)

# ── Key-name → pynput mapping ──────────────────────────────────────────────

_SPECIAL_KEY_MAP: dict[str, keyboard.Key] = {
    # Shifts
    "right_shift": keyboard.Key.shift_r,
    "left_shift": keyboard.Key.shift_l,
    "shift": keyboard.Key.shift,
    # Ctrls
    "right_ctrl": keyboard.Key.ctrl_r,
    "left_ctrl": keyboard.Key.ctrl_l,
    "ctrl": keyboard.Key.ctrl,
    # Alts
    "right_alt": keyboard.Key.alt_r,
    "left_alt": keyboard.Key.alt_l,
    "alt": keyboard.Key.alt,
    # Function keys
    "f1": keyboard.Key.f1,
    "f2": keyboard.Key.f2,
    "f3": keyboard.Key.f3,
    "f4": keyboard.Key.f4,
    "f5": keyboard.Key.f5,
    "f6": keyboard.Key.f6,
    "f7": keyboard.Key.f7,
    "f8": keyboard.Key.f8,
    "f9": keyboard.Key.f9,
    "f10": keyboard.Key.f10,
    "f11": keyboard.Key.f11,
    "f12": keyboard.Key.f12,
    # Misc
    "caps_lock": keyboard.Key.caps_lock,
    "tab": keyboard.Key.tab,
    "escape": keyboard.Key.esc,
    "esc": keyboard.Key.esc,
    "insert": keyboard.Key.insert,
    "scroll_lock": keyboard.Key.scroll_lock,
    "pause": keyboard.Key.pause,
    "menu": keyboard.Key.menu,
    "print_screen": keyboard.Key.print_screen,
    "num_lock": keyboard.Key.num_lock,
    "page_up": keyboard.Key.page_up,
    "page_down": keyboard.Key.page_down,
    "home": keyboard.Key.home,
    "end": keyboard.Key.end,
    "delete": keyboard.Key.delete,
    "backspace": keyboard.Key.backspace,
    "enter": keyboard.Key.enter,
    "space": keyboard.Key.space,
    "up": keyboard.Key.up,
    "down": keyboard.Key.down,
    "left": keyboard.Key.left,
    "right": keyboard.Key.right,
}


def parse_key(key_str: str) -> keyboard.Key | keyboard.KeyCode | None:
    """
    Convert a human-readable key name string to a pynput key object.

    Examples
    --------
    "right_shift"  →  keyboard.Key.shift_r
    "f9"           →  keyboard.Key.f9
    "z"            →  keyboard.KeyCode.from_char('z')
    """
    key_str = key_str.strip().lower()

    # Try named special keys first
    if key_str in _SPECIAL_KEY_MAP:
        return _SPECIAL_KEY_MAP[key_str]

    # Single printable character
    if len(key_str) == 1:
        return keyboard.KeyCode.from_char(key_str)

    logger.warning("Unknown key name %r — hotkey will be disabled.", key_str)
    return None


def key_display_name(key_str: str) -> str:
    """Return a nice display string for a key name, e.g. 'right_shift' → 'Right Shift'."""
    return key_str.replace("_", " ").title()


# ── HotkeyManager ─────────────────────────────────────────────────────────


class HotkeyManager(threading.Thread):
    """
    Listens for global keyboard events and enqueues action dicts.

    Parameters
    ----------
    event_queue:
        Thread-safe queue shared with the AppController.
    hotkey_ptt:
        Key name for push-to-talk (hold while speaking).
    hotkey_toggle:
        Key name for toggle mode (press once to start, again to stop).
    hotkey_cancel:
        Key name to abort the current recording.
    """

    def __init__(
        self,
        event_queue: queue.Queue,
        hotkey_ptt: str = "right_shift",
        hotkey_toggle: str = "f9",
        hotkey_cancel: str = "escape",
    ) -> None:
        super().__init__(name="HotkeyListener", daemon=True)

        self._queue = event_queue
        self._paused = threading.Event()
        self._stop_event = threading.Event()
        self._listener: Optional[keyboard.Listener] = None

        # Resolve configured key strings to pynput objects
        self._ptt_key = parse_key(hotkey_ptt)
        self._toggle_key = parse_key(hotkey_toggle)
        self._cancel_key = parse_key(hotkey_cancel)

        # Track which keys are currently held down to avoid repeated events
        # from key-repeat auto-fire
        self._held: set = set()

        logger.debug(
            "HotkeyManager configured — PTT: %s  Toggle: %s  Cancel: %s",
            hotkey_ptt,
            hotkey_toggle,
            hotkey_cancel,
        )

    # ── Public interface ───────────────────────────────────────────────────

    def pause(self) -> None:
        """Temporarily disable all hotkeys (e.g. while the settings dialog is open)."""
        self._paused.set()
        logger.debug("Hotkeys paused.")

    def resume(self) -> None:
        """Re-enable hotkeys after a pause."""
        self._paused.clear()
        logger.debug("Hotkeys resumed.")

    def update_keys(
        self,
        hotkey_ptt: str,
        hotkey_toggle: str,
        hotkey_cancel: str,
    ) -> None:
        """
        Update the configured hotkeys at runtime.
        Call after saving new settings; the listener will pick up changes
        automatically on the next key event.
        """
        self._ptt_key = parse_key(hotkey_ptt)
        self._toggle_key = parse_key(hotkey_toggle)
        self._cancel_key = parse_key(hotkey_cancel)
        self._held.clear()
        logger.debug("Hotkeys updated.")

    def stop(self) -> None:
        """Stop the listener thread."""
        self._stop_event.set()
        if self._listener is not None:
            self._listener.stop()

    # ── Thread entry point ─────────────────────────────────────────────────

    def run(self) -> None:
        logger.info("Hotkey listener thread started.")
        while not self._stop_event.is_set():
            try:
                self._listener = keyboard.Listener(
                    on_press=self._on_press,
                    on_release=self._on_release,
                    suppress=False,  # Never suppress — let keys through to other apps
                )
                with self._listener:
                    self._listener.join()
            except Exception as exc:  # noqa: BLE001
                if not self._stop_event.is_set():
                    logger.error("Hotkey listener crashed: %s — restarting.", exc)
                    # Brief sleep before restarting to avoid tight loop on error
                    self._stop_event.wait(timeout=1.0)

        logger.info("Hotkey listener thread stopped.")

    # ── Internal callbacks (run in pynput's listener thread) ───────────────

    def _matches(self, key, target) -> bool:
        """Check whether a received pynput key matches our configured target."""
        if target is None:
            return False
        if isinstance(target, keyboard.Key):
            return key == target
        if isinstance(target, keyboard.KeyCode):
            # Compare by char for printable keys so that case doesn't matter
            if isinstance(key, keyboard.KeyCode):
                return (key.char or "").lower() == (target.char or "").lower()
        return False

    def _key_id(self, key) -> str:
        """Return a stable hashable string id for a key."""
        if isinstance(key, keyboard.Key):
            return f"Key.{key.name}"
        if isinstance(key, keyboard.KeyCode):
            return f"Char.{(key.char or '').lower()}"
        return str(key)

    def _on_press(self, key) -> None:
        if self._paused.is_set():
            return

        kid = self._key_id(key)

        # PTT press — only fire once (ignore key-repeat)
        if self._matches(key, self._ptt_key) and kid not in self._held:
            self._held.add(kid)
            self._enqueue("ptt_press")
            return

        # Toggle — only fire on first press event
        if self._matches(key, self._toggle_key) and kid not in self._held:
            self._held.add(kid)
            self._enqueue("toggle")
            return

        # Cancel
        if self._matches(key, self._cancel_key) and kid not in self._held:
            self._held.add(kid)
            self._enqueue("cancel")

    def _on_release(self, key) -> None:
        if self._paused.is_set():
            return

        kid = self._key_id(key)
        self._held.discard(kid)

        if self._matches(key, self._ptt_key):
            self._enqueue("ptt_release")

    def _enqueue(self, event_type: str) -> None:
        try:
            self._queue.put_nowait({"type": event_type})
            logger.debug("Hotkey event enqueued: %s", event_type)
        except queue.Full:
            logger.warning("Hotkey event queue full — dropping event %s.", event_type)
