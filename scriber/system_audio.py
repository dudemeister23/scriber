"""System audio capture via Core Audio Process Taps (macOS 14.4+).

Taps all process output into a mono Float32 stream at the system sample rate
(typically 48 kHz), resamples to 16 kHz mono, and exposes the result as
16-bit PCM bytes compatible with AudioRecorder.

Why Core Audio taps instead of ScreenCaptureKit: taps only require the
microphone entitlement Scriber already holds, so they do NOT trigger the
purple "Screen is being recorded" indicator in the menu bar. That keeps the
feature subtle during screen shares.

Architecture
------------
- CATapDescription configures a mono global tap (every process, no exclusions).
- AudioHardwareCreateProcessTap produces an AudioObjectID.
- AudioHardwareCreateAggregateDevice wraps the tap so we can run an IOProc
  on it via the standard device API.
- The IOProc is registered with ctypes (pyobjc can't round-trip the opaque
  AudioDeviceIOProcID pointer between create/start/stop/destroy).
- Float32 samples are copied out of the Core Audio buffers in the IOProc,
  pushed to a deque, and drained by .stop() where we resample + quantize.

Resampling uses 3:1 block averaging (48k -> 16k), which is a cheap low-pass
that's perfectly adequate for speech transcription.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import platform
import threading
import uuid
from collections import deque

import numpy as np

import CoreAudio

logger = logging.getLogger("scriber")


class SystemAudioUnavailable(RuntimeError):
    """Raised when Core Audio process taps can't be used on this system."""


# -------------------------------------------------------------------------
# ctypes declarations
# -------------------------------------------------------------------------

_ca = ctypes.CDLL(ctypes.util.find_library("CoreAudio"))

AudioObjectID = ctypes.c_uint32
AudioDeviceIOProcID = ctypes.c_void_p
OSStatus = ctypes.c_int32


class _AudioTimeStamp(ctypes.Structure):
    _fields_ = [
        ("mSampleTime", ctypes.c_double),
        ("mHostTime", ctypes.c_uint64),
        ("mRateScalar", ctypes.c_double),
        ("mWordClockTime", ctypes.c_uint64),
        ("mSMPTETime", ctypes.c_byte * 32),
        ("mFlags", ctypes.c_uint32),
        ("mReserved", ctypes.c_uint32),
    ]


class _AudioBuffer(ctypes.Structure):
    _fields_ = [
        ("mNumberChannels", ctypes.c_uint32),
        ("mDataByteSize", ctypes.c_uint32),
        ("mData", ctypes.c_void_p),
    ]


class _AudioBufferList(ctypes.Structure):
    # mBuffers is a flexible array — only the first element is in the decl;
    # subsequent elements are accessed via pointer arithmetic on the address.
    _fields_ = [
        ("mNumberBuffers", ctypes.c_uint32),
        ("mBuffers", _AudioBuffer * 1),
    ]


_IO_PROC = ctypes.CFUNCTYPE(
    OSStatus,
    AudioObjectID,
    ctypes.POINTER(_AudioTimeStamp),
    ctypes.POINTER(_AudioBufferList),
    ctypes.POINTER(_AudioTimeStamp),
    ctypes.POINTER(_AudioBufferList),
    ctypes.POINTER(_AudioTimeStamp),
    ctypes.c_void_p,
)

_ca.AudioDeviceCreateIOProcID.argtypes = [
    AudioObjectID, _IO_PROC, ctypes.c_void_p, ctypes.POINTER(AudioDeviceIOProcID)
]
_ca.AudioDeviceCreateIOProcID.restype = OSStatus
_ca.AudioDeviceStart.argtypes = [AudioObjectID, AudioDeviceIOProcID]
_ca.AudioDeviceStart.restype = OSStatus
_ca.AudioDeviceStop.argtypes = [AudioObjectID, AudioDeviceIOProcID]
_ca.AudioDeviceStop.restype = OSStatus
_ca.AudioDeviceDestroyIOProcID.argtypes = [AudioObjectID, AudioDeviceIOProcID]
_ca.AudioDeviceDestroyIOProcID.restype = OSStatus


# -------------------------------------------------------------------------
# Version check
# -------------------------------------------------------------------------

def _require_supported_macos() -> None:
    """Raise SystemAudioUnavailable if Core Audio taps aren't supported."""
    ver = platform.mac_ver()[0] or "0"
    parts = ver.split(".")
    try:
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        raise SystemAudioUnavailable(f"Could not parse macOS version: {ver}")
    # 14.4 = Sonoma 14.4 when AudioHardwareCreateProcessTap went public.
    # macOS 15+ and the new-numbering (26+) all satisfy this.
    if (major, minor) < (14, 4) and major < 15:
        raise SystemAudioUnavailable(
            f"macOS 14.4 or later required for system audio capture (found {ver})."
        )


def is_supported() -> bool:
    """Cheap check used by UI code to enable/disable the meeting-recording menu item."""
    try:
        _require_supported_macos()
        return True
    except SystemAudioUnavailable:
        return False


# -------------------------------------------------------------------------
# SystemAudioTap
# -------------------------------------------------------------------------

# CoreAudio dict keys come through pyobjc as bytes (4-char codes); the
# dict passed to AudioHardwareCreateAggregateDevice needs str keys.
def _k(x):
    return x.decode("ascii") if isinstance(x, bytes) else x


TAP_SAMPLE_RATE = 48000  # Core Audio process taps deliver at device rate (48 kHz)
OUTPUT_SAMPLE_RATE = 16000  # Match AudioRecorder
DECIMATION = TAP_SAMPLE_RATE // OUTPUT_SAMPLE_RATE  # 3:1


class SystemAudioTap:
    """Records system-wide audio output to mono 16 kHz 16-bit PCM bytes.

    Usage:
        tap = SystemAudioTap()
        tap.start()
        ...
        pcm_bytes = tap.stop()  # raw bytes, same format as AudioRecorder frames

    Not thread-safe for concurrent start/stop calls. Safe to read
    .is_running / .get_pcm_bytes() from other threads while running.
    """

    def __init__(self):
        _require_supported_macos()
        self._tap_id = 0
        self._agg_id = 0
        self._proc_id = AudioDeviceIOProcID()
        self._io_proc_cfunc = None  # hold ref to prevent GC of the callback
        self._desc = None  # hold CATapDescription ref
        self._running = False

        # Float32 chunks captured by the IOProc
        self._chunks: deque = deque()
        self._chunks_lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        return self._running

    # --- lifecycle ---

    def start(self):
        if self._running:
            return

        # 1. Create the tap description (mono, all processes, no exclusions)
        self._desc = (
            CoreAudio.CATapDescription.alloc()
            .initMonoGlobalTapButExcludeProcesses_([])
        )
        self._desc.setName_("Scriber Meeting Tap")

        err, tap_id = CoreAudio.AudioHardwareCreateProcessTap(self._desc, None)
        if err != 0:
            self._desc = None
            raise SystemAudioUnavailable(
                f"AudioHardwareCreateProcessTap failed (OSStatus {err}). "
                "Make sure Scriber has the microphone entitlement."
            )
        self._tap_id = tap_id
        tap_uid = self._desc.UUID().UUIDString()

        # 2. Create an aggregate device that wraps the tap so we can run an
        #    IOProc on it.
        agg_dict = {
            _k(CoreAudio.kAudioAggregateDeviceNameKey): "Scriber Meeting Aggregate",
            _k(CoreAudio.kAudioAggregateDeviceUIDKey): str(uuid.uuid4()),
            _k(CoreAudio.kAudioAggregateDeviceIsPrivateKey): 1,
            _k(CoreAudio.kAudioAggregateDeviceIsStackedKey): 0,
            _k(CoreAudio.kAudioAggregateDeviceTapListKey): [
                {
                    _k(CoreAudio.kAudioSubTapUIDKey): tap_uid,
                    _k(CoreAudio.kAudioSubTapDriftCompensationKey): 1,
                }
            ],
        }
        err, agg_id = CoreAudio.AudioHardwareCreateAggregateDevice(agg_dict, None)
        if err != 0:
            CoreAudio.AudioHardwareDestroyProcessTap(self._tap_id)
            self._tap_id = 0
            self._desc = None
            raise SystemAudioUnavailable(
                f"AudioHardwareCreateAggregateDevice failed (OSStatus {err})."
            )
        self._agg_id = agg_id

        # 3. Register the IOProc via ctypes. Must retain the CFUNCTYPE ref.
        self._chunks.clear()
        self._io_proc_cfunc = _IO_PROC(self._io_proc)
        err = _ca.AudioDeviceCreateIOProcID(
            self._agg_id, self._io_proc_cfunc, None, ctypes.byref(self._proc_id)
        )
        if err != 0:
            self._cleanup_device()
            raise SystemAudioUnavailable(
                f"AudioDeviceCreateIOProcID failed (OSStatus {err})."
            )

        # 4. Start.
        err = _ca.AudioDeviceStart(self._agg_id, self._proc_id)
        if err != 0:
            _ca.AudioDeviceDestroyIOProcID(self._agg_id, self._proc_id)
            self._proc_id = AudioDeviceIOProcID()
            self._cleanup_device()
            raise SystemAudioUnavailable(
                f"AudioDeviceStart failed (OSStatus {err})."
            )

        self._running = True
        logger.info("SystemAudioTap started (tap=%s agg=%s)", self._tap_id, self._agg_id)

    def stop(self) -> bytes:
        """Stop the tap and return accumulated PCM as 16-bit mono 16kHz bytes."""
        if not self._running:
            return b""
        _ca.AudioDeviceStop(self._agg_id, self._proc_id)
        _ca.AudioDeviceDestroyIOProcID(self._agg_id, self._proc_id)
        self._proc_id = AudioDeviceIOProcID()
        self._running = False
        self._cleanup_device()

        pcm = self._drain_pcm_bytes()
        logger.info("SystemAudioTap stopped (%d output bytes)", len(pcm))
        # Release callback ref now that the IOProc is no longer referenced.
        self._io_proc_cfunc = None
        return pcm

    def get_pcm_bytes(self) -> bytes:
        """Drain whatever has been captured so far without stopping. Thread-safe."""
        return self._drain_pcm_bytes()

    def _cleanup_device(self):
        if self._agg_id:
            CoreAudio.AudioHardwareDestroyAggregateDevice(self._agg_id)
            self._agg_id = 0
        if self._tap_id:
            CoreAudio.AudioHardwareDestroyProcessTap(self._tap_id)
            self._tap_id = 0
        self._desc = None

    # --- IOProc (runs on Core Audio real-time thread) ---

    def _io_proc(self, device, now, in_data, in_time, out_data, out_time, client_data):
        # Runs on Core Audio's real-time thread. Copy samples out and return fast.
        try:
            if not in_data:
                return 0
            abl = in_data.contents
            n_buffers = abl.mNumberBuffers
            # Tap is mono, so there will be one buffer with 1 channel of Float32.
            base_addr = ctypes.addressof(abl.mBuffers)
            for i in range(n_buffers):
                buf = _AudioBuffer.from_address(
                    base_addr + i * ctypes.sizeof(_AudioBuffer)
                )
                if not buf.mData or buf.mDataByteSize == 0:
                    continue
                n_floats = buf.mDataByteSize // 4
                # Copy out of the Core Audio buffer — it's reused on next callback.
                arr = np.ctypeslib.as_array(
                    (ctypes.c_float * n_floats).from_address(buf.mData)
                ).copy()
                # If the tap unexpectedly gave us >1 channel, downmix to mono.
                channels = buf.mNumberChannels or 1
                if channels > 1:
                    arr = arr.reshape(-1, channels).mean(axis=1)
                with self._chunks_lock:
                    self._chunks.append(arr)
        except Exception as e:
            # Must not raise out of the IOProc. Log nothing here either —
            # logging would take the GIL unpredictably.
            pass
        return 0

    # --- PCM drain / resample ---

    def _drain_pcm_bytes(self) -> bytes:
        with self._chunks_lock:
            chunks = list(self._chunks)
            self._chunks.clear()
        if not chunks:
            return b""
        mono_f32 = np.concatenate(chunks)
        # 48k -> 16k: block-average every 3 samples.
        # Acts as a low-pass @ 8kHz which is fine for speech.
        trimmed = mono_f32[: (len(mono_f32) // DECIMATION) * DECIMATION]
        if trimmed.size == 0:
            return b""
        downsampled = trimmed.reshape(-1, DECIMATION).mean(axis=1)
        # Float32 [-1, 1] -> int16 PCM
        clipped = np.clip(downsampled, -1.0, 1.0)
        int16 = (clipped * 32767.0).astype(np.int16)
        return int16.tobytes()
