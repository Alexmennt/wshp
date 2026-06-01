"""
Transcription engine using faster-whisper, running in a background QThread.

Lifecycle
---------
1. Instantiate TranscriptionEngine and call .start() – the thread wakes up and
   loads the Whisper model (may take 5–20 s on first run while downloading).
2. Emit the audio_ready signal (or call .submit()) with a float32 numpy array
   sampled at 16 kHz.
3. The worker transcribes the audio and emits transcription_done(text).
4. Call .stop() to request a clean shutdown before the process exits.

Signals emitted (all are safe to connect to Qt slots in any thread):
  model_loading   – model has started loading
  model_ready     – model loaded successfully; str = device actually used
  model_error     – model failed to load; str = error message
  transcription_started – transcription is underway
  transcription_done    – str = final transcribed text (stripped)
  transcription_error   – str = error message
  progress        – int 0-100 (currently just 0 / 100 boundary events)
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Optional

import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal

logger = logging.getLogger(__name__)

# Sentinel object – place in the audio queue to ask the worker to shut down.
_STOP_SENTINEL = object()

# Approximate total download sizes in MB for each supported model.
# Used to show "X MB / Y MB" progress.  Values are best-effort estimates.
_KNOWN_MODEL_SIZES_MB: dict[str, int] = {
    "tiny": 75,
    "tiny.en": 75,
    "base": 145,
    "base.en": 145,
    "small": 245,
    "small.en": 245,
    "medium": 790,
    "medium.en": 790,
    "large-v1": 1550,
    "large-v2": 1550,
    "large-v3": 1550,
    "large-v3-turbo": 1550,  # mobiuslabsgmbh variant; actual blob ~1,542 MB
    "distil-large-v3": 756,
}


class _DownloadWatcher(threading.Thread):
    """
    Daemon thread that polls the HuggingFace cache directory once per second
    and calls *callback(downloaded_mb, total_mb)* with updated byte counts.

    total_mb is -1 when the model size is not known in advance.

    If no bytes appear within ``_NO_ACTIVITY_TIMEOUT_S`` seconds the watcher
    assumes the model is being loaded from a cache location it cannot see
    (e.g. the HF default cache) and stops silently without calling the
    callback again.  This prevents the UI from showing a stuck "0 / N MB"
    progress bar when no actual download is taking place.
    """

    _NO_ACTIVITY_TIMEOUT_S = 10  # seconds before giving up on 0-byte progress

    def __init__(
        self,
        cache_dir: str,
        model_id: str,
        total_mb: int,
        callback,
        no_download_callback=None,
    ) -> None:
        super().__init__(daemon=True, name="DownloadWatcher")
        self._cache_dir = cache_dir
        self._model_id = model_id
        self._total_mb = total_mb
        self._callback = callback
        # Called (no args) when watcher decides nothing is downloading
        self._no_download_callback = no_download_callback
        self._stop_event = threading.Event()

        # The HuggingFace cache layout:
        #   <cache_dir>/models--Systran--faster-whisper-large-v3/blobs/…
        sanitized = model_id.replace("/", "--")
        self._watch_dir = os.path.join(cache_dir, f"models--{sanitized}")

    def run(self) -> None:
        start = time.monotonic()
        ever_nonzero = False

        while not self._stop_event.wait(timeout=1.0):
            downloaded = self._dir_size_mb(self._watch_dir)

            if downloaded > 0:
                ever_nonzero = True

            # If we have seen zero bytes for longer than the timeout the
            # model is not downloading here — bail out silently.
            if (
                not ever_nonzero
                and (time.monotonic() - start) > self._NO_ACTIVITY_TIMEOUT_S
            ):
                logger.info(
                    "DownloadWatcher: no activity after %ds — "
                    "model is likely cached in another location.",
                    self._NO_ACTIVITY_TIMEOUT_S,
                )
                if self._no_download_callback is not None:
                    try:
                        self._no_download_callback()
                    except Exception:
                        pass
                return

            try:
                self._callback(downloaded, self._total_mb)
            except Exception:
                pass  # never crash the watcher

    def stop(self) -> None:
        self._stop_event.set()

    @staticmethod
    def _dir_size_mb(path: str) -> int:
        total = 0
        try:
            for root, _dirs, files in os.walk(path):
                for fname in files:
                    try:
                        total += os.path.getsize(os.path.join(root, fname))
                    except OSError:
                        pass
        except OSError:
            pass
        return total // (1024 * 1024)


class TranscriptionEngine(QThread):
    """Background thread that owns the Whisper model and transcribes audio."""

    # ── Signals ───────────────────────────────────────────────────────────────
    model_loading = pyqtSignal()
    model_ready = pyqtSignal(str)  # device string, e.g. "cuda:0"
    model_error = pyqtSignal(str)
    model_downloading = pyqtSignal(str)  # model name, emitted before download starts
    download_progress = pyqtSignal(
        int, int
    )  # (downloaded_mb, total_mb); total=-1 if unknown

    transcription_started = pyqtSignal()
    transcription_done = pyqtSignal(str)
    transcription_error = pyqtSignal(str)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self._config = config
        self._audio_queue: queue.Queue = queue.Queue()
        self._model = None
        self._running = True

    # ── Public API ────────────────────────────────────────────────────────────

    def submit(self, audio: np.ndarray) -> None:
        """
        Queue a float32 numpy array (16 kHz, mono) for transcription.
        Returns immediately; result arrives via transcription_done signal.
        """
        self._audio_queue.put(audio)

    def stop(self) -> None:
        """Request the thread to exit cleanly."""
        self._running = False
        self._audio_queue.put(_STOP_SENTINEL)

    @property
    def is_model_loaded(self) -> bool:
        return self._model is not None

    # ── QThread entry point ───────────────────────────────────────────────────

    def run(self) -> None:  # noqa: C901  (complex but intentionally self-contained)
        # ── 1. Load model ─────────────────────────────────────────────────────
        self.model_loading.emit()
        try:
            self._model = self._load_model()
        except Exception as exc:
            logger.exception("Failed to load Whisper model")
            self.model_error.emit(str(exc))
            return

        device_str = self._resolve_device()
        self.model_ready.emit(device_str)
        logger.info(
            "Whisper model '%s' ready on %s", self._config.model_size, device_str
        )

        # ── 2. Process audio queue ─────────────────────────────────────────────
        while self._running:
            try:
                item = self._audio_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if item is _STOP_SENTINEL:
                break

            audio: np.ndarray = item
            self._transcribe(audio)

        logger.info("TranscriptionEngine thread exiting")

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _resolve_device(self) -> str:
        """Return the device string actually used by the loaded model."""
        try:
            # faster-whisper exposes the underlying CTranslate2 model
            ct2_model = self._model.model
            device = ct2_model.device
            device_index = ct2_model.device_index
            if isinstance(device_index, (list, tuple)):
                device_index = device_index[0]
            return f"{device}:{device_index}"
        except Exception:
            return self._config.device

    def _load_model(self):
        """
        Import faster-whisper and construct the WhisperModel.

        Device selection logic
        ----------------------
        config.device == "auto"  → try CUDA, fall back to CPU
        config.device == "cuda"  → force CUDA (raises if unavailable)
        config.device == "cpu"   → force CPU

        compute_type is sanitised automatically: float16 is only valid on GPU.

        If the model is not yet cached a download-progress watcher thread is
        started so the UI can show live MB progress via download_progress signal.
        """
        from faster_whisper import WhisperModel  # type: ignore

        device = self._config.device
        compute_type = self._config.compute_type
        model_size = self._config.model_size
        cache_dir = self._model_cache_dir()

        if device == "auto":
            device = self._detect_best_device()

        # float16 is not supported on CPU – downgrade gracefully
        if device == "cpu" and compute_type in ("float16", "int8_float16"):
            logger.warning(
                "compute_type '%s' is not supported on CPU; switching to 'int8'.",
                compute_type,
            )
            compute_type = "int8"

        needs_download = not self._is_model_cached(model_size, cache_dir)

        if needs_download:
            logger.info(
                "Model '%s' not in cache — downloading from HuggingFace…", model_size
            )
            self.model_downloading.emit(model_size)

            # Start a background thread that polls the cache dir every second
            # and emits download_progress(downloaded_mb, total_mb).
            total_mb = _KNOWN_MODEL_SIZES_MB.get(model_size, -1)
            model_id = self._resolve_model_id(model_size)
            watcher = _DownloadWatcher(
                cache_dir=cache_dir,
                model_id=model_id,
                total_mb=total_mb,
                callback=lambda done, total: self.download_progress.emit(done, total),
                # If the watcher times out with 0 bytes the model was already
                # cached somewhere else — suppress any further download UI.
                no_download_callback=lambda: self.model_loading.emit(),
            )
            watcher.start()
        else:
            watcher = None

        logger.info(
            "Loading model '%s' on device='%s' compute_type='%s' …",
            model_size,
            device,
            compute_type,
        )
        t0 = time.perf_counter()

        # Load model with timeout protection to prevent indefinite hangs
        # (common with large-v3 on systems with network issues or VRAM pressure)
        _MODEL_LOAD_TIMEOUT_S = 300  # 5 minutes max for large model download+load

        def _load():
            return WhisperModel(
                model_size,
                device=device,
                compute_type=compute_type,
                download_root=cache_dir,
            )

        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(_load)
                try:
                    model = future.result(timeout=_MODEL_LOAD_TIMEOUT_S)
                except FutureTimeoutError as exc:
                    logger.error(
                        "Model loading timed out after %ds. "
                        "This can happen if the download is stuck or "
                        "the system has insufficient memory/VRAM for '%s'.",
                        _MODEL_LOAD_TIMEOUT_S,
                        model_size,
                    )
                    raise RuntimeError(
                        f"Model loading timed out after {_MODEL_LOAD_TIMEOUT_S}s. "
                        f"Try a smaller model (e.g., 'large-v3-turbo' or 'small') "
                        f"or check your network connection."
                    ) from exc
        finally:
            if watcher is not None:
                watcher.stop()

        elapsed = time.perf_counter() - t0
        logger.info("Model loaded in %.1f s", elapsed)
        return model

    @staticmethod
    def _resolve_model_id(model_size: str) -> str:
        """Return the HuggingFace repo id faster-whisper will use for a model."""
        try:
            from faster_whisper.utils import _MODELS  # type: ignore

            return _MODELS.get(model_size, model_size)
        except Exception:
            aliases = {
                "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
                "distil-large-v3": "distil-whisper/distil-large-v3",
            }
            return aliases.get(model_size, f"Systran/faster-whisper-{model_size}")

    @staticmethod
    def _is_model_cached(model_size: str, cache_dir: str) -> bool:
        """
        Return True if the model weights are already on disk so no download
        is required.

        Strategy
        --------
        1. Use faster-whisper's own ``download_model(..., local_files_only=True)``.
           This correctly resolves model-ID aliases — e.g. "large-v3-turbo" maps
           to "mobiuslabsgmbh/faster-whisper-large-v3-turbo", not to the Systran
           repo we previously hard-coded.  We probe our custom cache first, then
           the HF default cache.
        2. Fall back to scanning every ``blobs/`` sub-directory inside cache_dir
           for any file larger than 100 MB (handles edge cases where the
           faster-whisper API is unavailable or raises unexpectedly).
        """
        # ── Primary: ask faster-whisper itself ────────────────────────────
        try:
            from faster_whisper.utils import download_model  # type: ignore

            # Probe our custom cache dir first
            for kwargs in [{"cache_dir": cache_dir}, {}]:
                try:
                    download_model(model_size, local_files_only=True, **kwargs)
                    logger.debug(
                        "Model '%s' is cached (probe kwargs=%s).", model_size, kwargs
                    )
                    return True
                except Exception:
                    pass

        except ImportError:
            logger.debug(
                "faster_whisper.utils.download_model not importable; "
                "falling back to blob scan."
            )

        # ── Fallback: scan blobs/ for any large file ──────────────────────
        # Collect all cache roots to check
        cache_roots: list[str] = [cache_dir]
        try:
            from huggingface_hub.constants import HF_HUB_CACHE  # type: ignore

            if HF_HUB_CACHE and HF_HUB_CACHE not in cache_roots:
                cache_roots.append(HF_HUB_CACHE)
        except Exception:
            fallback = os.path.join(
                os.path.expanduser("~"), ".cache", "huggingface", "hub"
            )
            if fallback not in cache_roots:
                cache_roots.append(fallback)

        for root in cache_roots:
            if not os.path.isdir(root):
                continue
            try:
                for entry in os.listdir(root):
                    # Match any org: models--<org>--faster-whisper-<size>
                    if not entry.startswith("models--"):
                        continue
                    if not entry.endswith(f"faster-whisper-{model_size}"):
                        continue
                    blobs_dir = os.path.join(root, entry, "blobs")
                    if not os.path.isdir(blobs_dir):
                        continue
                    for fname in os.listdir(blobs_dir):
                        fpath = os.path.join(blobs_dir, fname)
                        try:
                            if (
                                os.path.isfile(fpath)
                                and os.path.getsize(fpath) > 100 * 1024 * 1024
                            ):
                                logger.debug(
                                    "Model '%s' blob found via scan: %s",
                                    model_size,
                                    fpath,
                                )
                                return True
                        except OSError:
                            pass
            except OSError:
                pass

        return False

    @staticmethod
    def _detect_best_device() -> str:
        """Return 'cuda' if a CUDA device is available, else 'cpu'."""
        try:
            import ctranslate2  # type: ignore

            if ctranslate2.get_cuda_device_count() > 0:
                return "cuda"
        except Exception:
            pass
        logger.info("No CUDA device found; falling back to CPU.")
        return "cpu"

    @staticmethod
    def _model_cache_dir() -> str:
        import os

        cache = os.path.join(os.path.expanduser("~"), ".wshp", "models")
        os.makedirs(cache, exist_ok=True)
        return cache

    def _transcribe(self, audio: np.ndarray) -> None:  # noqa: C901
        """Run faster-whisper on the supplied audio and emit the result."""
        if self._model is None:
            self.transcription_error.emit("Model not loaded.")
            return

        self.transcription_started.emit()

        # Ensure float32, mono, 16 kHz normalised to [-1, 1]
        audio = self._prepare_audio(audio)

        if audio is None or len(audio) == 0:
            self.transcription_error.emit("Empty audio buffer received.")
            return

        try:
            cfg = self._config
            lang: Optional[str] = cfg.language if cfg.language else None
            prompt: Optional[str] = cfg.initial_prompt if cfg.initial_prompt else None

            t0 = time.perf_counter()

            segments_gen, info = self._model.transcribe(
                audio,
                language=lang,
                beam_size=cfg.beam_size,
                initial_prompt=prompt,
                # VAD is handled upstream by webrtcvad / energy VAD in audio/vad.py.
                # Disabling faster-whisper's built-in vad_filter removes the
                # onnxruntime dependency (~400 MB) from the PyInstaller bundle.
                vad_filter=False,
                # Word timestamps are cheap and allow future highlight features
                word_timestamps=False,
                # Condition on previous text reduces hallucinations in short clips
                condition_on_previous_text=False,
                # Suppress common hallucination tokens
                suppress_blank=True,
                no_speech_threshold=0.6,
                log_prob_threshold=-1.0,
            )

            # Materialise the lazy generator
            parts: list[str] = []
            for segment in segments_gen:
                text = segment.text.strip()
                if text:
                    parts.append(text)

            elapsed = time.perf_counter() - t0
            detected_lang = getattr(info, "language", "?")
            logger.info(
                "Transcribed %.2f s of audio in %.2f s (lang=%s)",
                len(audio) / 16000,
                elapsed,
                detected_lang,
            )

            result = " ".join(parts).strip()

            # Emit even if empty – the controller decides what to do
            self.transcription_done.emit(result)

        except Exception as exc:
            logger.exception("Transcription failed")
            self.transcription_error.emit(str(exc))

    @staticmethod
    def _prepare_audio(audio: np.ndarray) -> Optional[np.ndarray]:
        """
        Ensure audio is a 1-D float32 array in the range [-1, 1].
        Handles int16 input (from sounddevice with dtype='int16') and
        float32 input (already normalised).
        """
        if audio is None:
            return None

        audio = np.asarray(audio)

        # Flatten to 1-D (handles mono/stereo frames from sounddevice)
        if audio.ndim > 1:
            audio = audio.mean(axis=1)

        if audio.dtype == np.int16:
            audio = audio.astype(np.float32) / 32768.0
        elif audio.dtype != np.float32:
            audio = audio.astype(np.float32)

        # Safety clamp – protects against clipping artefacts
        audio = np.clip(audio, -1.0, 1.0)
        return audio
