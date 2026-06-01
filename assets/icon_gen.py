"""
assets/icon_gen.py
──────────────────
Generates the wshp application icon programmatically using Pillow.

The icon reproduces the waveform logo:
  • Dark rounded-square (squircle) background — graphite gradient
  • Seven white capsule-shaped vertical bars of varying heights forming
    a centred audio-waveform silhouette

Run directly to regenerate icon.png:
    python assets/icon_gen.py
"""

from __future__ import annotations

import pathlib

import numpy as np
from PIL import Image, ImageDraw

HERE = pathlib.Path(__file__).parent
OUT_PNG = HERE / "icon.png"

# ── Default render size (use 512 for crisp scaling down to 16 / 24 / 32) ──────
SIZE = 512


def generate(size: int = SIZE) -> Image.Image:
    """
    Return a PIL RGBA image of the wshp icon at *size* × *size* pixels.

    Parameters
    ----------
    size:
        Output image dimension in pixels (square).  512 is the canonical size.
    """

    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    # ── 1. Background: dark squircle with diagonal gradient ───────────────────
    #
    # Gradient direction: top-left (#252527) → bottom-right (#3c3c40)
    # Matches the reference logo's subtle depth effect.
    yy, xx = np.mgrid[0:size, 0:size]
    t = (xx * 0.38 + yy * 0.62) / size  # 0 at top-left, 1 at bottom-right

    r_ch = np.clip(34 + t * 24, 0, 255).astype(np.uint8)
    g_ch = np.clip(34 + t * 23, 0, 255).astype(np.uint8)
    b_ch = np.clip(36 + t * 25, 0, 255).astype(np.uint8)
    a_ch = np.full((size, size), 255, dtype=np.uint8)

    bg = Image.fromarray(np.stack([r_ch, g_ch, b_ch, a_ch], axis=-1), "RGBA")

    # Rounded-rectangle (squircle) mask — corner radius ≈ 24 % of size
    corner_r = int(size * 0.235)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=corner_r, fill=255
    )
    bg.putalpha(mask)
    canvas = Image.alpha_composite(canvas, bg)

    # ── 2. Waveform bars ──────────────────────────────────────────────────────
    #
    # Seven white capsule bars.  Heights are tuned to reproduce the
    # silhouette in the reference logo (tallest bar is bar 4, index 3).
    #
    #  Index:  0     1     2     3     4     5     6
    #  Height: 28 %  48 %  85 %  100%  64 %  30 %  48 %
    #
    heights = [0.28, 0.48, 0.85, 1.00, 0.64, 0.30, 0.48]
    n_bars = len(heights)

    bar_w = size * 0.073  # bar width
    bar_gap = size * 0.027  # gap between bars
    usable_h = size * 0.72  # total usable height for the tallest bar

    total_w = n_bars * bar_w + (n_bars - 1) * bar_gap
    cx = size / 2.0
    cy = size / 2.0
    start_x = cx - total_w / 2.0

    draw = ImageDraw.Draw(canvas)
    cap_r = bar_w / 2.0  # capsule end-cap radius = half bar width

    for i, h in enumerate(heights):
        bh = usable_h * h
        bx0 = start_x + i * (bar_w + bar_gap)
        bx1 = bx0 + bar_w
        by0 = cy - bh / 2.0
        by1 = cy + bh / 2.0
        draw.rounded_rectangle(
            [bx0, by0, bx1, by1],
            radius=cap_r,
            fill=(255, 255, 255, 255),
        )

    return canvas


def save(path: pathlib.Path = OUT_PNG, size: int = SIZE) -> pathlib.Path:
    """Generate the icon and save it as a PNG.  Returns the output path."""
    img = generate(size)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG", optimize=True)
    return path


if __name__ == "__main__":
    out = save()
    kb = out.stat().st_size // 1024
    print(f"Icon saved → {out}  ({kb} KB, {SIZE}×{SIZE} px)")
