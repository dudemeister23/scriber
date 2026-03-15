"""Audio capture using sounddevice."""

import io
import math
import threading
import wave

import numpy as np
import sounddevice as sd


class AudioRecorder:
    """Records audio from the microphone with RMS level metering."""

    SAMPLE_RATE = 16000  # 16kHz mono — matches Scribe's pcm_s16le_16 preference
    CHANNELS = 1
    DTYPE = "int16"
    BLOCKSIZE = 1024

    def __init__(self):
        self._frames: list[bytes] = []
        self._stream = None
        self._lock = threading.Lock()
        self._recording = False
        self._current_rms: float = 0.0
        self._on_chunk = None  # callable(bytes) or None

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def rms_level(self) -> float:
        """Current audio RMS level, normalized to 0.0–1.0."""
        return self._current_rms

    def start(self, device=None):
        """Start recording. Pass a device index or None for system default."""
        with self._lock:
            self._frames = []
            self._current_rms = 0.0
            self._recording = True
            self._stream = sd.InputStream(
                samplerate=self.SAMPLE_RATE,
                channels=self.CHANNELS,
                dtype=self.DTYPE,
                blocksize=self.BLOCKSIZE,
                device=device,
                callback=self._callback,
            )
            self._stream.start()

    def stop(self) -> bytes:
        """Stop recording and return WAV bytes."""
        with self._lock:
            self._recording = False
            self._current_rms = 0.0
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
                self._stream = None
            return self._build_wav()

    def set_on_chunk(self, callback):
        """Set or clear the on_chunk callback for streaming mode."""
        self._on_chunk = callback

    def _callback(self, indata, frames, time_info, status):
        if self._recording:
            raw = indata.tobytes()
            self._frames.append(raw)
            # Compute RMS level (indata is numpy int16 array)
            rms = np.sqrt(np.mean(indata.astype(np.float32) ** 2))
            # Normalize: 800 ~ normal speech, sqrt curve to boost low/mid levels
            linear = min(rms / 800.0, 1.0)
            self._current_rms = math.sqrt(linear)
            # Stream chunk to external consumer if set
            if self._on_chunk is not None:
                try:
                    self._on_chunk(raw)
                except Exception:
                    pass  # don't crash the audio thread

    def _build_wav(self) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(self.CHANNELS)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(self.SAMPLE_RATE)
            wf.writeframes(b"".join(self._frames))
        return buf.getvalue()

    @staticmethod
    def get_input_devices() -> list[dict]:
        """Return list of audio input devices."""
        devices = sd.query_devices()
        result = []
        for i, d in enumerate(devices):
            if d["max_input_channels"] > 0:
                result.append({"index": i, "name": d["name"]})
        return result

    @staticmethod
    def resolve_device_name(name: str) -> int | None:
        """Resolve a device name to its current index. Returns None for system default."""
        if not name:
            return None
        devices = sd.query_devices()
        for i, d in enumerate(devices):
            if d["name"] == name and d["max_input_channels"] > 0:
                return i
        return None
