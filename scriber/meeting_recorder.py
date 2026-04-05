"""Meeting recorder: mic + system audio mixed into a single WAV file.

Runs two captures in parallel (existing AudioRecorder for the mic,
SystemAudioTap for system audio), drains both every 500 ms from a worker
thread, mixes equal-length chunks, and appends to a wave.Wave_write sink
so memory stays bounded for multi-hour meetings.

The WAV is written to ~/Documents/Scriber Meetings/YYYY-MM-DD_HHMMSS.wav
(override via `meetings_dir` constructor arg). Output format is 16-bit
mono PCM at 16 kHz — identical to AudioRecorder, so the file drops
straight into the existing file-transcription pipeline.
"""

from __future__ import annotations

import datetime
import logging
import os
import threading
import time
import wave

import numpy as np

from .audio import AudioRecorder
from .system_audio import SystemAudioTap, SystemAudioUnavailable

logger = logging.getLogger("scriber")

DEFAULT_MEETINGS_DIR = os.path.expanduser("~/Documents/Scriber Meetings")

# How often the mixer wakes up to drain both sources and append to the WAV.
MIXER_TICK_SECONDS = 0.5

# Each source is attenuated before summing so combined peaks don't clip.
MIX_GAIN = 0.7


class MeetingRecorder:
    """Coordinates mic + system audio capture into a single mixed WAV file."""

    def __init__(self, meetings_dir: str = "", mic_device: int | None = None):
        self._meetings_dir = os.path.expanduser(meetings_dir) if meetings_dir else DEFAULT_MEETINGS_DIR
        self._mic_device = mic_device
        self._mic_rec = AudioRecorder()
        self._tap = SystemAudioTap()  # may raise SystemAudioUnavailable

        self._mic_chunks: list[bytes] = []
        self._mic_lock = threading.Lock()
        self._mic_leftover = np.array([], dtype=np.int16)
        self._tap_leftover = np.array([], dtype=np.int16)

        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._writer: wave.Wave_write | None = None
        self._writer_lock = threading.Lock()
        self._file_path = ""
        self._start_time = 0.0

    @property
    def file_path(self) -> str:
        return self._file_path

    @property
    def elapsed_seconds(self) -> float:
        if self._start_time == 0.0:
            return 0.0
        return time.monotonic() - self._start_time

    @property
    def is_recording(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    # --- lifecycle ---

    def start(self) -> str:
        """Create the output WAV, start both captures, launch the mixer thread."""
        if self.is_recording:
            return self._file_path

        os.makedirs(self._meetings_dir, exist_ok=True)
        name = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S") + ".wav"
        self._file_path = os.path.join(self._meetings_dir, name)

        self._writer = wave.open(self._file_path, "wb")
        self._writer.setnchannels(AudioRecorder.CHANNELS)
        self._writer.setsampwidth(2)  # int16
        self._writer.setframerate(AudioRecorder.SAMPLE_RATE)

        # Mic — use the on_chunk hook rather than the final WAV output so we
        # can mix incrementally.
        self._mic_chunks = []
        self._mic_leftover = np.array([], dtype=np.int16)
        self._tap_leftover = np.array([], dtype=np.int16)
        self._mic_rec.set_on_chunk(self._on_mic_chunk)
        try:
            self._mic_rec.start(device=self._mic_device)
        except Exception as e:
            self._close_writer()
            self._mic_rec.set_on_chunk(None)
            raise RuntimeError(f"Could not start microphone: {e}") from e

        # System audio tap — if this fails, tear down the mic and propagate.
        try:
            self._tap.start()
        except SystemAudioUnavailable:
            self._mic_rec.set_on_chunk(None)
            self._mic_rec.stop()
            self._close_writer()
            raise

        self._start_time = time.monotonic()
        self._stop_event.clear()
        self._worker = threading.Thread(
            target=self._mixer_loop, name="MeetingMixer", daemon=True
        )
        self._worker.start()
        logger.info("Meeting recording started: %s", self._file_path)
        return self._file_path

    def stop(self) -> str:
        """Stop both captures, flush remaining audio, finalize the WAV. Returns path."""
        if not self.is_recording and not self._writer:
            return self._file_path

        self._stop_event.set()

        # Stop the mic (callback detaches, buffered frames stay in _mic_chunks)
        self._mic_rec.set_on_chunk(None)
        try:
            self._mic_rec.stop()
        except Exception as e:
            logger.warning("Mic stop error: %s", e)

        # Stop the tap — remaining samples flushed via _tap.stop()'s return,
        # but we already drain via get_pcm_bytes() in the loop; stop() also
        # returns any final batch which we merge into the leftover.
        try:
            final_tap = self._tap.stop()
            if final_tap:
                self._tap_leftover = np.concatenate([
                    self._tap_leftover,
                    np.frombuffer(final_tap, dtype=np.int16),
                ])
        except Exception as e:
            logger.warning("Tap stop error: %s", e)

        # Let the mixer thread drain any final data it may have queued
        if self._worker:
            self._worker.join(timeout=3.0)
            self._worker = None

        # Final flush: mix what we can, then append any single-source tail.
        self._mix_and_write(final=True)

        self._close_writer()
        logger.info("Meeting recording stopped: %s (%.1fs)",
                    self._file_path, self.elapsed_seconds)
        return self._file_path

    # --- mic callback ---

    def _on_mic_chunk(self, raw: bytes):
        with self._mic_lock:
            self._mic_chunks.append(raw)

    # --- mixer loop ---

    def _mixer_loop(self):
        while not self._stop_event.wait(MIXER_TICK_SECONDS):
            try:
                self._mix_and_write(final=False)
            except Exception as e:
                logger.error("Mixer iteration failed: %s", e)

    def _mix_and_write(self, final: bool):
        """Drain both sources, mix equal-length portion, write to WAV."""
        # Drain mic
        with self._mic_lock:
            mic_chunks = self._mic_chunks
            self._mic_chunks = []
        if mic_chunks:
            new_mic = np.frombuffer(b"".join(mic_chunks), dtype=np.int16)
            mic = np.concatenate([self._mic_leftover, new_mic]) if self._mic_leftover.size \
                else new_mic
        else:
            mic = self._mic_leftover

        # Drain tap (no-op if tap was already stopped — leftovers still apply)
        tap_pcm = self._tap.get_pcm_bytes() if self._tap.is_running else b""
        if tap_pcm:
            new_tap = np.frombuffer(tap_pcm, dtype=np.int16)
            tap = np.concatenate([self._tap_leftover, new_tap]) if self._tap_leftover.size \
                else new_tap
        else:
            tap = self._tap_leftover

        n = min(len(mic), len(tap))
        if n > 0:
            mixed = (
                mic[:n].astype(np.int32) * MIX_GAIN
                + tap[:n].astype(np.int32) * MIX_GAIN
            )
            np.clip(mixed, -32768, 32767, out=mixed)
            self._write_frames(mixed.astype(np.int16).tobytes())

        mic_tail = mic[n:]
        tap_tail = tap[n:]

        if final:
            # Append whichever stream has leftover as-is (mono, attenuated to
            # match the mixed body so the volume matches). We don't want to
            # lose audio at the very end just because one stream drifted.
            longest = mic_tail if len(mic_tail) >= len(tap_tail) else tap_tail
            if longest.size > 0:
                tail = (longest.astype(np.int32) * MIX_GAIN)
                np.clip(tail, -32768, 32767, out=tail)
                self._write_frames(tail.astype(np.int16).tobytes())
            self._mic_leftover = np.array([], dtype=np.int16)
            self._tap_leftover = np.array([], dtype=np.int16)
        else:
            self._mic_leftover = mic_tail
            self._tap_leftover = tap_tail

    def _write_frames(self, data: bytes):
        with self._writer_lock:
            if self._writer is not None:
                self._writer.writeframes(data)

    def _close_writer(self):
        with self._writer_lock:
            if self._writer is not None:
                try:
                    self._writer.close()
                except Exception as e:
                    logger.warning("WAV close error: %s", e)
                self._writer = None
