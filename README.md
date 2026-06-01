# wshp — Local Whisper Dictation for Windows

A fully local, privacy-first Whisper dictation tool for Windows. Speak into your microphone; text appears instantly in whatever app has focus — no cloud, no subscriptions, no data leaving your machine.

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue?logo=python)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Platform: Windows](https://img.shields.io/badge/platform-Windows%2010%2F11-blue?logo=windows)](https://www.microsoft.com/windows)
[![CUDA 12](https://img.shields.io/badge/CUDA-12-76B900?logo=nvidia)](https://developer.nvidia.com/cuda-downloads)

---

## Features

- **Push-to-Talk** — hold a key while speaking, release to transcribe  
- **Toggle mode** — press once to start recording, press again to stop  
- **Auto-VAD mode** — always listening; voice activity detection decides when to transcribe  
- **Local inference** — powered by [faster-whisper](https://github.com/SYSTRAN/faster-whisper) + CTranslate2  
- **CUDA accelerated** — uses your GPU for near-real-time transcription  
- **Clipboard injection** — pastes text into any app via Ctrl+V (handles Unicode/emoji)  
- **Floating overlay** — small always-on-top status pill (draggable)  
- **System tray** — lives quietly in your taskbar notification area  
- **Settings UI** — full dark-themed settings dialog; no config file editing needed  
- **Persistent config** — settings saved to `~/.wshp/config.json`  
- **Model cache** — Whisper models downloaded once to `~/.wshp/models/`  

---

## Recommended Models (12 GB VRAM Card)

| Model | VRAM (float16) | Speed | Accuracy | Notes |
|---|---|---|---|---|
| **large-v3-turbo** ⭐ | ~2.5 GB | Fastest | Highest | **Default — best all-round** |
| large-v3 | ~4.5 GB | Fast | Highest | Slightly more accurate on accents |
| distil-large-v3 | ~2.4 GB | Fast | Very high | Distilled, English-focused |
| medium | ~1.5 GB | Very fast | Good | If you need lower latency |

With a 12 GB card you can comfortably run `large-v3-turbo` or even `large-v3` —  
both leave plenty of VRAM headroom for other applications.

---

## Requirements

| Requirement | Notes |
|---|---|
| Python 3.10 + | 3.11 or 3.12 recommended |
| NVIDIA GPU | CUDA 12.x + cuDNN 9 for GPU acceleration |
| Windows 10 / 11 | 64-bit |
| Microphone | Any input device recognised by Windows |

### CUDA Libraries

faster-whisper requires **cuBLAS** and **cuDNN 9** for CUDA 12.  
The easiest way to get them on Windows is via the official NVIDIA installers or conda:

```bash
# Via conda (easiest)
conda install -c conda-forge cudatoolkit=12 cudnn=9

# Or manually install CUDA Toolkit 12.x from:
# https://developer.nvidia.com/cuda-downloads
# And cuDNN 9 from:
# https://developer.nvidia.com/cudnn-downloads
```

---

## Installation

```bash
# 1. Clone / download the project
cd wshp

# 2. Create a virtual environment (recommended)
python -m venv .venv
.venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. (Optional) If webrtcvad fails to install, the app falls back to
#    energy-based VAD automatically — no action needed.
```

### Verify CUDA is detected

```python
import ctranslate2
print(ctranslate2.get_cuda_device_count())  # Should print 1 or more
```

---

## Running

```bash
# With the virtual environment activated:
python main.py

# Or directly:
.venv\Scripts\python main.py
```

On first launch, wshp will:
1. Start in the system tray (look for the **W** icon in your notification area)
2. Download the selected Whisper model (~1–3 GB depending on model) — one time only
3. Load the model onto the GPU (~5–15 seconds)
4. Show "Ready" in the overlay — you can start dictating

---

## Usage

### Default Hotkeys

| Key | Action |
|---|---|
| **Right Shift** (hold) | Record while held; release to transcribe (PTT mode) |
| **F9** | Toggle recording on/off (Toggle mode) / Enable VAD |
| **Escape** | Cancel current recording without transcribing |

> All hotkeys are configurable in **Settings → Hotkeys**.

### Workflow (Push-to-Talk mode)

1. Click into any text field in any application  
2. Hold **Right Shift**  
3. Speak naturally  
4. Release **Right Shift**  
5. Wait ~1–3 seconds for transcription  
6. Text appears at the cursor position  

### Workflow (Auto-VAD mode)

1. Switch to VAD mode via tray icon → **Mode → Auto-VAD**  
2. wshp listens continuously  
3. When you speak, recording starts automatically  
4. When you stop speaking (silence > 0.8 s), transcription begins  
5. Text is injected into the focused application  

---

## System Tray Menu

Right-click the tray icon (**W**) for the full menu:

- **Mode** → Push-to-Talk / Toggle / Auto-VAD  
- **Hide/Show Overlay** — toggle the floating status pill  
- **Pause Hotkeys** — temporarily disable all hotkeys  
- **⚙ Settings…** — open the full settings dialog  
- **Quit wshp**  

Double-click the tray icon to toggle the overlay.

---

## Settings

Open via tray → **⚙ Settings…** or press **F10** (if configured).

### Model tab
- **Model size** — select from large-v3-turbo (default) down to base
- **Device** — Auto / CUDA / CPU
- **Precision** — float16 (GPU), int8_float16, int8, float32
- **Language** — auto-detect or pin to a specific language for speed/accuracy
- **Beam size** — 1–10; higher = marginally more accurate, slower
- **Initial prompt** — seed Whisper with domain vocabulary

### Hotkeys tab
- Change PTT key, toggle key, cancel key (click the button then press the desired key)
- Switch operating mode

### Audio tab
- Select microphone device
- VAD aggressiveness (0–3)
- Silence duration before auto-stop
- Minimum speech length (shorter clips are discarded)

### Injection tab
- **Clipboard** (default) — fastest, handles all Unicode
- **Keyboard** — character-by-character simulation (for apps blocking paste)
- Clipboard restore, focus delay, per-character delay

### Overlay tab
- Show / hide the floating status pill
- Corner position (top/bottom left/right)
- Opacity

---

## Overlay

The overlay is a small rounded pill anchored to a screen corner.

| Colour | Meaning |
|---|---|
| 🔵 Blue | Loading model |
| ⚪ Grey | Ready / Idle |
| 🔴 Red (blinking) | Recording |
| 🟡 Amber | Transcribing |
| 🟢 Green | Done — showing text preview |

The overlay can be **dragged** anywhere on screen.  
It never steals keyboard focus so hotkeys continue working.

---

## Troubleshooting

### "Model not found" / slow download
Models are downloaded automatically from Hugging Face on first use.  
They are cached in `~/.wshp/models/` and reused on subsequent launches.  
If download fails, check your internet connection or set `HF_ENDPOINT` to a mirror.

### Text appears in the wrong application
wshp captures the target window the moment you press the PTT key.  
Make sure the text field has focus *before* you press the hotkey.  
If text still misses, increase **Injection → Focus delay** in Settings.

### CUDA out of memory
Switch to a smaller model (e.g. `large-v3-turbo` → `medium`) or change  
Precision to `int8_float16` in Settings → Model.

### webrtcvad not installing
This is optional.  If it fails, wshp automatically falls back to energy-based VAD.  
You will see a warning in the console but everything else works normally.

### No CUDA — running on CPU
Install cuBLAS + cuDNN 9 for CUDA 12 (see Requirements above).  
For CPU-only use, set Precision to `int8` or `float32` in Settings and  
use a smaller model (`medium` or `small`) for acceptable speed.

### Hotkey conflicts
If another application uses the same hotkey, change it in Settings → Hotkeys.  
wshp never *suppresses* hotkeys — all keys still reach their target application.

---

## Project Structure

```
wshp/
├── main.py               # Entry point — QApplication + AppController + TrayIcon
├── app_controller.py     # Central coordinator (state machine)
├── config.py             # Config dataclass + JSON persistence
├── audio/
│   ├── recorder.py       # sounddevice InputStream wrapper
│   └── vad.py            # WebRTC VAD (energy fallback)
├── transcription/
│   └── engine.py         # faster-whisper in a QThread
├── input/
│   ├── hotkeys.py        # pynput global hotkey listener
│   └── injector.py       # Clipboard / keyboard text injection
├── ui/
│   ├── overlay.py        # Floating always-on-top status pill
│   ├── tray.py           # QSystemTrayIcon + context menu
│   └── settings.py       # Full settings dialog
└── requirements.txt
```

---

## Building from Source

To build a distributable `wshp.exe` (no Python required on the target machine):

```bash
# Install build dependencies
pip install -r requirements.txt

# Run the build script
python build.py
# Output: dist/wshp/wshp.exe
```

See `build.py` and `wshp.spec` for advanced options (--clean, --ico).

> **Note:** Whisper models (~1–3 GB) are **not** bundled — they are downloaded
> to `%USERPROFILE%\.wshp\models\` on first launch.

---

## Contributing

Contributions are welcome! Please keep in mind:

- **One feature per PR** — small, focused changes are easier to review.
- **No telemetry, no network calls** — the app must remain fully offline after
  the initial model download.
- **Windows only** — the project uses `pywin32`, `pynput`, and Windows-specific
  clipboard APIs. Cross-platform support is not planned.
- **Code style** — follow the existing patterns. Use descriptive names, type
  hints, and docstrings.

1. Fork the repository.
2. Create a feature branch (`git checkout -b feature/my-thing`).
3. Make your changes and test them.
4. Submit a pull request.

---

## Privacy

- **No network requests** after the initial model download  
- **No telemetry** of any kind  
- Audio is processed entirely on your local GPU / CPU  
- The clipboard is saved and restored after injection (configurable)  
- Config is stored locally in `~/.wshp/config.json`  

---

## License

[MIT](LICENSE) — use freely, modify, distribute.
