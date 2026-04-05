"""Offline file transcription with speaker diarization.

Pipeline: ffmpeg decode -> pyannote diarization -> Parakeet ASR per segment
-> merge consecutive same-speaker segments.

Fully offline after initial model download. No API calls.
"""

import io
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import wave
from dataclasses import dataclass

logger = logging.getLogger("scriber")

# Hardcoded models — this feature is locked to these specific offline models.
# pyannote.audio 4.x uses the "community-1" pipeline which bundles segmentation
# + PLDA. The 3.x "speaker-diarization-3.1" pipeline still works but pulls
# from community-1 anyway, requiring two EULAs.
DIARIZATION_MODEL_ID = "pyannote/speaker-diarization-community-1"
ASR_MODEL_ID = "mlx-community/parakeet-tdt-0.6b-v2"

TARGET_SR = 16000  # Both pyannote and Parakeet operate at 16 kHz
MIN_SEGMENT_SEC = 0.3  # Skip segments shorter than this (noise/artifacts)
MERGE_GAP_SEC = 2.0  # Merge same-speaker segments closer than this

# Supported audio/video extensions (ffmpeg decodes all of these)
SUPPORTED_EXTENSIONS = [
    "wav", "mp3", "m4a", "aac", "flac", "ogg", "opus", "wma",
    "mp4", "mov", "m4v", "mkv", "webm", "avi", "aiff", "aif",
]

os.environ.setdefault("HF_HOME", os.path.expanduser("~/.cache/huggingface"))
os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "0"


@dataclass
class TranscribedSegment:
    speaker: str  # e.g. "SPEAKER_00"
    start: float  # seconds
    end: float  # seconds
    text: str


# --- Dependency checks ---

def check_dependencies() -> str:
    """Return an empty string if all deps are met, else a user-facing message."""
    missing = []
    for mod in ("torch", "pyannote.audio"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        return (
            f"Missing Python packages: {', '.join(missing)}. "
            "Run: pip install -r requirements-file-transcribe.txt"
        )
    return ""


# --- ffmpeg / audio decoding ---

def find_ffmpeg() -> str:
    """Locate ffmpeg binary. Returns path or empty string."""
    # Check common paths first (brew installs to /opt/homebrew/bin on Apple Silicon)
    for candidate in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    found = shutil.which("ffmpeg")
    return found or ""


def decode_audio(file_path: str, sample_rate: int = TARGET_SR):
    """Decode any audio/video file to mono float32 numpy samples at target rate.

    Uses ffmpeg to handle any format. Returns (samples, sample_rate).
    """
    import numpy as np

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError(
            "ffmpeg not found. Install with: brew install ffmpeg"
        )

    if not os.path.isfile(file_path):
        raise RuntimeError(f"File not found: {file_path}")

    cmd = [
        ffmpeg,
        "-nostdin",
        "-i", file_path,
        "-f", "f32le",
        "-ac", "1",
        "-ar", str(sample_rate),
        "-loglevel", "error",
        "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, check=True)
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"ffmpeg decode failed: {stderr or e}")

    samples = np.frombuffer(result.stdout, dtype=np.float32)
    if samples.size == 0:
        raise RuntimeError("No audio decoded — file may be empty or corrupt")
    logger.info("Decoded %s: %d samples at %d Hz (%.1fs)",
                os.path.basename(file_path), len(samples), sample_rate,
                len(samples) / sample_rate)
    return samples, sample_rate


# --- pyannote diarization ---

_diarization_pipeline = None
_diarization_lock = threading.Lock()


def _load_diarization_pipeline(hf_token: str):
    """Load (and cache) the pyannote diarization pipeline."""
    global _diarization_pipeline
    with _diarization_lock:
        if _diarization_pipeline is not None:
            return _diarization_pipeline

        if not hf_token:
            raise RuntimeError(
                "HuggingFace token required — set it in Settings"
            )

        logger.info("Loading diarization pipeline (%s)...", DIARIZATION_MODEL_ID)
        from pyannote.audio import Pipeline
        import torch

        # Disable pyannote's OTEL telemetry — it's chatty over the network
        # and gives us no value. Silently no-ops on older pyannote.
        try:
            from pyannote.audio.telemetry.metrics import set_telemetry_metrics
            set_telemetry_metrics(False)
        except Exception as e:
            logger.debug("Could not disable pyannote telemetry: %s", e)

        pipeline = Pipeline.from_pretrained(
            DIARIZATION_MODEL_ID,
            token=hf_token,
        )
        if pipeline is None:
            raise RuntimeError(
                "Failed to load diarization pipeline. "
                "Verify HF token and that you have accepted the EULA at "
                "https://huggingface.co/pyannote/speaker-diarization-community-1"
            )

        # Move to MPS if available (faster on Apple Silicon)
        if torch.backends.mps.is_available():
            try:
                pipeline.to(torch.device("mps"))
                logger.info("Diarization pipeline moved to MPS")
            except Exception as e:
                logger.warning("Could not move pipeline to MPS (%s), using CPU", e)
        else:
            logger.info("MPS unavailable, diarization on CPU")

        _diarization_pipeline = pipeline
        return pipeline


def diarize(samples, sample_rate: int, hf_token: str, num_speakers: int = 0,
            on_progress=None):
    """Run pyannote diarization on audio samples.

    Args:
        samples: float32 numpy array of mono audio
        sample_rate: sample rate in Hz
        hf_token: HuggingFace token
        num_speakers: 0 = auto-detect, >0 = fixed speaker count
        on_progress: optional callable(str) for per-step status updates

    Returns:
        list of (speaker_label, start_sec, end_sec) tuples
    """
    import torch

    pipeline = _load_diarization_pipeline(hf_token)

    # pyannote expects (channels, samples) tensor
    waveform = torch.from_numpy(samples).unsqueeze(0)

    kwargs = {}
    if num_speakers and num_speakers > 0:
        kwargs["num_speakers"] = num_speakers

    # Progress hook: pyannote.audio invokes this during each pipeline step
    # (segmentation, embedding, clustering) with completed/total counts.
    if on_progress:
        def _hook(step_name, step_artifact=None, file=None,
                  total=None, completed=None):
            label = step_name.replace("_", " ").capitalize()
            if total and completed is not None:
                pct = int(completed * 100 / total) if total else 0
                on_progress(f"Diarizing: {label} {completed}/{total} ({pct}%)")
            else:
                on_progress(f"Diarizing: {label}\u2026")
            logger.info("pyannote step: %s %s/%s", step_name, completed, total)
        kwargs["hook"] = _hook

    result = pipeline(
        {"waveform": waveform, "sample_rate": sample_rate},
        **kwargs,
    )

    # pyannote.audio 4.x returns a DiarizeOutput dataclass; 3.x returned an
    # Annotation directly. Handle both.
    if hasattr(result, "exclusive_speaker_diarization"):
        # 4.x — prefer the exclusive annotation (no overlapping speech)
        # since we transcribe each segment independently.
        diarization = result.exclusive_speaker_diarization
    elif hasattr(result, "speaker_diarization"):
        diarization = result.speaker_diarization
    else:
        diarization = result  # 3.x Annotation

    segments = []
    for turn, _, speaker in diarization.itertracks(yield_label=True):
        segments.append((speaker, turn.start, turn.end))
    logger.info("Diarization: %d segments, %d speakers",
                len(segments), len({s[0] for s in segments}))
    return segments


# --- Parakeet ASR ---

_asr_model = None
_asr_lock = threading.Lock()


def _load_asr_model():
    global _asr_model
    with _asr_lock:
        if _asr_model is not None:
            return _asr_model
        logger.info("Loading Parakeet ASR model (%s)...", ASR_MODEL_ID)
        from mlx_audio.stt import load_model
        _asr_model = load_model(ASR_MODEL_ID)
        logger.info("Parakeet ASR model loaded")
        return _asr_model


def _samples_to_wav_bytes(samples, sample_rate: int) -> bytes:
    """Convert float32 samples to 16-bit PCM WAV bytes."""
    import numpy as np
    clipped = np.clip(samples, -1.0, 1.0)
    int_samples = (clipped * 32767.0).astype(np.int16)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(int_samples.tobytes())
    return buf.getvalue()


def _transcribe_chunk(model, samples, sample_rate: int) -> str:
    """Transcribe a single audio chunk with Parakeet."""
    wav_bytes = _samples_to_wav_bytes(samples, sample_rate)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(wav_bytes)
        tmp_path = tmp.name
    try:
        result = model.generate(tmp_path)
        if hasattr(result, "text"):
            return result.text.strip()
        return str(result).strip()
    finally:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def transcribe_segments(samples, sample_rate: int, raw_segments,
                        on_progress=None, cancel_event=None):
    """Transcribe each diarization segment with Parakeet.

    Returns a list of TranscribedSegment, skipping segments that are too short
    or produced no text.
    """
    model = _load_asr_model()

    results = []
    total = len(raw_segments)
    for i, (speaker, start, end) in enumerate(raw_segments):
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("Cancelled")

        if on_progress:
            on_progress(f"Transcribing segment {i + 1}/{total}\u2026")

        duration = end - start
        if duration < MIN_SEGMENT_SEC:
            logger.debug("Skipping short segment %.2fs (%s)", duration, speaker)
            continue

        start_sample = int(start * sample_rate)
        end_sample = int(end * sample_rate)
        chunk = samples[start_sample:end_sample]
        if chunk.size == 0:
            continue

        try:
            text = _transcribe_chunk(model, chunk, sample_rate)
        except Exception as e:
            logger.warning("Segment %d transcription failed: %s", i, e)
            continue

        if text:
            results.append(TranscribedSegment(
                speaker=speaker, start=start, end=end, text=text,
            ))

    return results


# --- Post-processing ---

def merge_consecutive(segments):
    """Merge adjacent same-speaker segments separated by less than MERGE_GAP_SEC."""
    if not segments:
        return segments
    merged = [segments[0]]
    for seg in segments[1:]:
        last = merged[-1]
        if seg.speaker == last.speaker and (seg.start - last.end) < MERGE_GAP_SEC:
            merged[-1] = TranscribedSegment(
                speaker=last.speaker,
                start=last.start,
                end=seg.end,
                text=(last.text + " " + seg.text).strip(),
            )
        else:
            merged.append(seg)
    return merged


# --- Pipeline entry point ---

def run_pipeline(file_path: str, hf_token: str, num_speakers: int = 0,
                 on_progress=None, cancel_event=None):
    """Run the full file-transcription pipeline.

    Args:
        file_path: path to an audio or video file
        hf_token: HuggingFace token (needed to load pyannote model)
        num_speakers: 0 = auto-detect, >0 = fixed count
        on_progress: callable(str) for status updates
        cancel_event: threading.Event to abort

    Returns:
        list of TranscribedSegment, merged.
    """
    if not os.path.isfile(file_path):
        raise RuntimeError(f"File not found: {file_path}")

    if on_progress:
        on_progress("Decoding audio\u2026")
    samples, sample_rate = decode_audio(file_path)

    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("Cancelled")

    duration = len(samples) / sample_rate
    if on_progress:
        on_progress(f"Diarizing {duration:.0f}s of audio\u2026")
    raw_segments = diarize(samples, sample_rate, hf_token, num_speakers,
                           on_progress=on_progress)

    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("Cancelled")

    if not raw_segments:
        return []

    transcribed = transcribe_segments(
        samples, sample_rate, raw_segments,
        on_progress=on_progress, cancel_event=cancel_event,
    )
    merged = merge_consecutive(transcribed)
    logger.info("File transcription complete: %d merged segments", len(merged))
    return merged


# --- Export formats ---

def _fmt_timestamp(seconds: float) -> str:
    """Format seconds as MM:SS or HH:MM:SS."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _fmt_srt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms >= 1000:
        ms = 999
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def format_txt(segments) -> str:
    """Format segments as plain text with speaker labels and timestamps."""
    if not segments:
        return ""
    lines = []
    for seg in segments:
        start = _fmt_timestamp(seg.start)
        end = _fmt_timestamp(seg.end)
        lines.append(f"{seg.speaker} ({start}\u2013{end}):")
        lines.append(seg.text)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def format_srt(segments) -> str:
    """Format segments as SRT subtitle file."""
    if not segments:
        return ""
    lines = []
    for i, seg in enumerate(segments, 1):
        lines.append(str(i))
        lines.append(f"{_fmt_srt_time(seg.start)} --> {_fmt_srt_time(seg.end)}")
        lines.append(f"{seg.speaker}: {seg.text}")
        lines.append("")
    return "\n".join(lines)


# --- Model status / download ---

def is_diarization_model_downloaded() -> bool:
    """Check whether the pyannote diarization model repo is cached locally."""
    try:
        from huggingface_hub import scan_cache_dir
        cache = scan_cache_dir()
        return any(r.repo_id == DIARIZATION_MODEL_ID for r in cache.repos)
    except Exception as e:
        logger.debug("Error checking diarization cache: %s", e)
        return False


def is_asr_model_downloaded() -> bool:
    """Check whether the Parakeet model is cached locally."""
    try:
        from huggingface_hub import scan_cache_dir
        cache = scan_cache_dir()
        return any(r.repo_id == ASR_MODEL_ID for r in cache.repos)
    except Exception:
        return False


def download_diarization_model(hf_token: str, on_progress=None,
                               on_complete=None, on_error=None):
    """Download the pyannote diarization model weights via huggingface_hub.

    Does NOT require pyannote.audio to be installed — just pulls the files
    into the HF cache so pyannote can load them later, offline.
    """
    def _download():
        try:
            if not hf_token:
                raise RuntimeError(
                    "HuggingFace token required — add it in Settings first"
                )
            from huggingface_hub import snapshot_download

            if on_progress:
                on_progress("Downloading diarization pipeline\u2026")
            snapshot_download(
                DIARIZATION_MODEL_ID, repo_type="model", token=hf_token,
            )
            logger.info("Diarization model downloaded")
            if on_complete:
                on_complete()
        except Exception as e:
            import traceback
            logger.error("Diarization model download failed: %s\n%s",
                         e, traceback.format_exc())
            if on_error:
                on_error(str(e))

    thread = threading.Thread(target=_download, daemon=True)
    thread.start()
    return thread


def download_asr_model(on_progress=None, on_complete=None, on_error=None):
    """Download the Parakeet ASR model in a background thread."""
    def _download():
        try:
            if on_progress:
                on_progress("Downloading Parakeet model\u2026")
            from huggingface_hub import snapshot_download
            snapshot_download(ASR_MODEL_ID, repo_type="model")
            if on_complete:
                on_complete()
        except Exception as e:
            logger.error("Parakeet download failed: %s", e)
            if on_error:
                on_error(str(e))

    thread = threading.Thread(target=_download, daemon=True)
    thread.start()
    return thread
