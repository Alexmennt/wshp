"""
app_controller.py
─────────────────
Central coordinator for wshp.

The AppController owns all application components and wires them together.
It runs entirely on the Qt main thread; background work happens in:
  • HotkeyManager  – daemon thread (pynput listener)
  • AudioRecorder  – sounddevice callback thread
  • TranscriptionEngine – QThread

Cross-thread communication uses a thread-safe Queue polled by a QTimer,
and Qt signals for QThread → main-thread callbacks.

State machine
─────────────
  LOADING  ──(model ready)──► IDLE
  IDLE     ──(ptt/toggle/vad)──► RECORDING
  RECORDING──(release/silence)──► TRANSCRIBING
  RECORDING──(cancel)──────────► IDLE
  TRANSCRIBING──(result)───────► IDLE
  TRANSCRIBING──(error)────────► IDLE

All states are strings for easy serialisation / logging.
"""

from __future__ import annotations

import logging
import queue
import time
from typing import Optional

import numpy as np
from PyQt6.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot

from audio.recorder import AudioRecorder
from audio.vad import VADProcessor
from config import Config, save_config
from input.hotkeys import HotkeyManager
from input.injector import TextInjector
from transcription.engine import TranscriptionEngine

logger = logging.getLogger(__name__)

# ── Application states ────────────────────────────────────────────────────────
STATE_LOADING = "loading"
STATE_IDLE = "idle"
STATE_RECORDING = "recording"
STATE_TRANSCRIBING = "transcribing"
STATE_ERROR = "error"
STATE_PAUSED = "paused"


class AppController(QObject):
    """
    Central coordinator that owns and connects all subsystems.

    Signals
    -------
    state_changed(str)
        New application state (use the STATE_* constants above).
    transcription_ready(str)
        A completed transcription text, after injection.
    model_info(str)
        Human-readable string about the loaded model / device.
    error_occurred(str)
        A user-visible error description.
    recording_tick(float)
        Emitted roughly every 100 ms while recording; value is elapsed seconds.
    """

    # ── Signals ───────────────────────────────────────────────────────────────
    state_changed = pyqtSignal(str)
    transcription_ready = pyqtSignal(str)
    model_info = pyqtSignal(str)
    model_downloading = pyqtSignal(
        str, int
    )  # (model_name, total_mb); total_mb=-1 if unknown
    download_progress = pyqtSignal(int, int)  # (downloaded_mb, total_mb)
    error_occurred = pyqtSignal(str)
    recording_tick = pyqtSignal(float)

    def __init__(self, config: Config, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._config = config
        self._state: str = STATE_LOADING
        self._paused: bool = False

        # ── Event queue (hotkey thread → main thread) ─────────────────────────
        self._event_queue: queue.Queue = queue.Queue(maxsize=50)

        # ── Subsystems ────────────────────────────────────────────────────────
        self._vad: VADProcessor = self._build_vad()
        self._recorder: AudioRecorder = AudioRecorder(on_chunk=self._on_audio_chunk)
        self._engine: TranscriptionEngine = TranscriptionEngine(config, parent=self)
        self._injector: TextInjector = TextInjector(config)
        self._hotkeys: HotkeyManager = HotkeyManager(
            event_queue=self._event_queue,
            hotkey_ptt=config.hotkey_ptt,
            hotkey_toggle=config.hotkey_toggle,
            hotkey_cancel=config.hotkey_cancel,
        )

        # ── VAD mode state ────────────────────────────────────────────────────
        # In VAD mode, recording starts automatically when speech is detected.
        # We track whether VAD recording has been auto-started.
        self._vad_recording_active: bool = False

        # ── Recording timer ───────────────────────────────────────────────────
        self._recording_start_time: float = 0.0
        self._recording_timer = QTimer(self)
        self._recording_timer.setInterval(100)  # 10 Hz
        self._recording_timer.timeout.connect(self._on_recording_tick)

        # ── Event poll timer (reads hotkey queue on main thread) ───────────────
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(10)  # 100 Hz polling
        self._poll_timer.timeout.connect(self._poll_events)
        self._poll_timer.start()

        # ── Connect TranscriptionEngine signals ───────────────────────────────
        self._engine.model_loading.connect(self._on_model_loading)
        self._engine.model_ready.connect(self._on_model_ready)
        self._engine.model_error.connect(self._on_model_error)
        self._engine.model_downloading.connect(self._on_model_downloading)
        self._engine.download_progress.connect(self._on_download_progress)
        self._engine.transcription_started.connect(self._on_transcription_started)
        self._engine.transcription_done.connect(self._on_transcription_done)
        self._engine.transcription_error.connect(self._on_transcription_error)

    # ── Startup / shutdown ────────────────────────────────────────────────────

    def start(self) -> None:
        """
        Start all background systems.  Call once after construction.
        The UI should display a loading indicator until model_info is emitted.
        """
        logger.info("AppController starting …")

        # Start hotkey listener daemon thread
        self._hotkeys.start()

        # Open the audio stream (always-on; recording starts on demand)
        try:
            self._recorder.start_stream(device_index=self._config.microphone_index)
        except Exception as exc:
            logger.error("Failed to open audio stream: %s", exc)
            self.error_occurred.emit(f"Microphone error: {exc}")

        # Start the transcription engine thread (it will load the model)
        self._engine.start()

        logger.info("AppController started.")

    def stop(self) -> None:
        """
        Gracefully shut down all subsystems.  Call before QApplication.quit().
        """
        logger.info("AppController stopping …")

        self._poll_timer.stop()
        self._recording_timer.stop()

        # Stop audio first so no more chunks arrive
        self._recorder.stop_stream()

        # Stop hotkey listener
        self._hotkeys.stop()

        # Stop the transcription engine
        if self._engine.isRunning():
            self._engine.stop()
            self._engine.wait(3000)  # up to 3 s

        logger.info("AppController stopped.")

    # ── Config reload ─────────────────────────────────────────────────────────

    def apply_config(self, new_config: Config) -> None:
        """
        Apply a new Config object (e.g. after the user saves the settings dialog).

        Restarts components that have already been initialised if their
        settings changed.
        """
        old_config = self._config
        self._config = new_config
        save_config(new_config)

        # Update hotkeys
        self._hotkeys.update_keys(
            hotkey_ptt=new_config.hotkey_ptt,
            hotkey_toggle=new_config.hotkey_toggle,
            hotkey_cancel=new_config.hotkey_cancel,
        )

        # Rebuild VAD if parameters changed
        if (
            new_config.vad_aggressiveness != old_config.vad_aggressiveness
            or new_config.vad_silence_duration != old_config.vad_silence_duration
        ):
            self._vad = self._build_vad()
            logger.info("VAD processor rebuilt with new parameters.")

        # If microphone changed, restart the audio stream
        if new_config.microphone_index != old_config.microphone_index:
            self._recorder.stop_stream()
            try:
                self._recorder.start_stream(device_index=new_config.microphone_index)
            except Exception as exc:
                logger.error("Failed to reopen audio stream: %s", exc)
                self.error_occurred.emit(f"Microphone error: {exc}")

        # If model/device/compute_type changed, reload the model
        model_changed = (
            new_config.model_size != old_config.model_size
            or new_config.device != old_config.device
            or new_config.compute_type != old_config.compute_type
        )
        if model_changed:
            logger.info("Model settings changed — reloading engine.")
            self._engine.stop()
            self._engine.wait(5000)
            self._engine = TranscriptionEngine(new_config, parent=self)
            self._engine.model_loading.connect(self._on_model_loading)
            self._engine.model_ready.connect(self._on_model_ready)
            self._engine.model_error.connect(self._on_model_error)
            self._engine.model_downloading.connect(self._on_model_downloading)
            self._engine.download_progress.connect(self._on_download_progress)
            self._engine.transcription_started.connect(self._on_transcription_started)
            self._engine.transcription_done.connect(self._on_transcription_done)
            self._engine.transcription_error.connect(self._on_transcription_error)
            self._engine.start()
            self._set_state(STATE_LOADING)

        # Update the injector config reference
        self._injector = TextInjector(new_config)

        logger.info("Config applied.")

    # ── Pause / resume ────────────────────────────────────────────────────────

    def pause(self) -> None:
        """Pause all hotkey processing (e.g. while settings dialog is open)."""
        if self._paused:
            return
        self._paused = True
        self._hotkeys.pause()
        # Cancel any in-progress recording
        if self._state == STATE_RECORDING:
            self._cancel_recording()
        self._set_state(STATE_PAUSED)
        logger.info("Hotkeys paused.")

    def resume(self) -> None:
        """Resume hotkey processing."""
        if not self._paused:
            return
        self._paused = False
        self._hotkeys.resume()
        self._set_state(STATE_IDLE)
        logger.info("Hotkeys resumed.")

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def current_state(self) -> str:
        return self._state

    @property
    def config(self) -> Config:
        return self._config

    # ── Event queue polling ───────────────────────────────────────────────────

    @pyqtSlot()
    def _poll_events(self) -> None:
        """Drain the hotkey event queue on every QTimer tick (main thread)."""
        try:
            while True:
                event = self._event_queue.get_nowait()
                self._handle_hotkey_event(event)
        except queue.Empty:
            pass

    # ── Hotkey event handlers ─────────────────────────────────────────────────

    def _on_ptt_press(self) -> None:
        mode = self._config.mode
        if mode != "ptt":
            return  # PTT key only acts in PTT mode
        if self._state != STATE_IDLE:
            return  # Ignore if already recording or transcribing
        self._start_recording()

    def _on_ptt_release(self) -> None:
        mode = self._config.mode
        if mode != "ptt":
            return
        if self._state != STATE_RECORDING:
            return
        self._stop_and_transcribe()

    def _on_toggle(self) -> None:
        """
        Handle the toggle hotkey.

        Behaviour by mode:
          ptt     – toggle key starts a one-shot recording (press = start, release PTT = stop)
          toggle  – press to start recording, press again to transcribe
          vad     – press to manually start a recording (overrides auto-VAD); press again to transcribe
        """
        if self._state == STATE_IDLE:
            self._start_recording()
        elif self._state == STATE_RECORDING:
            self._stop_and_transcribe()
        # Ignore presses in LOADING / TRANSCRIBING / ERROR / PAUSED states

    def _on_cancel(self) -> None:
        if self._state == STATE_RECORDING:
            self._cancel_recording()
            logger.info("Recording cancelled by user.")

    # ── Recording lifecycle ───────────────────────────────────────────────────

    def _start_recording(self) -> None:
        """Begin an audio recording session."""
        # Snapshot the target window before we do anything else
        self._injector.capture_target()

        self._recorder.start_recording()
        self._vad.reset()
        self._vad_recording_active = True

        self._recording_start_time = time.monotonic()
        self._recording_timer.start()
        self._set_state(STATE_RECORDING)

        logger.info("Recording started (mode=%s).", self._config.mode)

    def _stop_and_transcribe(self) -> None:
        """Stop recording and submit audio to the transcription engine."""
        self._recording_timer.stop()

        audio = self._recorder.stop_recording()
        self._vad.reset()
        self._vad_recording_active = False

        duration = len(audio) / 16_000 if audio is not None else 0.0

        if audio is None or duration < self._config.min_speech_duration:
            logger.info(
                "Recording too short (%.2f s < %.2f s) — discarding.",
                duration,
                self._config.min_speech_duration,
            )
            self._set_state(STATE_IDLE)
            return

        logger.info("Submitting %.2f s of audio for transcription.", duration)
        self._set_state(STATE_TRANSCRIBING)
        self._engine.submit(audio)

    def _cancel_recording(self) -> None:
        """Abort the current recording without transcribing."""
        self._recording_timer.stop()
        self._recorder.discard_recording()
        self._vad.reset()
        self._vad_recording_active = False
        self._injector.clear_target()
        self._set_state(STATE_IDLE)

    # ── Audio chunk handler (called from sounddevice thread) ─────────────────
    # NOTE: This runs on the sounddevice background thread.
    # Only call thread-safe operations here — NO Qt object access.

    def _on_audio_chunk(self, chunk: np.ndarray) -> None:
        """
        Process each 30 ms audio chunk through the VAD.

        This method runs on the sounddevice background thread — it must only
        touch thread-safe primitives (Queue, atomic reads of simple flags).

        VAD mode behaviour
        ──────────────────
        • While IDLE:   if speech is detected → enqueue _vad_speech_start so
                        the main thread starts recording.
        • While RECORDING (VAD-initiated, _vad_recording_active == True):
                        if silence follows speech → enqueue _vad_speech_end so
                        the main thread stops and transcribes.
        • While RECORDING (manually started, _vad_recording_active == False):
                        VAD does not auto-stop; the user must press the key again.

        PTT / toggle mode:  nothing is done here; PTT key events drive everything.
        """
        if self._config.mode != "vad":
            return  # VAD auto-detection only applies in VAD mode

        if self._state not in (STATE_RECORDING, STATE_IDLE):
            return  # Leave TRANSCRIBING / LOADING / ERROR states alone

        result = self._vad.process(chunk)

        if self._state == STATE_IDLE:
            # Waiting for speech to start — trigger on the first confirmed speech frame.
            if result.speech_active:
                try:
                    self._event_queue.put_nowait({"type": "_vad_speech_start"})
                except queue.Full:
                    pass  # Drop if the queue is somehow full; next chunk will retry

        elif self._state == STATE_RECORDING and self._vad_recording_active:
            # Auto-recording: stop when VAD detects sustained silence after speech.
            if result.speech_ended:
                try:
                    self._event_queue.put_nowait({"type": "_vad_speech_end"})
                except queue.Full:
                    pass

    def _handle_hotkey_event(self, event: dict) -> None:
        """Dispatch a single event (hotkey or internal VAD) to the handler."""
        if self._paused:
            return
        if self._state == STATE_LOADING:
            logger.debug("Ignoring event '%s' — model still loading.", event)
            return

        etype = event.get("type", "")

        if etype == "ptt_press":
            self._on_ptt_press()
        elif etype == "ptt_release":
            self._on_ptt_release()
        elif etype == "toggle":
            self._on_toggle()
        elif etype == "cancel":
            self._on_cancel()
        elif etype == "_vad_speech_start":
            self._on_vad_speech_start()
        elif etype == "_vad_speech_end":
            self._on_vad_speech_end()
        else:
            logger.debug("Unknown event type: %r", etype)

    def _on_vad_speech_start(self) -> None:
        """
        VAD detected speech start — called on the main thread via the event queue.

        Guards:
        • Only acts when we are genuinely IDLE (not mid-recording or transcribing).
        • Only acts when the operating mode is still "vad" (user may have switched
          mode since the chunk was processed on the audio thread).
        """
        if self._state != STATE_IDLE:
            return  # Already recording or loading; ignore stale event
        if self._config.mode != "vad":
            return  # Mode changed since the audio chunk was processed

        logger.info("VAD: speech detected — starting auto-recording.")
        self._injector.capture_target()
        pre_roll = self._vad.get_pre_roll()
        self._recorder.start_recording()
        self._recorder.prepend_recording_audio(pre_roll)
        self._vad_recording_active = True  # Mark as VAD-initiated so auto-stop works
        self._recording_start_time = time.monotonic()
        self._recording_timer.start()
        self._set_state(STATE_RECORDING)

    def _on_vad_speech_end(self) -> None:
        """VAD detected end of speech (called on main thread via queue)."""
        if self._state != STATE_RECORDING or self._config.mode != "vad":
            return
        logger.info("VAD: silence detected — stopping recording.")
        self._stop_and_transcribe()

    # ── TranscriptionEngine signal handlers ───────────────────────────────────

    @pyqtSlot()
    def _on_model_loading(self) -> None:
        logger.info("Model loading …")
        self._set_state(STATE_LOADING)

    @pyqtSlot(str)
    def _on_model_downloading(self, model_name: str) -> None:
        from transcription.engine import _KNOWN_MODEL_SIZES_MB

        total_mb = _KNOWN_MODEL_SIZES_MB.get(model_name, -1)
        logger.info("Downloading model '%s' (~%d MB) …", model_name, total_mb)
        self.model_downloading.emit(model_name, total_mb)

    @pyqtSlot(int, int)
    def _on_download_progress(self, downloaded_mb: int, total_mb: int) -> None:
        self.download_progress.emit(downloaded_mb, total_mb)

    @pyqtSlot(str)
    def _on_model_ready(self, device: str) -> None:
        logger.info("Model ready on %s.", device)
        info = (
            f"Model '{self._config.model_size}' loaded on {device}  "
            f"({self._config.compute_type})"
        )
        self.model_info.emit(info)
        self._set_state(STATE_IDLE)

    @pyqtSlot(str)
    def _on_model_error(self, message: str) -> None:
        logger.error("Model error: %s", message)
        self.error_occurred.emit(f"Failed to load model: {message}")
        self._set_state(STATE_ERROR)

    @pyqtSlot()
    def _on_transcription_started(self) -> None:
        logger.debug("Transcription started.")

    @pyqtSlot(str)
    def _on_transcription_done(self, text: str) -> None:
        logger.info("Transcription result: %r", text[:80])

        if text:
            self._injector.inject(text)
            self.transcription_ready.emit(text)
        else:
            logger.info("Empty transcription — nothing to inject.")

        self._set_state(STATE_IDLE)

    @pyqtSlot(str)
    def _on_transcription_error(self, message: str) -> None:
        logger.error("Transcription error: %s", message)
        self.error_occurred.emit(f"Transcription failed: {message}")
        self._set_state(STATE_IDLE)

    # ── Recording timer tick ──────────────────────────────────────────────────

    @pyqtSlot()
    def _on_recording_tick(self) -> None:
        elapsed = time.monotonic() - self._recording_start_time
        self.recording_tick.emit(elapsed)

        # Safety: enforce a maximum recording length of 5 minutes
        if elapsed > 300.0 and self._state == STATE_RECORDING:
            logger.warning("Maximum recording length reached — auto-stopping.")
            self._stop_and_transcribe()

    # ── State machine helper ──────────────────────────────────────────────────

    def _set_state(self, new_state: str) -> None:
        if new_state == self._state:
            return
        logger.debug("State: %s → %s", self._state, new_state)
        self._state = new_state
        self.state_changed.emit(new_state)

    # ── Builders ──────────────────────────────────────────────────────────────

    def _build_vad(self) -> VADProcessor:
        return VADProcessor(
            aggressiveness=self._config.vad_aggressiveness,
            silence_duration=self._config.vad_silence_duration,
        )
