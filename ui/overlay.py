"""
ui/overlay.py
─────────────
Bottom-centre animated waveform pill overlay.

Appearance
──────────
A compact near-black rounded pill anchored to the bottom-centre of the
primary screen, sitting just above the taskbar.  Inside it, 13 thin white
vertical bars animate to simulate live audio activity.

States
──────
  recording     – visible, bars animate with random bouncing heights
  transcribing  – visible, bars flow as a slow left-to-right sine wave
  everything else – hidden (pill fades out with a short delay)

Public API (kept compatible with app_controller / main.py):
  set_state(state)
  set_preview_text(text)      – no-op in this design
  set_recording_time(seconds) – no-op in this design
  snap_to_corner()            – repositions to bottom-centre
  toggle_visibility()         – honours config.show_overlay
  position_changed signal     – never emitted (kept for API compat)
"""

from __future__ import annotations

import logging
import math
import random
from typing import Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QGuiApplication, QPainter, QPainterPath
from PyQt6.QtWidgets import QWidget

logger = logging.getLogger(__name__)

# ── Design tokens ──────────────────────────────────────────────────────────────

_N_BARS = 13  # number of waveform bars
_BAR_W = 4  # bar width  (logical px)
_BAR_GAP = 4  # gap between bars (logical px)
_PAD_H = 26  # horizontal padding inside pill on each side (logical px)
_PILL_H = 42  # pill height (logical px)

# Derived pill width: bars + gaps + padding
_PILL_W = _N_BARS * _BAR_W + (_N_BARS - 1) * _BAR_GAP + 2 * _PAD_H
# = 13*4 + 12*4 + 52 = 52 + 48 + 52 = 152 px  (~3.6:1 ratio)

# Vertical space the bars can use inside the pill
_BAR_AREA_H = _PILL_H - 14  # 7 px top + 7 px bottom margin

_BAR_MIN_F = 0.28  # minimum bar height as fraction of _BAR_AREA_H
_BAR_MAX_F = 0.95  # maximum bar height as fraction of _BAR_AREA_H

# Colours
_BG_BASE = (0, 0, 0, 255)  # opaque black RGBA components
_BARS_BASE = (255, 255, 255, 242)  # near-white RGBA components

# Animation cadence
_ANIM_MS = 40  # animation frame interval (~25 fps)
_TARGET_MS = 130  # how often recording bars pick new random targets

# Fade-out: driven by a plain QTimer so it works with WA_TranslucentBackground.
# setWindowOpacity() conflicts with per-pixel alpha on Windows — we apply the
# opacity multiplier directly to the colours inside paintEvent instead.
_FADE_INTERVAL_MS = 16  # ~60 fps fade updates
_FADE_STEPS = 18  # total steps → 18 × 16 ms ≈ 290 ms fade

# Position
_BOTTOM_MARGIN = 34  # px above the available screen bottom (clears taskbar)

# How long to keep showing after leaving an active state before hiding
_HIDE_DELAY_MS = 500


class Overlay(QWidget):
    """
    Bottom-centre animated waveform pill.

    All public methods are safe to call from the Qt main thread at any time.
    """

    # Kept for API compatibility with main.py; never emitted in this design.
    position_changed = pyqtSignal(int, int)

    def __init__(self, config, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._config = config
        self._state = "idle"

        # ── Bar animation state ────────────────────────────────────────────
        self._heights = [0.25] * _N_BARS  # current height fractions [0..1]
        self._targets = [0.25] * _N_BARS  # target height fractions  [0..1]
        self._phase = 0.0  # running phase for transcribing wave

        # ── Timers ────────────────────────────────────────────────────────
        # Drives each animation frame
        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(_ANIM_MS)
        self._anim_timer.timeout.connect(self._step)

        # Randomises target heights in recording mode
        self._target_timer = QTimer(self)
        self._target_timer.setInterval(_TARGET_MS)
        self._target_timer.timeout.connect(self._new_targets)

        # Brief pause before hiding so the pill doesn't vanish too abruptly
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(_HIDE_DELAY_MS)
        self._hide_timer.timeout.connect(self._do_hide)

        # Manual alpha fade — QPropertyAnimation on windowOpacity does not work
        # on Windows when WA_TranslucentBackground is active (per-pixel alpha and
        # LWA_ALPHA conflict at the DWM level).  We keep our own _alpha float and
        # multiply it into every colour inside paintEvent instead.
        self._alpha: float = 1.0
        self._fade_timer = QTimer(self)
        self._fade_timer.setInterval(_FADE_INTERVAL_MS)
        self._fade_timer.timeout.connect(self._fade_step)

        # ── Qt window setup ────────────────────────────────────────────────
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.NoDropShadowWindowHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)

        self.setFixedSize(_PILL_W, _PILL_H)
        self._disable_native_frame_effects()
        self._reposition()
        self.hide()

    def _disable_native_frame_effects(self) -> None:
        """Remove Windows 11's automatic border/corner treatment for tool windows."""
        try:
            import ctypes

            hwnd = int(self.winId())
            dwmapi = ctypes.windll.dwmapi

            # DWMWA_NCRENDERING_POLICY = 2, DWMNCRP_DISABLED = 1
            value = ctypes.c_int(1)
            dwmapi.DwmSetWindowAttribute(hwnd, 2, ctypes.byref(value), ctypes.sizeof(value))

            # DWMWA_WINDOW_CORNER_PREFERENCE = 33, DWMWCP_DONOTROUND = 1
            value = ctypes.c_int(1)
            dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(value), ctypes.sizeof(value))

            # DWMWA_BORDER_COLOR = 34, DWMWA_COLOR_NONE = 0xFFFFFFFE
            color = ctypes.c_uint(0xFFFFFFFE)
            dwmapi.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(color), ctypes.sizeof(color))
        except Exception:
            logger.debug("Could not disable native overlay frame effects.", exc_info=True)

    # ── Public API ─────────────────────────────────────────────────────────────

    def set_state(self, state: str) -> None:
        """
        Transition to *state*.

        "recording"    → show pill, start bouncy random bar animation
        "transcribing" → show pill, switch to slow sine-wave animation
        anything else  → stop animation, hide pill after a short delay
        """
        if state == self._state:
            return

        prev_state = self._state
        self._state = state
        logger.debug("Overlay state: %s → %s", prev_state, state)

        if state == "recording":
            self._hide_timer.stop()
            self._cancel_fade()  # abort any ongoing fade immediately
            self._new_targets()
            self._target_timer.start()
            self._anim_timer.start()
            if self._config.show_overlay:
                self._show_full()

        elif state == "transcribing":
            self._hide_timer.stop()
            self._cancel_fade()
            self._target_timer.stop()  # wave is deterministic — no random targets
            self._anim_timer.start()
            if self._config.show_overlay:
                self._show_full()

        else:
            # Stop driving the animation but let the last frame stay visible
            # for _HIDE_DELAY_MS so the pill doesn't snap away instantly.
            self._target_timer.stop()
            self._anim_timer.stop()
            if self.isVisible():
                self._hide_timer.start()

    def set_preview_text(self, text: str) -> None:
        """No-op — kept for API compatibility with app_controller."""
        pass

    def set_recording_time(self, seconds: float) -> None:
        """No-op — kept for API compatibility with app_controller."""
        pass

    def snap_to_corner(self) -> None:
        """Reposition to bottom-centre (API compat — corner config is ignored)."""
        self._reposition()

    def toggle_visibility(self) -> None:
        """
        Toggle the overlay on/off.  Updates config.show_overlay so the
        preference persists across state changes.
        """
        self._config.show_overlay = not getattr(self._config, "show_overlay", True)

        if not self._config.show_overlay:
            self._do_hide()
        elif self._state in ("recording", "transcribing"):
            self.show()

    # ── Animation ──────────────────────────────────────────────────────────────

    def _new_targets(self) -> None:
        """
        Pick fresh random target heights for each bar (recording mode).

        Uses a Beta(2, 1.3) distribution so bars spend more time near the
        top of their range, giving a visually "active" impression.
        """
        for i in range(_N_BARS):
            raw = random.betavariate(2, 1.3)
            self._targets[i] = _BAR_MIN_F + (_BAR_MAX_F - _BAR_MIN_F) * raw

    def _step(self) -> None:
        """Advance one animation frame."""
        if self._state == "recording":
            # Exponential lerp: current → target
            alpha = 0.28
            for i in range(_N_BARS):
                self._heights[i] += (self._targets[i] - self._heights[i]) * alpha

        elif self._state == "transcribing":
            # Slow left-to-right sine wave
            self._phase += 0.09  # ≈ 2.25 rad/s at 25 fps
            spread = math.pi * 1.8
            for i in range(_N_BARS):
                t = (i / (_N_BARS - 1)) * spread
                wave = 0.50 + 0.38 * math.sin(self._phase + t)
                wave = max(_BAR_MIN_F, min(_BAR_MAX_F, wave))
                # Smooth transition into the wave
                self._heights[i] += (wave - self._heights[i]) * 0.18

        self.update()

    # ── Painting ───────────────────────────────────────────────────────────────

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # ── Clear entire widget to fully transparent ───────────────────────
        # On Windows with WA_TranslucentBackground the DWM compositor leaves
        # stale dark pixels in the rounded corners between frames.  Filling
        # the widget rect with CompositionMode_Clear before painting the pill
        # wipes those pixels back to alpha=0 each frame.
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        p.fillRect(self.rect(), QColor(0, 0, 0, 0))
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

        # Keep antialiasing inside the transparent window bounds. Painting on
        # the outermost pixels can blend with DWM's backing surface and show as
        # a pale outline on Windows.
        inset = 1.0
        w = float(self.width()) - inset * 2.0
        h = float(self.height()) - inset * 2.0

        # Apply our manual fade alpha to both colours so the entire pill
        # fades uniformly without touching windowOpacity.
        a = max(0.0, min(1.0, self._alpha))
        bg = QColor(*_BG_BASE[:3], int(_BG_BASE[3] * a))
        bars = QColor(*_BARS_BASE[:3], int(_BARS_BASE[3] * a))

        # ── Pill background ────────────────────────────────────────────────
        pill = QPainterPath()
        pill.addRoundedRect(inset, inset, w, h, h / 2.0, h / 2.0)
        p.fillPath(pill, bg)

        # ── Waveform bars ──────────────────────────────────────────────────
        total_bar_w = _N_BARS * _BAR_W + (_N_BARS - 1) * _BAR_GAP
        bx0 = inset + (w - total_bar_w) / 2.0  # x of first bar
        cy = inset + h / 2.0  # vertical centre

        p.setPen(Qt.PenStyle.NoPen)

        for i, frac in enumerate(self._heights):
            frac = max(_BAR_MIN_F, min(1.0, frac))
            bh = max(3.0, _BAR_AREA_H * frac)
            bx = bx0 + i * (_BAR_W + _BAR_GAP)
            by = cy - bh / 2.0
            cap = _BAR_W / 2.0

            bar = QPainterPath()
            bar.addRoundedRect(bx, by, float(_BAR_W), bh, cap, cap)
            p.fillPath(bar, bars)

        p.end()

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _reposition(self) -> None:
        """Move the pill to bottom-centre of the primary screen."""
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        avail = screen.availableGeometry()
        x = avail.center().x() - self.width() // 2
        y = avail.bottom() - self.height() - _BOTTOM_MARGIN
        self.move(x, y)

    def _do_hide(self) -> None:
        """Kick off the manual alpha fade-out."""
        if not self.isVisible():
            return
        self._alpha = 1.0
        self._fade_timer.start()

    def _fade_step(self) -> None:
        """Decrement alpha by one step and repaint; hide when fully transparent."""
        self._alpha -= 1.0 / _FADE_STEPS
        if self._alpha <= 0.0:
            self._alpha = 0.0
            self._fade_timer.stop()
            self.hide()
            self._alpha = 1.0  # reset for the next time the pill is shown
            return
        self.update()

    def _cancel_fade(self) -> None:
        """Abort any in-progress fade and restore full opacity instantly."""
        self._fade_timer.stop()
        self._alpha = 1.0

    def _show_full(self) -> None:
        """Show the pill at full opacity (safe to call mid-fade)."""
        self._alpha = 1.0
        self._disable_native_frame_effects()
        self.repaint()
        self.show()
        self.update()
