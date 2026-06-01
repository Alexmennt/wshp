"""
ui/settings.py
──────────────
Settings dialog for wshp.

Sections
────────
  • Model        – model size, device, compute type, language, beam size
  • Hotkeys      – PTT key, toggle key, cancel key, mode
  • Audio        – microphone device, VAD aggressiveness, silence duration
  • Injection    – method (clipboard / keyboard), clipboard restore, delays
  • Overlay      – show/hide, position, opacity
  • About        – version / links

All changes are applied immediately to the in-memory Config object.
The caller is responsible for calling save_config() if they want persistence.
"""

from __future__ import annotations

import logging
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont, QKeySequence
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from config import (
    Config,
    get_compute_type_options,
    get_model_options,
    save_config,
)

logger = logging.getLogger(__name__)

# ── Style constants ────────────────────────────────────────────────────────────

_STYLE_SHEET = """
QDialog {
    background-color: #1e1e24;
    color: #e0e0e8;
}

QTabWidget::pane {
    border: 1px solid #3a3a4a;
    background: #1e1e24;
    border-radius: 6px;
}

QTabBar::tab {
    background: #2a2a34;
    color: #a0a0b0;
    padding: 8px 20px;
    border: 1px solid #3a3a4a;
    border-bottom: none;
    border-top-left-radius: 5px;
    border-top-right-radius: 5px;
    min-width: 80px;
}

QTabBar::tab:selected {
    background: #1e1e24;
    color: #e0e0f0;
    border-bottom: 2px solid #5c85ff;
}

QTabBar::tab:hover:!selected {
    background: #32323e;
    color: #c0c0d0;
}

QGroupBox {
    color: #9090a8;
    border: 1px solid #3a3a4a;
    border-radius: 6px;
    margin-top: 12px;
    padding: 12px 8px 8px 8px;
    font-size: 11px;
    font-weight: bold;
    letter-spacing: 0.5px;
    text-transform: uppercase;
}

QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    top: -1px;
    padding: 0 4px;
    background-color: #1e1e24;
}

QLabel {
    color: #c0c0d0;
    font-size: 13px;
}

QLabel#hint {
    color: #707088;
    font-size: 11px;
    font-style: italic;
}

QComboBox, QSpinBox, QDoubleSpinBox, QKeySequenceEdit {
    background-color: #2a2a38;
    color: #e0e0f0;
    border: 1px solid #4a4a60;
    border-radius: 5px;
    padding: 5px 8px;
    font-size: 13px;
    min-height: 26px;
    selection-background-color: #4060c0;
}

QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QKeySequenceEdit:focus {
    border: 1px solid #5c85ff;
}

QComboBox::drop-down {
    border: none;
    width: 24px;
}

QComboBox QAbstractItemView {
    background-color: #2a2a38;
    color: #e0e0f0;
    border: 1px solid #4a4a60;
    selection-background-color: #3a50a0;
}

QCheckBox {
    color: #c0c0d0;
    font-size: 13px;
    spacing: 8px;
}

QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border-radius: 3px;
    border: 1px solid #5a5a78;
    background: #2a2a38;
}

QCheckBox::indicator:checked {
    background-color: #5c85ff;
    border-color: #5c85ff;
    image: none;
}

QCheckBox::indicator:checked {
    background-color: #4060d0;
}

QSlider::groove:horizontal {
    height: 4px;
    background: #3a3a50;
    border-radius: 2px;
}

QSlider::handle:horizontal {
    background: #5c85ff;
    border: none;
    width: 16px;
    height: 16px;
    margin: -6px 0;
    border-radius: 8px;
}

QSlider::sub-page:horizontal {
    background: #4060c0;
    border-radius: 2px;
}

QPushButton {
    background-color: #2e2e3e;
    color: #d0d0e0;
    border: 1px solid #4a4a60;
    border-radius: 5px;
    padding: 6px 16px;
    font-size: 13px;
    min-height: 28px;
}

QPushButton:hover {
    background-color: #3a3a50;
    border-color: #6a6a88;
}

QPushButton:pressed {
    background-color: #252535;
}

QPushButton#primary {
    background-color: #3a5ad0;
    border-color: #4060d0;
    color: #ffffff;
    font-weight: bold;
}

QPushButton#primary:hover {
    background-color: #4a6ae0;
}

QDialogButtonBox QPushButton {
    min-width: 90px;
}

QScrollArea {
    border: none;
    background: transparent;
}

QFrame#separator {
    color: #3a3a4a;
}
"""


# ── Helpers ────────────────────────────────────────────────────────────────────


def _hint(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("hint")
    lbl.setWordWrap(True)
    return lbl


def _separator() -> QFrame:
    line = QFrame()
    line.setObjectName("separator")
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Sunken)
    return line


def _group(title: str, layout: QVBoxLayout | QFormLayout | None = None) -> QGroupBox:
    box = QGroupBox(title)
    if layout is not None:
        box.setLayout(layout)
    return box


# ── Hotkey capture widget ──────────────────────────────────────────────────────


class HotkeyButton(QPushButton):
    """
    A button that captures a single key press and reports it as a config
    key-name string (e.g. "right_shift", "f9", "z").

    Click the button, then press the desired key.  Press Escape to cancel.
    """

    key_captured = pyqtSignal(str)  # emits the key-name string

    # Map Qt.Key → config key name string
    _QT_KEY_MAP: dict[Qt.Key, str] = {
        Qt.Key.Key_Shift: "right_shift",  # fallback
        Qt.Key.Key_Control: "left_ctrl",
        Qt.Key.Key_Alt: "left_alt",
        Qt.Key.Key_CapsLock: "caps_lock",
        Qt.Key.Key_Tab: "tab",
        Qt.Key.Key_Escape: "escape",
        Qt.Key.Key_Insert: "insert",
        Qt.Key.Key_Delete: "delete",
        Qt.Key.Key_Home: "home",
        Qt.Key.Key_End: "end",
        Qt.Key.Key_PageUp: "page_up",
        Qt.Key.Key_PageDown: "page_down",
        Qt.Key.Key_Up: "up",
        Qt.Key.Key_Down: "down",
        Qt.Key.Key_Left: "left",
        Qt.Key.Key_Right: "right",
        Qt.Key.Key_Backspace: "backspace",
        Qt.Key.Key_Return: "enter",
        Qt.Key.Key_Enter: "enter",
        Qt.Key.Key_Space: "space",
        Qt.Key.Key_F1: "f1",
        Qt.Key.Key_F2: "f2",
        Qt.Key.Key_F3: "f3",
        Qt.Key.Key_F4: "f4",
        Qt.Key.Key_F5: "f5",
        Qt.Key.Key_F6: "f6",
        Qt.Key.Key_F7: "f7",
        Qt.Key.Key_F8: "f8",
        Qt.Key.Key_F9: "f9",
        Qt.Key.Key_F10: "f10",
        Qt.Key.Key_F11: "f11",
        Qt.Key.Key_F12: "f12",
        Qt.Key.Key_Print: "print_screen",
        Qt.Key.Key_ScrollLock: "scroll_lock",
        Qt.Key.Key_Pause: "pause",
        Qt.Key.Key_NumLock: "num_lock",
        Qt.Key.Key_Menu: "menu",
    }

    def __init__(self, key_name: str = "", parent=None) -> None:
        super().__init__(parent)
        self._key_name = key_name
        self._listening = False
        self._update_label()
        self.clicked.connect(self._start_listening)
        self.setMinimumWidth(160)

    def set_key(self, key_name: str) -> None:
        self._key_name = key_name
        self._listening = False
        self._update_label()

    def current_key(self) -> str:
        return self._key_name

    def _start_listening(self) -> None:
        self._listening = True
        self.setText("Press a key…")
        self.setFocus()

    def _update_label(self) -> None:
        display = (
            self._key_name.replace("_", " ").title() if self._key_name else "(none)"
        )
        self.setText(display)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if not self._listening:
            super().keyPressEvent(event)
            return

        qt_key = Qt.Key(event.key())

        # Cancel on Escape
        if qt_key == Qt.Key.Key_Escape:
            self._listening = False
            self._update_label()
            return

        # Ignore pure modifier-only presses except when they're the target
        modifiers = event.modifiers()

        # Try to determine if this is a special key we know about
        key_name = self._resolve_key(event)
        if key_name:
            self._key_name = key_name
            self._listening = False
            self._update_label()
            self.key_captured.emit(key_name)

        event.accept()

    def _resolve_key(self, event) -> Optional[str]:
        qt_key = Qt.Key(event.key())
        modifiers = event.modifiers()

        # Handle sided modifiers via native scan codes where possible
        # Qt doesn't expose left/right shift easily — use nativeScanCode
        sc = event.nativeScanCode()

        if qt_key in (Qt.Key.Key_Shift,):
            # Scan code 42 = left shift, 54 = right shift (US keyboard)
            return "right_shift" if sc == 54 else "left_shift"

        if qt_key in (Qt.Key.Key_Control,):
            return "right_ctrl" if sc in (285, 29 + 256) else "left_ctrl"

        if qt_key in (Qt.Key.Key_Alt,):
            return "right_alt" if sc in (312, 56 + 256) else "left_alt"

        if qt_key in self._QT_KEY_MAP:
            return self._QT_KEY_MAP[qt_key]

        # Printable single character (letter, digit, symbol)
        text = event.text().strip()
        if text and len(text) == 1 and text.isprintable():
            return text.lower()

        return None


# ── Tab pages ─────────────────────────────────────────────────────────────────


class _ModelTab(QWidget):
    def __init__(self, config: Config, parent=None) -> None:
        super().__init__(parent)
        self._cfg = config
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(12)
        root.setContentsMargins(16, 16, 16, 16)

        # ── Model selection ──────────────────────────────────────────────────
        model_grp_layout = QFormLayout()
        model_grp_layout.setSpacing(10)
        model_grp_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._model_combo = QComboBox()
        for opt in get_model_options():
            self._model_combo.addItem(opt["label"], opt["id"])
        self._set_combo(self._model_combo, self._cfg.model_size)
        model_grp_layout.addRow("Model:", self._model_combo)
        model_grp_layout.addRow(
            "",
            _hint(
                "large-v3-turbo is the recommended default: best accuracy, "
                "fastest inference, lowest VRAM (~2.5 GB)."
            ),
        )

        # Device
        self._device_combo = QComboBox()
        for label, val in [
            ("Auto-detect (recommended)", "auto"),
            ("CUDA (GPU)", "cuda"),
            ("CPU (slow for large models)", "cpu"),
        ]:
            self._device_combo.addItem(label, val)
        self._set_combo(self._device_combo, self._cfg.device)
        model_grp_layout.addRow("Device:", self._device_combo)

        # Compute type
        self._compute_combo = QComboBox()
        for opt in get_compute_type_options():
            self._compute_combo.addItem(opt["label"], opt["id"])
        self._set_combo(self._compute_combo, self._cfg.compute_type)
        model_grp_layout.addRow("Precision:", self._compute_combo)
        model_grp_layout.addRow(
            "",
            _hint("float16 is optimal for NVIDIA GPUs.  Use int8 or float32 for CPU."),
        )

        # Language
        self._lang_combo = QComboBox()
        self._lang_combo.setEditable(True)
        languages = [
            ("Auto-detect", ""),
            ("English", "en"),
            ("French", "fr"),
            ("German", "de"),
            ("Spanish", "es"),
            ("Italian", "it"),
            ("Portuguese", "pt"),
            ("Dutch", "nl"),
            ("Polish", "pl"),
            ("Russian", "ru"),
            ("Japanese", "ja"),
            ("Chinese", "zh"),
            ("Korean", "ko"),
            ("Arabic", "ar"),
            ("Hindi", "hi"),
        ]
        for label, code in languages:
            self._lang_combo.addItem(label, code)
        current_lang = self._cfg.language or ""
        idx = self._lang_combo.findData(current_lang)
        if idx >= 0:
            self._lang_combo.setCurrentIndex(idx)
        else:
            self._lang_combo.setCurrentText(current_lang)
        model_grp_layout.addRow("Language:", self._lang_combo)
        model_grp_layout.addRow(
            "",
            _hint(
                "Set a specific language to improve accuracy and speed.  "
                "Auto-detect adds a small overhead."
            ),
        )

        # Beam size
        self._beam_spin = QSpinBox()
        self._beam_spin.setRange(1, 10)
        self._beam_spin.setValue(self._cfg.beam_size)
        self._beam_spin.setToolTip(
            "Higher = slightly more accurate but slower.  Default: 5."
        )
        model_grp_layout.addRow("Beam size:", self._beam_spin)

        root.addWidget(_group("Whisper Model", model_grp_layout))

        # ── Initial prompt ───────────────────────────────────────────────────
        from PyQt6.QtWidgets import QTextEdit

        prompt_grp_layout = QVBoxLayout()
        self._prompt_edit = QTextEdit()
        self._prompt_edit.setPlainText(self._cfg.initial_prompt)
        self._prompt_edit.setFixedHeight(70)
        self._prompt_edit.setPlaceholderText(
            "Optional: hint the model with terminology, names, or style.  "
            "e.g. 'Meeting notes: Alice, Bob, Charlie.'"
        )
        prompt_grp_layout.addWidget(self._prompt_edit)
        prompt_grp_layout.addWidget(
            _hint(
                "The initial prompt is prepended to every transcription.  "
                "It helps with domain-specific vocabulary and punctuation style."
            )
        )
        root.addWidget(_group("Initial Prompt", prompt_grp_layout))

        root.addStretch()

    # ── Getters ───────────────────────────────────────────────────────────────

    def apply(self, cfg: Config) -> None:
        cfg.model_size = self._model_combo.currentData() or "large-v3-turbo"
        cfg.device = self._device_combo.currentData() or "auto"
        cfg.compute_type = self._compute_combo.currentData() or "float16"
        lang = self._lang_combo.currentData()
        if lang is None:
            lang = self._lang_combo.currentText().strip()
        cfg.language = lang if lang else None
        cfg.beam_size = self._beam_spin.value()
        cfg.initial_prompt = self._prompt_edit.toPlainText().strip()

    @staticmethod
    def _set_combo(combo: QComboBox, value: str) -> None:
        idx = combo.findData(value)
        if idx >= 0:
            combo.setCurrentIndex(idx)


class _HotkeyTab(QWidget):
    def __init__(self, config: Config, parent=None) -> None:
        super().__init__(parent)
        self._cfg = config
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(12)
        root.setContentsMargins(16, 16, 16, 16)

        # ── Mode ─────────────────────────────────────────────────────────────
        mode_layout = QFormLayout()
        mode_layout.setSpacing(10)
        mode_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._mode_combo = QComboBox()
        modes = [
            ("Push-to-Talk  (hold key while speaking)", "ptt"),
            ("Toggle  (press once to start, again to stop)", "toggle"),
            ("Auto-VAD  (always listening, VAD decides)", "vad"),
        ]
        for label, val in modes:
            self._mode_combo.addItem(label, val)
        idx = self._mode_combo.findData(self._cfg.mode)
        self._mode_combo.setCurrentIndex(max(0, idx))
        mode_layout.addRow("Mode:", self._mode_combo)
        mode_layout.addRow(
            "",
            _hint(
                "Push-to-Talk is the most privacy-friendly option.  "
                "Auto-VAD requires no key press but listens continuously."
            ),
        )
        root.addWidget(_group("Operating Mode", mode_layout))

        # ── Keys ─────────────────────────────────────────────────────────────
        key_layout = QFormLayout()
        key_layout.setSpacing(10)
        key_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._ptt_btn = HotkeyButton(self._cfg.hotkey_ptt)
        key_layout.addRow("PTT / Start key:", self._ptt_btn)
        key_layout.addRow(
            "",
            _hint(
                "Hold this key while speaking (PTT mode) or press to start/stop (Toggle mode)."
            ),
        )

        self._toggle_btn = HotkeyButton(self._cfg.hotkey_toggle)
        key_layout.addRow("Toggle key:", self._toggle_btn)
        key_layout.addRow(
            "",
            _hint("Alternative key for toggle mode. Also enables/disables Auto-VAD."),
        )

        self._cancel_btn = HotkeyButton(self._cfg.hotkey_cancel)
        key_layout.addRow("Cancel key:", self._cancel_btn)
        key_layout.addRow(
            "", _hint("Abort the current recording without transcribing.")
        )

        root.addWidget(_group("Key Bindings", key_layout))
        root.addStretch()

    def apply(self, cfg: Config) -> None:
        cfg.mode = self._mode_combo.currentData() or "ptt"
        cfg.hotkey_ptt = self._ptt_btn.current_key() or "right_shift"
        cfg.hotkey_toggle = self._toggle_btn.current_key() or "f9"
        cfg.hotkey_cancel = self._cancel_btn.current_key() or "escape"


class _AudioTab(QWidget):
    def __init__(self, config: Config, parent=None) -> None:
        super().__init__(parent)
        self._cfg = config
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(12)
        root.setContentsMargins(16, 16, 16, 16)

        # ── Microphone ───────────────────────────────────────────────────────
        mic_layout = QFormLayout()
        mic_layout.setSpacing(10)
        mic_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._mic_combo = QComboBox()
        self._populate_mics()
        mic_layout.addRow("Input device:", self._mic_combo)
        mic_layout.addRow(
            "",
            _hint(
                "Choose the microphone or audio input device to record from.  "
                "Restart recording after changing."
            ),
        )
        root.addWidget(_group("Microphone", mic_layout))

        # ── VAD ───────────────────────────────────────────────────────────────
        vad_layout = QFormLayout()
        vad_layout.setSpacing(10)
        vad_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._vad_agg_slider = QSlider(Qt.Orientation.Horizontal)
        self._vad_agg_slider.setRange(0, 3)
        self._vad_agg_slider.setValue(self._cfg.vad_aggressiveness)
        self._vad_agg_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self._vad_agg_slider.setTickInterval(1)
        self._vad_agg_label = QLabel(self._vad_label(self._cfg.vad_aggressiveness))
        self._vad_agg_slider.valueChanged.connect(
            lambda v: self._vad_agg_label.setText(self._vad_label(v))
        )

        agg_row = QHBoxLayout()
        agg_row.addWidget(self._vad_agg_slider)
        agg_row.addWidget(self._vad_agg_label)
        agg_container = QWidget()
        agg_container.setLayout(agg_row)
        vad_layout.addRow("Aggressiveness:", agg_container)
        vad_layout.addRow(
            "",
            _hint(
                "Higher values reject more background noise but may cut off soft speech."
            ),
        )

        self._silence_spin = QDoubleSpinBox()
        self._silence_spin.setRange(0.2, 5.0)
        self._silence_spin.setSingleStep(0.1)
        self._silence_spin.setDecimals(1)
        self._silence_spin.setSuffix(" s")
        self._silence_spin.setValue(self._cfg.vad_silence_duration)
        vad_layout.addRow("Silence duration:", self._silence_spin)
        vad_layout.addRow(
            "",
            _hint(
                "How long the VAD waits after speech ends before finalising the recording.  "
                "Shorter = snappier; longer = catches trailing words."
            ),
        )

        self._min_speech_spin = QDoubleSpinBox()
        self._min_speech_spin.setRange(0.1, 5.0)
        self._min_speech_spin.setSingleStep(0.1)
        self._min_speech_spin.setDecimals(1)
        self._min_speech_spin.setSuffix(" s")
        self._min_speech_spin.setValue(self._cfg.min_speech_duration)
        vad_layout.addRow("Minimum speech:", self._min_speech_spin)
        vad_layout.addRow(
            "",
            _hint(
                "Recordings shorter than this are silently discarded (avoids transcribing coughs / accidental key presses)."
            ),
        )

        root.addWidget(_group("Voice Activity Detection", vad_layout))
        root.addStretch()

    def _populate_mics(self) -> None:
        self._mic_combo.clear()
        self._mic_combo.addItem("System default", None)

        try:
            from audio.recorder import AudioRecorder

            devices = AudioRecorder.list_input_devices()
            for dev in devices:
                label = f"[{dev['index']}] {dev['name']}"
                if dev.get("default"):
                    label += "  ★"
                self._mic_combo.addItem(label, dev["index"])
        except Exception as exc:
            logger.warning("Could not enumerate audio devices: %s", exc)

        # Select current
        current = self._cfg.microphone_index
        if current is not None:
            idx = self._mic_combo.findData(current)
            if idx >= 0:
                self._mic_combo.setCurrentIndex(idx)

    @staticmethod
    def _vad_label(value: int) -> str:
        return ["0 – Off", "1 – Low", "2 – Medium", "3 – High"][value]

    def apply(self, cfg: Config) -> None:
        cfg.microphone_index = self._mic_combo.currentData()
        cfg.vad_aggressiveness = self._vad_agg_slider.value()
        cfg.vad_silence_duration = self._silence_spin.value()
        cfg.min_speech_duration = self._min_speech_spin.value()


class _InjectionTab(QWidget):
    def __init__(self, config: Config, parent=None) -> None:
        super().__init__(parent)
        self._cfg = config
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(12)
        root.setContentsMargins(16, 16, 16, 16)

        # ── Method ───────────────────────────────────────────────────────────
        method_layout = QFormLayout()
        method_layout.setSpacing(10)
        method_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._method_combo = QComboBox()
        self._method_combo.addItem("Clipboard  (Ctrl+V) — recommended", "clipboard")
        self._method_combo.addItem(
            "Keyboard simulation — slower, works in all apps", "keyboard"
        )
        idx = self._method_combo.findData(self._cfg.injection_method)
        self._method_combo.setCurrentIndex(max(0, idx))
        method_layout.addRow("Method:", self._method_combo)
        method_layout.addRow(
            "",
            _hint(
                "Clipboard mode copies the text and sends Ctrl+V — fast and "
                "handles all Unicode / emoji.  Keyboard mode simulates individual "
                "key presses and works even in apps that block paste."
            ),
        )

        self._restore_cb = QCheckBox("Restore clipboard after injection")
        self._restore_cb.setChecked(self._cfg.restore_clipboard)
        method_layout.addRow("", self._restore_cb)

        root.addWidget(_group("Text Injection", method_layout))

        # ── Timing ───────────────────────────────────────────────────────────
        timing_layout = QFormLayout()
        timing_layout.setSpacing(10)
        timing_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._focus_delay_spin = QSpinBox()
        self._focus_delay_spin.setRange(0, 500)
        self._focus_delay_spin.setSingleStep(10)
        self._focus_delay_spin.setSuffix(" ms")
        self._focus_delay_spin.setValue(self._cfg.focus_delay_ms)
        timing_layout.addRow("Focus delay:", self._focus_delay_spin)
        timing_layout.addRow(
            "",
            _hint(
                "Time to wait after bringing the target window to focus before "
                "sending Ctrl+V.  Increase if text is being missed."
            ),
        )

        self._kb_delay_spin = QDoubleSpinBox()
        self._kb_delay_spin.setRange(0.0, 0.1)
        self._kb_delay_spin.setSingleStep(0.001)
        self._kb_delay_spin.setDecimals(3)
        self._kb_delay_spin.setSuffix(" s / char")
        self._kb_delay_spin.setValue(self._cfg.keyboard_injection_delay)
        timing_layout.addRow("Keyboard delay:", self._kb_delay_spin)
        timing_layout.addRow(
            "",
            _hint(
                "Per-character delay in keyboard simulation mode.  "
                "Set to 0 for maximum speed."
            ),
        )

        root.addWidget(_group("Timing", timing_layout))
        root.addStretch()

    def apply(self, cfg: Config) -> None:
        cfg.injection_method = self._method_combo.currentData() or "clipboard"
        cfg.restore_clipboard = self._restore_cb.isChecked()
        cfg.focus_delay_ms = self._focus_delay_spin.value()
        cfg.keyboard_injection_delay = self._kb_delay_spin.value()


class _OverlayTab(QWidget):
    def __init__(self, config: Config, parent=None) -> None:
        super().__init__(parent)
        self._cfg = config
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(12)
        root.setContentsMargins(16, 16, 16, 16)

        overlay_layout = QFormLayout()
        overlay_layout.setSpacing(10)
        overlay_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._show_cb = QCheckBox("Show overlay window")
        self._show_cb.setChecked(self._cfg.show_overlay)
        overlay_layout.addRow("", self._show_cb)

        self._pos_combo = QComboBox()
        positions = [
            ("Bottom Right", "bottom_right"),
            ("Bottom Left", "bottom_left"),
            ("Top Right", "top_right"),
            ("Top Left", "top_left"),
        ]
        for label, val in positions:
            self._pos_combo.addItem(label, val)
        idx = self._pos_combo.findData(self._cfg.overlay_position)
        self._pos_combo.setCurrentIndex(max(0, idx))
        overlay_layout.addRow("Position:", self._pos_combo)

        self._opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self._opacity_slider.setRange(30, 100)
        self._opacity_slider.setValue(int(self._cfg.overlay_opacity * 100))
        self._opacity_label = QLabel(f"{int(self._cfg.overlay_opacity * 100)}%")
        self._opacity_slider.valueChanged.connect(
            lambda v: self._opacity_label.setText(f"{v}%")
        )
        opacity_row = QHBoxLayout()
        opacity_row.addWidget(self._opacity_slider)
        opacity_row.addWidget(self._opacity_label)
        opacity_container = QWidget()
        opacity_container.setLayout(opacity_row)
        overlay_layout.addRow("Opacity:", opacity_container)
        overlay_layout.addRow(
            "", _hint("The overlay can also be dragged to any position on screen.")
        )

        root.addWidget(_group("Overlay", overlay_layout))
        root.addStretch()

    def apply(self, cfg: Config) -> None:
        cfg.show_overlay = self._show_cb.isChecked()
        pos = self._pos_combo.currentData() or "bottom_right"
        if pos != "custom":
            cfg.overlay_position = pos
        cfg.overlay_opacity = self._opacity_slider.value() / 100.0


class _AboutTab(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(16)
        root.setContentsMargins(24, 24, 24, 24)
        root.setAlignment(Qt.AlignmentFlag.AlignTop)

        title = QLabel("wshp")
        title_font = QFont("Segoe UI", 24, QFont.Weight.Bold)
        title.setFont(title_font)
        title.setStyleSheet("color: #9090ff;")
        root.addWidget(title)

        subtitle = QLabel("Local Whisper Dictation for Windows")
        subtitle_font = QFont("Segoe UI", 13)
        subtitle.setFont(subtitle_font)
        subtitle.setStyleSheet("color: #8080a0;")
        root.addWidget(subtitle)

        root.addWidget(_separator())

        info_lines = [
            ("Engine", "faster-whisper (CTranslate2)"),
            ("Default model", "large-v3-turbo  (~2.5 GB VRAM)"),
            ("Max model (12 GB card)", "large-v3  (~4.5 GB VRAM, float16)"),
            ("Audio", "sounddevice @ 16 kHz mono"),
            ("VAD", "WebRTC VAD (energy fallback)"),
        ]
        grid_layout = QFormLayout()
        grid_layout.setSpacing(8)
        grid_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        for label, value in info_lines:
            key_lbl = QLabel(label + ":")
            key_lbl.setStyleSheet("color: #7070a0;")
            val_lbl = QLabel(value)
            val_lbl.setStyleSheet("color: #d0d0e0;")
            grid_layout.addRow(key_lbl, val_lbl)
        root.addLayout(grid_layout)

        root.addWidget(_separator())

        model_dir_lbl = QLabel(
            "Models are cached in <code>~/.wshp/models/</code>.  "
            "The first run will download the selected model automatically."
        )
        model_dir_lbl.setStyleSheet("color: #7878a0; font-size: 12px;")
        model_dir_lbl.setWordWrap(True)
        root.addWidget(model_dir_lbl)

        root.addStretch()


# ── Main settings dialog ──────────────────────────────────────────────────────


class SettingsDialog(QDialog):
    """
    Modal settings dialog.

    Signals
    -------
    settings_applied(Config)
        Emitted when the user clicks OK or Apply with the updated config.
    """

    settings_applied = pyqtSignal(object)  # Config

    def __init__(self, config: Config, parent=None) -> None:
        super().__init__(parent)
        self._cfg = config
        self._build()
        self.setStyleSheet(_STYLE_SHEET)
        self.setWindowTitle("wshp Settings")
        self.setMinimumSize(600, 520)
        self.resize(660, 580)

        # Set the wshp logo as the window icon (shows in the title bar and taskbar)
        try:
            from assets import get_qicon

            self.setWindowIcon(get_qicon())
        except Exception:
            pass

        # Centre on screen
        from PyQt6.QtGui import QGuiApplication

        screen = QGuiApplication.primaryScreen()
        if screen:
            sg = screen.availableGeometry()
            self.move(
                sg.center().x() - self.width() // 2,
                sg.center().y() - self.height() // 2,
            )

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Tab widget ────────────────────────────────────────────────────────
        self._tabs = QTabWidget()

        self._model_tab = _ModelTab(self._cfg)
        self._hotkey_tab = _HotkeyTab(self._cfg)
        self._audio_tab = _AudioTab(self._cfg)
        self._injection_tab = _InjectionTab(self._cfg)
        self._overlay_tab = _OverlayTab(self._cfg)
        self._about_tab = _AboutTab()

        # Wrap each tab in a QScrollArea so it works on smaller screens
        for widget, label in [
            (self._model_tab, "🤖  Model"),
            (self._hotkey_tab, "⌨️  Hotkeys"),
            (self._audio_tab, "🎙️  Audio"),
            (self._injection_tab, "📋  Injection"),
            (self._overlay_tab, "🪟  Overlay"),
            (self._about_tab, "ℹ️  About"),
        ]:
            scroll = QScrollArea()
            scroll.setWidget(widget)
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            self._tabs.addTab(scroll, label)

        root.addWidget(self._tabs)

        # ── Button bar ────────────────────────────────────────────────────────
        btn_bar = QHBoxLayout()
        btn_bar.setContentsMargins(16, 8, 16, 12)

        self._restart_note = QLabel(
            "⚠  Model changes take effect after restarting the engine."
        )
        self._restart_note.setObjectName("hint")
        self._restart_note.hide()
        btn_bar.addWidget(self._restart_note)

        btn_bar.addStretch()

        self._apply_btn = QPushButton("Apply")
        self._apply_btn.setObjectName("primary")
        self._apply_btn.clicked.connect(self._on_apply)
        btn_bar.addWidget(self._apply_btn)

        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btn_box.accepted.connect(self._on_ok)
        btn_box.rejected.connect(self.reject)
        btn_bar.addWidget(btn_box)

        root.addLayout(btn_bar)

        # Watch for model changes to show the restart note
        self._model_tab._model_combo.currentIndexChanged.connect(
            lambda: self._restart_note.setVisible(True)
        )
        self._model_tab._device_combo.currentIndexChanged.connect(
            lambda: self._restart_note.setVisible(True)
        )
        self._model_tab._compute_combo.currentIndexChanged.connect(
            lambda: self._restart_note.setVisible(True)
        )

    # ── Slots ─────────────────────────────────────────────────────────────────

    def _on_apply(self) -> None:
        self._collect_and_emit()

    def _on_ok(self) -> None:
        self._collect_and_emit()
        self.accept()

    def _collect_and_emit(self) -> None:
        """Gather values from all tabs, update config, save, and emit signal."""
        old_model = self._cfg.model_size

        self._model_tab.apply(self._cfg)
        self._hotkey_tab.apply(self._cfg)
        self._audio_tab.apply(self._cfg)
        self._injection_tab.apply(self._cfg)
        self._overlay_tab.apply(self._cfg)

        save_config(self._cfg)
        self.settings_applied.emit(self._cfg)
        logger.info("Settings saved.")
