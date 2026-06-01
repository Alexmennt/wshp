# -*- mode: python ; coding: utf-8 -*-
"""
wshp.spec — PyInstaller build specification
============================================
Produces a one-folder distribution:  dist/wshp/wshp.exe

Run via the friendly build script (recommended):
    python build.py

Or directly:
    pyinstaller --clean wshp.spec

Notes
-----
* CUDA DLLs (cuBLAS, cuDNN) bundled by ctranslate2 ARE included so the
  user does not need a separate CUDA installation for GPU acceleration.
* torch / onnxruntime / cv2 / llvmlite / triton / sklearn are explicitly
  stripped after collection — they are transitive deps pulled in by
  huggingface_hub / tokenizers but are never used at runtime.
* Whisper model files (~1-3 GB) live in ~/.wshp/models/ and are NOT bundled.
* UPX compression is disabled — it can corrupt CUDA/ctranslate2 DLLs.
* vad_filter is disabled in the transcription engine so onnxruntime is
  not needed at runtime (we use webrtcvad / energy VAD at the audio layer).
"""

from PyInstaller.utils.hooks import collect_all  # noqa: E402

block_cipher = None

# ---------------------------------------------------------------------------
# 1. Collect packages that ship native extensions or non-obvious data files
# ---------------------------------------------------------------------------

_COLLECT_PKGS = [
    "ctranslate2",      # CTranslate2 inference engine — core of faster-whisper
    "faster_whisper",   # Whisper model Python wrapper
    "av",               # PyAV / FFmpeg — audio decoding used by faster_whisper
    "tokenizers",       # HuggingFace Rust tokenizer extension
    "huggingface_hub",  # Model downloading & caching
    "sounddevice",      # Real-time audio capture (bundles PortAudio DLL)
    "pynput",           # Global hotkey listener
    "webrtcvad",        # WebRTC Voice Activity Detection (native .pyd)
]

all_datas: list = []
all_binaries: list = []
all_hidden: list = []

for _pkg in _COLLECT_PKGS:
    try:
        _d, _b, _h = collect_all(_pkg)
        all_datas    += _d
        all_binaries += _b
        all_hidden   += _h
    except Exception as _exc:
        print(f"[wshp.spec] WARNING: collect_all('{_pkg}') failed: {_exc}")

# ---------------------------------------------------------------------------
# 2. Strip bloat packages dragged in as transitive dependencies
#    These are never imported at runtime by wshp.
# ---------------------------------------------------------------------------

_BLOAT = {
    "torch",
    "torchvision",
    "torchaudio",
    "torchtext",
    "triton",
    "onnxruntime",
    "onnx",
    "cv2",
    "llvmlite",
    "numba",
    "sklearn",
    "scikit_learn",
    "tensorflow",
    "tensorboard",
    "keras",
}


def _is_bloat(path: str) -> bool:
    """
    Return True if *path* belongs to one of the bloat packages.

    Matches against the first path component that appears after a
    recognised site-packages marker, so we don't accidentally strip
    something like 'ctranslate2/torch_compat.py'.
    """
    normalized = path.replace("\\", "/").lower()
    for marker in ("site-packages/", "dist-packages/", "_internal/"):
        idx = normalized.rfind(marker)
        if idx >= 0:
            remainder = normalized[idx + len(marker):]
            top = remainder.split("/")[0]
            # Normalise dashes → underscores for comparison
            top = top.replace("-", "_").split(".")[0]
            if top in _BLOAT:
                return True
    # Also catch loose DLLs whose *destination* folder is a bloat package
    # (PyInstaller stores dest as the second element of the tuple)
    return False


def _dest_is_bloat(dest: str) -> bool:
    """Return True if the PyInstaller destination dir is a bloat package."""
    top = dest.replace("\\", "/").split("/")[0].replace("-", "_")
    return top in _BLOAT


# Filter collected datas: tuple is (src_path, dest_dir)
all_datas = [
    (src, dst)
    for src, dst in all_datas
    if not _is_bloat(src) and not _dest_is_bloat(dst)
]

# Filter collected binaries: tuple is (src_path, dest_dir)  [PyInstaller 6.x]
all_binaries = [
    (src, dst)
    for src, dst in all_binaries
    if not _is_bloat(src) and not _dest_is_bloat(dst)
]

# Filter hidden imports
all_hidden = [
    h for h in all_hidden
    if h.split(".")[0].replace("-", "_") not in _BLOAT
]

# ---------------------------------------------------------------------------
# 3. Application data files
# ---------------------------------------------------------------------------

all_datas += [
    ("assets/icon.png", "assets"),
    ("assets/icon.ico", "assets"),
]

# ---------------------------------------------------------------------------
# 4. Analysis
# ---------------------------------------------------------------------------

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=all_binaries,
    datas=all_datas,
    hiddenimports=all_hidden + [
        # ── Windows API (pywin32) ──────────────────────────────────────────
        "win32api",
        "win32con",
        "win32gui",
        "win32process",
        "win32event",
        "win32security",
        "pywintypes",

        # ── PyQt6 ─────────────────────────────────────────────────────────
        "PyQt6",
        "PyQt6.sip",
        "PyQt6.QtCore",
        "PyQt6.QtGui",
        "PyQt6.QtWidgets",

        # ── Clipboard ─────────────────────────────────────────────────────
        "pyperclip",

        # ── Imaging ───────────────────────────────────────────────────────
        "PIL",
        "PIL.Image",
        "PIL.ImageDraw",

        # ── Numeric ───────────────────────────────────────────────────────────
        "numpy",

        # ── cffi back-end (sounddevice & webrtcvad) ───────────────────────
        "_cffi_backend",

        # ── Our application packages ───────────────────────────────────────
        "config",
        "app_controller",
        "audio",
        "audio.recorder",
        "audio.vad",
        "transcription",
        "transcription.engine",
        "input",
        "input.hotkeys",
        "input.injector",
        "ui",
        "ui.overlay",
        "ui.tray",
        "ui.settings",
        "assets",
        "assets.icon_gen",
    ],
    # Use local hooks directory first - contains custom webrtcvad hook that
    # overrides the failing system hook (webrtcvad-wheels provides the module
    # but under a different distribution name, causing metadata lookup to fail)
    hookspath=["hooks"],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Heavy packages confirmed not needed at runtime
        "torch",
        "torchvision",
        "torchaudio",
        "torchtext",
        "triton",
        "onnxruntime",
        "onnx",
        "cv2",
        "llvmlite",
        "numba",
        "sklearn",
        "scikit_learn",
        "tensorflow",
        "tensorboard",
        "keras",
        # UI toolkits we don't use
        "tkinter",
        "_tkinter",
        "wx",
        "gtk",
        "gi",
        # Dev / notebook tools
        "IPython",
        "jupyter",
        "notebook",
        "matplotlib",
        "scipy",
        "pandas",
        "pyautogui",
        "pyscreeze",
        "docutils",
        "sphinx",
        "Crypto",
        "OpenSSL",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# ---------------------------------------------------------------------------
# 5. Python archive
# ---------------------------------------------------------------------------

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# ---------------------------------------------------------------------------
# 6. EXE
# ---------------------------------------------------------------------------

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,   # binaries go into COLLECT below (one-folder mode)
    name="wshp",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,               # UPX disabled — can corrupt CUDA / ctranslate2 DLLs
    console=False,           # No console window — tray-only app
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/icon.ico",          # EXE icon (shows in Explorer & Alt-Tab)
    version="version_info.txt",      # Windows VERSIONINFO resource
)

# ---------------------------------------------------------------------------
# 7. COLLECT — assemble the final dist/wshp/ folder
# ---------------------------------------------------------------------------

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="wshp",             # → dist/wshp/
)
