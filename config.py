"""
Configuration management for wshp (Whisper Flow for Windows).
Settings are stored as JSON in the user's home directory.
"""

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Optional

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".wshp")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")


@dataclass
class Config:
    # ── Model ─────────────────────────────────────────────────────────────────
    # "large-v3-turbo" is the best default: highest accuracy, fast, ~2.5 GB VRAM
    # "large-v3"       is the largest non-turbo option:        ~4.5 GB VRAM (float16)
    # Both fit comfortably on a 12 GB card.
    model_size: str = "large-v3-turbo"

    # "auto"  → use CUDA if available, else CPU
    # "cuda"  → force GPU
    # "cpu"   → force CPU (slow for large models)
    device: str = "auto"

    # "float16"       → best speed/quality on GPU (default)
    # "int8_float16"  → slightly less VRAM, negligible quality loss
    # "int8"          → CPU-friendly
    # "float32"       → CPU fallback (never auto-selected on GPU)
    compute_type: str = "float16"

    # ISO-639-1 language code ("en", "fr", …) or None for auto-detect
    language: Optional[str] = None

    # Number of beams for beam search (higher = more accurate but slower)
    beam_size: int = 5

    # Optional transcription hint / vocabulary primer for Whisper
    initial_prompt: str = ""

    # ── Hotkeys ───────────────────────────────────────────────────────────────
    # These are key *names* understood by pynput.keyboard.Key / keyboard.KeyCode.
    # Supported modifier/special names:
    #   right_shift, left_shift, right_ctrl, left_ctrl, right_alt, left_alt,
    #   f1–f12, caps_lock, tab, insert, scroll_lock, pause, menu
    # For regular characters use a single character, e.g. "z".
    hotkey_ptt: str = "right_shift"  # Hold to record (PTT mode)
    hotkey_toggle: str = "f9"  # Toggle VAD / recording on/off
    hotkey_cancel: str = "escape"  # Cancel current recording

    # ── Audio ─────────────────────────────────────────────────────────────────
    sample_rate: int = 16000  # Hz — Whisper requires 16 kHz
    channels: int = 1  # Mono
    # None → system default microphone; set to an integer index to pin a device
    microphone_index: Optional[int] = None

    # ── Voice Activity Detection (VAD) ────────────────────────────────────────
    # VAD mode aggressiveness: 0 (least) – 3 (most aggressive at filtering noise)
    vad_aggressiveness: int = 3
    # Seconds of consecutive silence before a VAD recording is finalised
    vad_silence_duration: float = 0.8
    # Recordings shorter than this (seconds) are discarded as noise
    min_speech_duration: float = 0.3

    # ── Operating Mode ────────────────────────────────────────────────────────
    # "ptt"  → hold hotkey_ptt while speaking; release to transcribe
    # "vad"  → always listening; VAD decides when to transcribe
    # "toggle" → press hotkey_toggle once to start, again to stop
    mode: str = "ptt"

    # ── Text Injection ────────────────────────────────────────────────────────
    # "clipboard" → set clipboard then send Ctrl+V (fastest, handles Unicode)
    # "keyboard"  → simulate key presses character-by-character (slower)
    injection_method: str = "clipboard"
    # Whether to save and restore the clipboard contents after injection
    restore_clipboard: bool = True
    # Per-character delay when using "keyboard" mode (seconds)
    keyboard_injection_delay: float = 0.005
    # Brief delay (ms) between SetForegroundWindow and sending Ctrl+V
    focus_delay_ms: int = 80

    # ── Overlay UI ────────────────────────────────────────────────────────────
    show_overlay: bool = True
    # "top_left" | "top_right" | "bottom_left" | "bottom_right" | "custom"
    overlay_position: str = "bottom_right"
    # Used when overlay_position == "custom"
    overlay_x: int = -1
    overlay_y: int = -1
    # Opacity: 0.0 (transparent) – 1.0 (opaque)
    overlay_opacity: float = 0.92


def load_config() -> "Config":
    """Load config from disk, falling back to defaults for any missing keys."""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            # Only forward recognised fields so old/extra keys don't cause errors
            known = set(Config.__dataclass_fields__.keys())
            filtered = {k: v for k, v in data.items() if k in known}
            return Config(**filtered)
        except (json.JSONDecodeError, TypeError, KeyError):
            pass  # Corrupt config → use defaults
    return Config()


def save_config(config: "Config") -> None:
    """Persist the current config to disk."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(asdict(config), fh, indent=2, ensure_ascii=False)


def get_model_options() -> list[dict]:
    """
    Return a human-readable list of supported model options with metadata.
    Useful for populating the settings UI dropdown.
    """
    return [
        {
            "id": "large-v3-turbo",
            "label": "large-v3-turbo  (Best — fastest, highest accuracy, ~2.5 GB VRAM)",
            "vram_gb": 2.5,
        },
        {
            "id": "large-v3",
            "label": "large-v3  (Highest quality, ~4.5 GB VRAM)",
            "vram_gb": 4.5,
        },
        {
            "id": "distil-large-v3",
            "label": "distil-large-v3  (Distilled, fast, ~2.4 GB VRAM)",
            "vram_gb": 2.4,
        },
        {
            "id": "medium",
            "label": "medium  (Balanced, ~1.5 GB VRAM)",
            "vram_gb": 1.5,
        },
        {
            "id": "small",
            "label": "small  (Light, ~0.5 GB VRAM)",
            "vram_gb": 0.5,
        },
        {
            "id": "base",
            "label": "base  (Minimal, ~0.15 GB VRAM)",
            "vram_gb": 0.15,
        },
    ]


def get_compute_type_options() -> list[dict]:
    """Return supported compute types for the settings UI."""
    return [
        {
            "id": "float16",
            "label": "float16  (Recommended for GPU — best speed/quality)",
        },
        {
            "id": "int8_float16",
            "label": "int8_float16  (GPU — slightly less VRAM, negligible quality loss)",
        },
        {
            "id": "int8",
            "label": "int8  (CPU / low-VRAM GPU)",
        },
        {
            "id": "float32",
            "label": "float32  (CPU fallback, slowest)",
        },
    ]
