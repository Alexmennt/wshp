"""
assets/__init__.py
──────────────────
Icon loading helpers for wshp.

Public API
──────────
    get_icon_path() -> str
        Return the absolute path to assets/icon.png, generating it first
        if it does not exist yet.

    get_qicon() -> QIcon
        Return a QIcon built from the app icon PNG.  Safe to call from the
        Qt main thread at any point after QApplication is constructed.

    get_tray_icon(state: str) -> QIcon
        Return a state-specific tray icon:
          • "idle"         — plain logo (no badge)
          • "loading"      — logo + blue dot
          • "recording"    — logo + red dot (used for blinking externally)
          • "transcribing" — logo + amber dot
          • "error"        — logo + orange-red dot
          • "paused"       — logo + grey dot
"""

from __future__ import annotations

import logging
import pathlib
from typing import Optional

logger = logging.getLogger(__name__)

HERE = pathlib.Path(__file__).parent
_ICON_PNG = HERE / "icon.png"

# ── Badge colours per state (R, G, B) ─────────────────────────────────────────
_BADGE_COLOURS: dict[str, tuple[int, int, int] | None] = {
    "idle": None,  # no badge
    "loading": (74, 144, 217),  # steel-blue
    "recording": (229, 57, 53),  # crimson
    "transcribing": (249, 168, 37),  # amber
    "error": (191, 54, 12),  # deep-orange
    "paused": (120, 144, 156),  # blue-grey
}

# Cache of already-built QIcon objects so we never regenerate mid-session.
_ICON_CACHE: dict[str, "QIcon"] = {}  # type: ignore[name-defined]
_BASE_QPIXMAP: Optional["QPixmap"] = None  # type: ignore[name-defined]


# ── File-level helpers (no Qt dependency) ─────────────────────────────────────


def get_icon_path() -> str:
    """
    Return the absolute path to ``assets/icon.png``.

    If the file does not exist it is generated on-the-fly using
    :mod:`assets.icon_gen`.  Any generation error is logged and the
    function returns the (non-existent) path so callers can decide what
    to do.
    """
    if not _ICON_PNG.exists():
        try:
            from assets.icon_gen import save

            save(_ICON_PNG)
            logger.info("App icon generated → %s", _ICON_PNG)
        except Exception as exc:
            logger.error("Failed to generate app icon: %s", exc)

    return str(_ICON_PNG)


# ── Qt helpers ────────────────────────────────────────────────────────────────


def get_qicon() -> "QIcon":
    """
    Return a :class:`~PyQt6.QtGui.QIcon` loaded from ``assets/icon.png``.

    Falls back to an empty ``QIcon`` if Pillow is unavailable and the file
    does not exist.  The result is cached after the first call.
    """
    if "plain" in _ICON_CACHE:
        return _ICON_CACHE["plain"]

    icon = _load_qicon()
    _ICON_CACHE["plain"] = icon
    return icon


def get_tray_icon(state: str) -> "QIcon":
    """
    Return a state-specific tray :class:`~PyQt6.QtGui.QIcon`.

    For *idle* the plain logo is returned.  For all other states a small
    coloured dot badge is composited onto the bottom-right corner of the
    logo so the user can see the application state at a glance even at
    tiny tray-icon sizes.

    Results are cached — repeated calls with the same *state* are free.
    """
    cache_key = f"tray_{state}"
    if cache_key in _ICON_CACHE:
        return _ICON_CACHE[cache_key]

    base_px = _get_base_pixmap()
    colour = _BADGE_COLOURS.get(state)

    if colour is None:
        # Idle — plain logo, no badge
        icon = _qicon_from_pixmap(base_px)
    else:
        badged = _add_badge(base_px, colour)
        icon = _qicon_from_pixmap(badged)

    _ICON_CACHE[cache_key] = icon
    return icon


def invalidate_cache() -> None:
    """Clear the icon cache (call after regenerating icon.png at runtime)."""
    global _BASE_QPIXMAP
    _ICON_CACHE.clear()
    _BASE_QPIXMAP = None


# ── Internal Qt helpers ───────────────────────────────────────────────────────


def _get_base_pixmap() -> "QPixmap":
    """Return the base logo pixmap, loading and caching it on first call."""
    global _BASE_QPIXMAP
    if _BASE_QPIXMAP is None:
        _BASE_QPIXMAP = _load_pixmap()
    return _BASE_QPIXMAP


def _load_pixmap() -> "QPixmap":
    """Load ``icon.png`` as a 64 × 64 QPixmap (tray-icon working size)."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QPixmap

    path = get_icon_path()
    px = QPixmap(path)

    if px.isNull():
        logger.warning("Could not load icon from %s — using fallback.", path)
        px = _fallback_pixmap(64)

    # Downscale to 64 × 64 once; Qt will handle further scaling for
    # the system tray (typically 16 × 16 or 24 × 24 on Windows 11).
    px = px.scaled(
        64,
        64,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    return px


def _load_qicon() -> "QIcon":
    """Build a multi-resolution QIcon from icon.png."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QIcon, QPixmap

    path = get_icon_path()
    base = QPixmap(path)

    if base.isNull():
        return QIcon()

    icon = QIcon()
    transform = Qt.TransformationMode.SmoothTransformation
    aspect = Qt.AspectRatioMode.KeepAspectRatio

    for sz in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(base.scaled(sz, sz, aspect, transform))

    return icon


def _qicon_from_pixmap(pixmap: "QPixmap") -> "QIcon":
    from PyQt6.QtGui import QIcon

    icon = QIcon()
    icon.addPixmap(pixmap)
    return icon


def _add_badge(
    pixmap: "QPixmap",
    colour: tuple[int, int, int],
) -> "QPixmap":
    """
    Return a copy of *pixmap* with a small coloured circle badge painted
    in the bottom-right corner.

    The badge has a dark ring around it so it remains visible against
    both light and dark taskbar backgrounds.
    """
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QColor, QPainter

    px = pixmap.copy()
    size = px.width()

    # Badge proportions that stay visible at 16 × 16 after Qt downscaling
    badge_d = max(16, size // 3)  # badge diameter
    ring_pad = max(2, size // 28)  # dark-ring thickness
    margin = max(1, size // 32)  # gap from edge

    x = size - badge_d - margin
    y = size - badge_d - margin

    painter = QPainter(px)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)

    # Dark halo so the badge contrasts with any taskbar colour
    halo_d = badge_d + ring_pad * 2
    painter.setBrush(QColor(18, 18, 22, 230))
    painter.drawEllipse(x - ring_pad, y - ring_pad, halo_d, halo_d)

    # Coloured dot
    r, g, b = colour
    painter.setBrush(QColor(r, g, b, 255))
    painter.drawEllipse(x, y, badge_d, badge_d)

    painter.end()
    return px


def _fallback_pixmap(size: int) -> "QPixmap":
    """
    Generate a minimal dark-circle fallback pixmap when icon.png is missing.
    Used so the rest of the app keeps working even without Pillow.
    """
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPixmap

    px = QPixmap(size, size)
    px.fill(Qt.GlobalColor.transparent)

    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(40, 40, 44, 255))

    path = QPainterPath()
    path.addRoundedRect(0, 0, size, size, size * 0.24, size * 0.24)
    p.drawPath(path)

    p.setBrush(QColor(220, 220, 225, 255))
    bar_w = size * 0.12
    cx = size / 2.0
    for i, h_frac in enumerate([0.30, 0.55, 0.85, 1.00, 0.65]):
        bh = (size * 0.65) * h_frac
        bx = cx - (2.5 * (bar_w + size * 0.03)) + i * (bar_w + size * 0.03)
        by = size / 2.0 - bh / 2.0
        path2 = QPainterPath()
        path2.addRoundedRect(bx, by, bar_w, bh, bar_w / 2, bar_w / 2)
        p.drawPath(path2)

    p.end()
    return px
