"""
Voice Activity Detection (VAD) for wshp.

Primary:  webrtcvad  — Google's WebRTC VAD, very accurate, no extra ML deps.
Fallback: Energy-based VAD — pure-numpy, always available.

Both implementations share the same public interface so the rest of the
codebase never has to care which one is running.
"""

from __future__ import annotations

import logging
import struct
from collections import deque
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Try to import webrtcvad; gracefully fall back if unavailable
# ---------------------------------------------------------------------------
try:
    import webrtcvad as _webrtcvad  # type: ignore

    _WEBRTCVAD_AVAILABLE = True
    logger.info("webrtcvad is available — using WebRTC VAD.")
except ImportError:
    _WEBRTCVAD_AVAILABLE = False
    logger.warning(
        "webrtcvad is not installed.  Falling back to energy-based VAD.  "
        "Install it with:  pip install webrtcvad"
    )


# ---------------------------------------------------------------------------
# Public result dataclass
# ---------------------------------------------------------------------------
class VADResult:
    """Snapshot of the VAD state after processing one audio chunk."""

    __slots__ = ("is_speech", "speech_active", "speech_ended", "energy_db")

    def __init__(
        self,
        *,
        is_speech: bool,
        speech_active: bool,
        speech_ended: bool,
        energy_db: float = -99.0,
    ) -> None:
        self.is_speech = is_speech  # Was this frame classified as speech?
        self.speech_active = speech_active  # Are we inside a speech segment?
        self.speech_ended = speech_ended  # Did a speech segment just finish?
        self.energy_db = energy_db  # RMS level in dBFS (for diagnostics)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"VADResult(is_speech={self.is_speech}, "
            f"speech_active={self.speech_active}, "
            f"speech_ended={self.speech_ended}, "
            f"energy_db={self.energy_db:.1f})"
        )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _rms_db(chunk: np.ndarray) -> float:
    """Return the RMS level of a float32 or int16 chunk in dBFS."""
    if chunk.dtype != np.float32:
        samples = chunk.astype(np.float32) / 32768.0
    else:
        samples = chunk
    rms = float(np.sqrt(np.mean(samples**2) + 1e-12))
    return 20.0 * np.log10(rms)


# ---------------------------------------------------------------------------
# WebRTC VAD implementation
# ---------------------------------------------------------------------------
class _WebRTCVAD:
    """
    Thin wrapper around webrtcvad that tracks speech state across calls.

    webrtcvad requires frames of exactly 10, 20, or 30 ms sampled at
    8 000, 16 000, or 32 000 Hz.  We use 30 ms @ 16 kHz = 480 samples.
    """

    FRAME_MS = 30  # milliseconds per frame (10 / 20 / 30)
    SAMPLE_RATE = 16_000  # Hz

    # How many consecutive speech frames before we declare speech_active
    _SPEECH_TRIGGER = 2
    # Pre-roll: number of frames to prepend to the buffer once speech starts
    # (so we don't miss the very first phoneme)
    _PRE_ROLL_FRAMES = 5

    def __init__(self, aggressiveness: int, silence_duration: float) -> None:
        self._vad = _webrtcvad.Vad(aggressiveness)
        self._frame_size = int(self.SAMPLE_RATE * self.FRAME_MS / 1000)  # 480
        self._silence_duration = silence_duration
        self._frames_for_silence = max(1, int(silence_duration * 1000 / self.FRAME_MS))

        # State
        self._speech_frames: int = 0
        self._silence_frames: int = 0
        self._speech_active: bool = False
        # Sliding window of recent raw frames (for pre-roll)
        self._pre_roll: deque[np.ndarray] = deque(maxlen=self._PRE_ROLL_FRAMES)

    # ------------------------------------------------------------------
    def process(self, chunk: np.ndarray) -> VADResult:
        """
        Process a single 30 ms int16 chunk.

        The caller is responsible for passing chunks of the right length
        (self.frame_size samples).  Mismatched sizes are handled gracefully.
        """
        energy_db = _rms_db(chunk)

        # webrtcvad wants raw int16 bytes
        audio_bytes = chunk.astype(np.int16).tobytes()

        try:
            is_speech = self._vad.is_speech(audio_bytes, self.SAMPLE_RATE)
        except Exception:
            is_speech = False

        # Keep the pre-roll ring buffer up-to-date
        self._pre_roll.append(chunk.copy())

        speech_ended = False

        if is_speech:
            self._speech_frames += 1
            self._silence_frames = 0

            if not self._speech_active and self._speech_frames >= self._SPEECH_TRIGGER:
                self._speech_active = True
        else:
            if self._speech_active:
                self._silence_frames += 1
                self._speech_frames = max(0, self._speech_frames - 1)

                if self._silence_frames >= self._frames_for_silence:
                    speech_ended = True
                    self._reset_state()
            else:
                # Not yet active — decay the partial counter
                self._speech_frames = max(0, self._speech_frames - 1)

        return VADResult(
            is_speech=is_speech,
            speech_active=self._speech_active,
            speech_ended=speech_ended,
            energy_db=energy_db,
        )

    def _reset_state(self) -> None:
        self._speech_frames = 0
        self._silence_frames = 0
        self._speech_active = False

    def reset(self) -> None:
        """Explicitly reset all state (call this when discarding a recording)."""
        self._reset_state()
        self._pre_roll.clear()

    @property
    def frame_size(self) -> int:
        return self._frame_size

    def get_pre_roll(self) -> np.ndarray:
        """Return accumulated pre-roll frames as a single int16 array."""
        if not self._pre_roll:
            return np.zeros(0, dtype=np.int16)
        return np.concatenate(list(self._pre_roll)).astype(np.int16)


# ---------------------------------------------------------------------------
# Energy-based VAD fallback
# ---------------------------------------------------------------------------
class _EnergyVAD:
    """
    Simple energy-based VAD.

    Uses a smoothed RMS level and a dynamic noise floor to separate speech
    from silence — no external libraries needed.

    This is less accurate than webrtcvad (especially in noisy environments)
    but works well for desktop dictation where the user controls the mic.
    """

    FRAME_MS = 30
    SAMPLE_RATE = 16_000

    # Thresholds (in dBFS)
    _SPEECH_DB_ABOVE_FLOOR = 12.0  # Speech must be this many dB above noise floor
    _FLOOR_FLOOR_DB = -70.0  # Noise floor never goes below this
    _FLOOR_CEIL_DB = -20.0  # Noise floor never goes above this

    # State machine triggers
    _SPEECH_TRIGGER = 2  # Consecutive speech frames → active
    _PRE_ROLL_FRAMES = 5

    def __init__(self, aggressiveness: int, silence_duration: float) -> None:
        # Map aggressiveness (0–3) to a multiplier on the threshold
        self._threshold_extra_db = aggressiveness * 2.0  # 0, 2, 4, 6 dB

        self._frame_size = int(self.SAMPLE_RATE * self.FRAME_MS / 1000)
        self._silence_duration = silence_duration
        self._frames_for_silence = max(1, int(silence_duration * 1000 / self.FRAME_MS))

        # Adaptive noise floor (slow moving average)
        self._noise_floor_db: float = -50.0

        # State
        self._speech_frames: int = 0
        self._silence_frames: int = 0
        self._speech_active: bool = False
        self._pre_roll: deque[np.ndarray] = deque(maxlen=self._PRE_ROLL_FRAMES)

    # ------------------------------------------------------------------
    def process(self, chunk: np.ndarray) -> VADResult:
        energy_db = _rms_db(chunk)
        self._pre_roll.append(chunk.copy())

        # Update noise floor very slowly (only when not in speech)
        if not self._speech_active:
            alpha = 0.02
            self._noise_floor_db = (
                alpha * energy_db + (1.0 - alpha) * self._noise_floor_db
            )
            self._noise_floor_db = float(
                np.clip(self._noise_floor_db, self._FLOOR_FLOOR_DB, self._FLOOR_CEIL_DB)
            )

        threshold = (
            self._noise_floor_db
            + self._SPEECH_DB_ABOVE_FLOOR
            + self._threshold_extra_db
        )
        is_speech = energy_db > threshold

        speech_ended = False

        if is_speech:
            self._speech_frames += 1
            self._silence_frames = 0

            if not self._speech_active and self._speech_frames >= self._SPEECH_TRIGGER:
                self._speech_active = True
        else:
            if self._speech_active:
                self._silence_frames += 1
                self._speech_frames = max(0, self._speech_frames - 1)

                if self._silence_frames >= self._frames_for_silence:
                    speech_ended = True
                    self._reset_state()
            else:
                self._speech_frames = max(0, self._speech_frames - 1)

        return VADResult(
            is_speech=is_speech,
            speech_active=self._speech_active,
            speech_ended=speech_ended,
            energy_db=energy_db,
        )

    def _reset_state(self) -> None:
        self._speech_frames = 0
        self._silence_frames = 0
        self._speech_active = False

    def reset(self) -> None:
        self._reset_state()
        self._pre_roll.clear()

    @property
    def frame_size(self) -> int:
        return self._frame_size

    def get_pre_roll(self) -> np.ndarray:
        if not self._pre_roll:
            return np.zeros(0, dtype=np.int16)
        return np.concatenate(list(self._pre_roll)).astype(np.int16)


# ---------------------------------------------------------------------------
# Public factory / façade
# ---------------------------------------------------------------------------
class VADProcessor:
    """
    Public VAD façade used by the rest of the application.

    Automatically selects the best available backend.

    Usage
    -----
    vad = VADProcessor(aggressiveness=3, silence_duration=0.8)
    result = vad.process(int16_numpy_chunk)   # 30 ms @ 16 kHz
    if result.speech_ended:
        ...  # pass audio buffer to transcriber
    """

    def __init__(
        self,
        aggressiveness: int = 3,
        silence_duration: float = 0.8,
        force_energy: bool = False,
    ) -> None:
        aggressiveness = int(np.clip(aggressiveness, 0, 3))

        if _WEBRTCVAD_AVAILABLE and not force_energy:
            self._backend: _WebRTCVAD | _EnergyVAD = _WebRTCVAD(
                aggressiveness, silence_duration
            )
            self.backend_name = "webrtcvad"
        else:
            self._backend = _EnergyVAD(aggressiveness, silence_duration)
            self.backend_name = "energy"

        logger.info("VADProcessor using backend: %s", self.backend_name)

    # ------------------------------------------------------------------
    def process(self, chunk: np.ndarray) -> VADResult:
        """
        Process one audio chunk.

        Parameters
        ----------
        chunk : np.ndarray
            int16 or float32 array of exactly `frame_size` samples
            recorded at 16 000 Hz, mono.

        Returns
        -------
        VADResult
        """
        # Ensure int16 for consistent processing
        if chunk.dtype == np.float32:
            chunk = (chunk * 32768.0).astype(np.int16)
        elif chunk.dtype != np.int16:
            chunk = chunk.astype(np.int16)

        # Pad/trim to exact frame size if needed
        fs = self._backend.frame_size
        if len(chunk) < fs:
            chunk = np.pad(chunk, (0, fs - len(chunk)))
        elif len(chunk) > fs:
            chunk = chunk[:fs]

        return self._backend.process(chunk)

    def reset(self) -> None:
        """Reset all internal state (call after discarding or completing a recording)."""
        self._backend.reset()

    def get_pre_roll(self) -> np.ndarray:
        """
        Return the most recent pre-roll frames as a contiguous int16 array.

        Pre-roll captures audio that was recorded just before the VAD
        triggered, ensuring the first phoneme is never clipped.
        """
        return self._backend.get_pre_roll()

    @property
    def frame_size(self) -> int:
        """Number of int16 samples expected per process() call (480 @ 16 kHz)."""
        return self._backend.frame_size

    @property
    def frame_ms(self) -> int:
        """Frame duration in milliseconds."""
        return self._backend.FRAME_MS

    @property
    def sample_rate(self) -> int:
        """Expected sample rate in Hz."""
        return self._backend.SAMPLE_RATE
