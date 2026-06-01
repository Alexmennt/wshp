"""
audio/recorder.py
-----------------
Continuous microphone capture using sounddevice.

Design
------
* An InputStream runs permanently in the background (always-on approach).
* Callers can call start_recording() / stop_recording() to bracket a capture
  session.  All audio arriving between those two calls is accumulated in an
  internal ring-buffer and returned as a single float32 numpy array at 16 kHz.
* Each 30 ms chunk is also forwarded to an optional on_chunk callback so that
  a VAD processor can inspect every frame regardless of recording state.
* Everything that touches the buffer is protected by a threading.Lock so it is
  safe to call from both the sounddevice callback thread and the Qt main thread.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

import numpy as np
import sounddevice as sd

logger = logging.getLogger(__name__)

# Whisper requires 16 kHz mono audio.
SAMPLE_RATE: int = 16_000
CHANNELS: int = 1
DTYPE: str = "int16"

# Duration of each audio chunk forwarded to the VAD callback (seconds).
# webrtcvad accepts 10 ms, 20 ms, or 30 ms frames — we use 30 ms.
CHUNK_DURATION: float = 0.030
CHUNK_FRAMES: int = int(SAMPLE_RATE * CHUNK_DURATION)  # 480 samples


class AudioRecorder:
    """
    Wrapper around a sounddevice InputStream that:

    * Runs continuously so the VAD can inspect every chunk.
    * Accumulates chunks into a buffer only while is_recording is True.
    * Returns the accumulated audio as a normalised float32 array on stop.

    Parameters
    ----------
    on_chunk:
        Optional callable invoked with every 30 ms int16 numpy chunk,
        regardless of whether we are currently recording.  Signature::

            on_chunk(chunk: np.ndarray) -> None   # shape (480,), dtype int16
    """

    def __init__(self, on_chunk: Optional[Callable[[np.ndarray], None]] = None) -> None:
        self._on_chunk = on_chunk
        self._lock = threading.Lock()

        self._buffer: list[np.ndarray] = []
        self._is_recording: bool = False
        self._stream: Optional[sd.InputStream] = None
        self._device_index: Optional[int] = None

        # Residual samples carried across callback boundaries so we always
        # deliver exactly CHUNK_FRAMES samples to the VAD callback.
        self._residual: np.ndarray = np.empty(0, dtype=DTYPE)

    # ------------------------------------------------------------------ #
    #  Stream lifecycle                                                    #
    # ------------------------------------------------------------------ #

    def start_stream(self, device_index: Optional[int] = None) -> None:
        """Open the InputStream and start capturing.  Safe to call once."""
        if self._stream is not None:
            logger.warning(
                "AudioRecorder.start_stream() called while stream is already open."
            )
            return

        self._device_index = device_index

        try:
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype=DTYPE,
                blocksize=CHUNK_FRAMES,
                device=device_index,
                callback=self._sd_callback,
                latency="low",
            )
            self._stream.start()
            logger.info(
                "Audio stream started — device=%s, rate=%d Hz, chunk=%d ms",
                device_index if device_index is not None else "default",
                SAMPLE_RATE,
                int(CHUNK_DURATION * 1000),
            )
        except Exception as exc:
            self._stream = None
            logger.error("Failed to open audio stream: %s", exc)
            raise

    def stop_stream(self) -> None:
        """Stop and close the InputStream."""
        if self._stream is None:
            return
        try:
            self._stream.stop()
            self._stream.close()
        except Exception as exc:
            logger.warning("Error closing audio stream: %s", exc)
        finally:
            self._stream = None
            logger.info("Audio stream stopped.")

    @property
    def is_active(self) -> bool:
        """True if the underlying sounddevice stream is open and running."""
        return self._stream is not None and self._stream.active

    # ------------------------------------------------------------------ #
    #  Recording session                                                   #
    # ------------------------------------------------------------------ #

    def start_recording(self) -> None:
        """Begin accumulating audio into the internal buffer."""
        with self._lock:
            self._buffer = []
            self._is_recording = True
        logger.debug("Recording started.")

    def prepend_recording_audio(self, audio: np.ndarray) -> None:
        """
        Prepend already-captured int16 audio to the active recording buffer.

        Used by Auto-VAD so the few frames that triggered speech detection are
        included in the final clip instead of clipping the first phoneme.
        """
        if audio is None or len(audio) == 0:
            return

        audio = np.asarray(audio)
        if audio.dtype == np.float32:
            audio = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16)
        elif audio.dtype != np.int16:
            audio = audio.astype(np.int16)

        with self._lock:
            if self._is_recording:
                self._buffer.insert(0, audio.copy())

    def stop_recording(self) -> Optional[np.ndarray]:
        """
        Stop accumulating audio and return the captured data.

        Returns
        -------
        numpy.ndarray, shape (N,), dtype float32, or None if nothing was captured.
        Audio is normalised to the range [-1.0, 1.0] and resampled to 16 kHz
        (already guaranteed by the InputStream settings).
        """
        with self._lock:
            self._is_recording = False
            chunks = list(self._buffer)
            self._buffer = []

        if not chunks:
            logger.debug("stop_recording() called with empty buffer.")
            return None

        audio_int16 = np.concatenate(chunks)  # shape (N,), int16
        audio_f32 = audio_int16.astype(np.float32) / 32_768.0  # normalise
        duration = len(audio_f32) / SAMPLE_RATE
        logger.debug("Recording stopped — captured %.2f s of audio.", duration)
        return audio_f32

    def discard_recording(self) -> None:
        """Abort the current recording without returning audio."""
        with self._lock:
            self._is_recording = False
            self._buffer = []
        logger.debug("Recording discarded.")

    @property
    def is_recording(self) -> bool:
        return self._is_recording

    def recording_duration(self) -> float:
        """Return the number of seconds captured so far (approximate)."""
        with self._lock:
            total_samples = sum(len(c) for c in self._buffer)
        return total_samples / SAMPLE_RATE

    # ------------------------------------------------------------------ #
    #  sounddevice callback (runs on a private background thread)          #
    # ------------------------------------------------------------------ #

    def _sd_callback(
        self,
        indata: np.ndarray,
        frames: int,
        time_info,  # CData — unused
        status: sd.CallbackFlags,
    ) -> None:
        if status:
            logger.warning("sounddevice status: %s", status)

        # indata shape: (frames, channels) — flatten to 1-D
        chunk = indata[:, 0].copy()  # int16, shape (frames,)

        # ── Accumulate when recording ──────────────────────────────────
        with self._lock:
            if self._is_recording:
                self._buffer.append(chunk.copy())

        # ── Forward to VAD callback ────────────────────────────────────
        # We merge with any leftover samples from the previous call so the
        # VAD always receives exactly CHUNK_FRAMES samples.
        if self._on_chunk is not None:
            combined = np.concatenate([self._residual, chunk])
            n_full = len(combined) // CHUNK_FRAMES
            for i in range(n_full):
                vad_chunk = combined[i * CHUNK_FRAMES : (i + 1) * CHUNK_FRAMES]
                try:
                    self._on_chunk(vad_chunk)
                except Exception as exc:  # never crash the audio thread
                    logger.error("on_chunk callback raised: %s", exc)
            self._residual = combined[n_full * CHUNK_FRAMES :]

    # ------------------------------------------------------------------ #
    #  Device enumeration helpers                                          #
    # ------------------------------------------------------------------ #

    @staticmethod
    def list_input_devices() -> list[dict]:
        """
        Return a list of available input devices.

        Each entry is a dict::

            {
                "index": int,
                "name":  str,
                "channels": int,
                "default": bool,
            }
        """
        devices = []
        try:
            default_info = sd.query_devices(kind="input")
            default_name = default_info["name"] if default_info else ""
        except Exception:
            default_name = ""

        for idx, dev in enumerate(sd.query_devices()):
            if dev["max_input_channels"] > 0:
                devices.append(
                    {
                        "index": idx,
                        "name": dev["name"],
                        "channels": dev["max_input_channels"],
                        "default": dev["name"] == default_name,
                    }
                )
        return devices

    @staticmethod
    def default_device_index() -> Optional[int]:
        """Return the index of the system default input device, or None."""
        try:
            info = sd.query_devices(kind="input")
            return int(info["index"]) if info else None
        except Exception:
            return None
