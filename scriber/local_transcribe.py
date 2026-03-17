"""Local speech-to-text via mlx-audio (Apple Silicon)."""

import logging
import os
import tempfile

logger = logging.getLogger("scriber")

MODELS = {
    "granite": {
        "id": "ibm-granite/granite-4.0-1b-speech",
        "label": "Granite 4.0 1B",
        "size": "~4.4 GB",
        "needs_punct": True,
        "description": "Best accuracy (ASR leaderboard #1). Requires punctuation post-processing.",
    },
    "parakeet": {
        "id": "mlx-community/parakeet-tdt-0.6b-v2",
        "label": "Parakeet TDT 0.6B",
        "size": "~2.4 GB",
        "needs_punct": False,
        "description": "Lightweight model with native punctuation.",
    },
}
DEFAULT_MODEL = "granite"

# Ensure HuggingFace cache dir is set (py2app may not inherit shell env)
os.environ.setdefault("HF_HOME", os.path.expanduser("~/.cache/huggingface"))
# Disable hf-xet binary transport (not available in py2app bundles)
os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "0"


def _find_ca_certs():
    """Find a valid CA certificate bundle, trying multiple strategies.

    py2app bundles may not preserve certifi's cacert.pem in the expected
    location, so we try several fallbacks:
    1. certifi.where() — works when running from source
    2. cacert.pem next to certifi's __init__.py — works if py2app copied it
    3. /etc/ssl/cert.pem — macOS system CA bundle (always exists)
    """
    try:
        import certifi
        path = certifi.where()
        if os.path.isfile(path):
            logger.debug("CA certs found via certifi.where(): %s", path)
            return path
        logger.debug("certifi.where() returned non-existent path: %s", path)
        certifi_dir = os.path.dirname(os.path.abspath(certifi.__file__))
        fallback = os.path.join(certifi_dir, "cacert.pem")
        if os.path.isfile(fallback):
            logger.debug("CA certs found next to certifi module: %s", fallback)
            return fallback
    except ImportError:
        logger.debug("certifi not importable")

    macos_certs = "/etc/ssl/cert.pem"
    if os.path.isfile(macos_certs):
        logger.debug("Using macOS system CA certs: %s", macos_certs)
        return macos_certs

    logger.warning("No CA certificate bundle found!")
    return None


# Fix SSL for py2app: the bundled Python can't find system CA certs.
_CA_CERT_PATH = _find_ca_certs()
if _CA_CERT_PATH:
    import ssl

    def _patched_create_default_context(purpose=ssl.Purpose.SERVER_AUTH, *, cafile=None, capath=None, cadata=None):
        if cafile is None and capath is None and cadata is None:
            cafile = _CA_CERT_PATH
        if cafile and not os.path.isfile(cafile):
            cafile = _CA_CERT_PATH
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.check_hostname = True
        if cafile or capath or cadata:
            ctx.load_verify_locations(cafile=cafile, capath=capath, cadata=cadata)
        return ctx

    ssl.create_default_context = _patched_create_default_context
    os.environ.setdefault("SSL_CERT_FILE", _CA_CERT_PATH)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _CA_CERT_PATH)

# Check if mlx-audio is available (Apple Silicon only)
_MLX_AVAILABLE = False
try:
    import mlx_audio  # noqa: F401
    _MLX_AVAILABLE = True
except ImportError:
    pass


def _get_model_id(model_key: str = None) -> str:
    """Resolve model key to HuggingFace model ID."""
    key = model_key or DEFAULT_MODEL
    return MODELS.get(key, MODELS[DEFAULT_MODEL])["id"]


def is_mlx_available() -> bool:
    """Check if mlx-audio is installed and importable."""
    return _MLX_AVAILABLE


def _is_repo_cached(repo_id: str) -> bool:
    """Check if a HuggingFace repo is present in the local cache."""
    try:
        from huggingface_hub import scan_cache_dir
        cache_info = scan_cache_dir()
        for repo in cache_info.repos:
            if repo.repo_id == repo_id:
                return True
        return False
    except Exception as e:
        logger.debug("Error checking model cache for %s: %s", repo_id, e)
        return False


def is_model_downloaded(model_key: str = None) -> bool:
    """Check if a speech model is cached locally."""
    if not _MLX_AVAILABLE:
        return False
    model_id = _get_model_id(model_key)
    return _is_repo_cached(model_id)


def is_punct_model_downloaded() -> bool:
    """Check if the punctuation post-processing model is cached."""
    return _is_repo_cached(PUNCT_MODEL_ID)


def download_model(model_key=None, on_progress=None, on_complete=None, on_error=None):
    """Download a speech model (and its punctuation model if needed) in a background thread."""
    import threading
    model_id = _get_model_id(model_key)
    key = model_key or DEFAULT_MODEL
    model_info = MODELS.get(key, MODELS[DEFAULT_MODEL])

    def _download():
        try:
            os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "0"
            os.environ.setdefault("HF_HOME", os.path.expanduser("~/.cache/huggingface"))
            from huggingface_hub import snapshot_download

            # Download the speech model
            if on_progress:
                on_progress(f"Downloading {model_info['label']}\u2026")
            snapshot_download(model_id, repo_type="model")
            logger.info("Model download complete: %s", model_id)

            # If this model needs punctuation post-processing, also download that
            if model_info.get("needs_punct") and not _is_repo_cached(PUNCT_MODEL_ID):
                if on_progress:
                    on_progress("Downloading punctuation model\u2026")
                snapshot_download(PUNCT_MODEL_ID, repo_type="model")
                logger.info("Punctuation model download complete: %s", PUNCT_MODEL_ID)

            if on_complete:
                on_complete()
        except Exception as e:
            import traceback
            logger.error("Model download failed: %s\n%s", e, traceback.format_exc())
            if on_error:
                on_error(str(e))

    thread = threading.Thread(target=_download, daemon=True)
    thread.start()
    return thread


def get_model_status() -> dict:
    """Get download status for all models. Returns {key: bool}."""
    status = {}
    for key in MODELS:
        downloaded = is_model_downloaded(key)
        # For models needing punctuation, both must be present
        if MODELS[key].get("needs_punct") and downloaded:
            downloaded = downloaded and is_punct_model_downloaded()
        status[key] = downloaded
    return status


def _split_audio_chunks(audio_data: bytes, max_seconds: int = 10, sample_rate: int = 16000) -> list:
    """Split WAV audio into chunks at silence boundaries.

    Reads the WAV header to get raw PCM, then splits into chunks of
    approximately max_seconds, preferring to cut at quiet points.

    Returns a list of WAV-formatted byte strings.
    """
    import struct
    import wave
    import io

    # Read WAV to get raw PCM samples
    with wave.open(io.BytesIO(audio_data), "rb") as w:
        n_channels = w.getnchannels()
        sampwidth = w.getsampwidth()
        rate = w.getframerate()
        n_frames = w.getnframes()
        raw = w.readframes(n_frames)

    if n_frames == 0:
        return [audio_data]

    # Convert to 16-bit samples
    samples = struct.unpack(f"<{n_frames * n_channels}h", raw)
    total_samples = len(samples)
    samples_per_chunk = max_seconds * rate

    # If audio is short enough, return as-is
    if total_samples <= int(samples_per_chunk * 1.3):
        return [audio_data]

    chunks = []
    pos = 0

    while pos < total_samples:
        end = min(pos + samples_per_chunk, total_samples)

        if end < total_samples:
            # Look for a quiet point in the last 30% of the chunk to split at
            search_start = pos + int(samples_per_chunk * 0.7)
            search_end = min(end + int(rate * 2), total_samples)  # extend up to 2s past target

            # Find the quietest 160ms window (10 frames at 16kHz)
            window = int(rate * 0.16)
            best_pos = end
            best_energy = float("inf")

            for i in range(search_start, search_end - window, window // 2):
                segment = samples[i : i + window]
                energy = sum(abs(s) for s in segment) / len(segment)
                if energy < best_energy:
                    best_energy = energy
                    best_pos = i + window // 2

            end = best_pos

        # Write chunk as WAV
        chunk_samples = samples[pos:end]
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(n_channels)
            w.setsampwidth(sampwidth)
            w.setframerate(rate)
            w.writeframes(struct.pack(f"<{len(chunk_samples)}h", *chunk_samples))
        chunks.append(buf.getvalue())
        pos = end

    logger.info("Split audio into %d chunks (total %d samples)", len(chunks), total_samples)
    return chunks


def _extract_result_text(result) -> str:
    """Extract text string from model result object."""
    if hasattr(result, "text"):
        return result.text.strip()
    elif isinstance(result, str):
        return result.strip()
    else:
        return str(result).strip()


_punct_model = None
_punct_tokenizer = None

PUNCT_MODEL_ID = "mlx-community/Qwen2.5-0.5B-Instruct-4bit"


def _get_punct_model():
    """Lazy-load the punctuation correction LLM (cached across calls)."""
    global _punct_model, _punct_tokenizer
    if _punct_model is None:
        logger.info("Loading punctuation model (%s)...", PUNCT_MODEL_ID)
        from mlx_lm import load
        _punct_model, _punct_tokenizer = load(PUNCT_MODEL_ID)
        logger.info("Punctuation model loaded")
    return _punct_model, _punct_tokenizer


def _restore_punctuation(text: str) -> str:
    """Restore punctuation and capitalization using a small local LLM.

    Uses Qwen2.5-0.5B (4-bit, ~400 MB) via MLX to rewrite unpunctuated
    speech transcripts with proper punctuation, capitalization, and
    sentence boundaries.  Typically completes in ~0.2-0.3s on Apple Silicon.
    """
    import re

    if not text or not text.strip():
        return text

    try:
        model, tokenizer = _get_punct_model()
        from mlx_lm import generate

        system_prompt = (
            "Add correct punctuation and capitalization to the user's speech transcript. "
            "Fix all capitalization errors, including the word 'I'. "
            "Do not modify, add, or remove any words. Preserve all repetitions and filler words exactly as they appear."
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "hello how are you doing today i hope you are well"},
            {"role": "assistant", "content": "Hello, how are you doing today? I hope you are well."},
            {"role": "user", "content": "i went to the store and i bought some apples then i drove back to new york"},
            {"role": "assistant", "content": "I went to the store and I bought some apples. Then I drove back to New York."},
            {"role": "user", "content": "um um the the dog is barking"},
            {"role": "assistant", "content": "Um, um, the the dog is barking."},
            {"role": "user", "content": text}
        ]
        formatted = tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True,
        )

        # max_tokens slightly above input length to allow for added punctuation
        max_tokens = int(len(text.split()) * 1.5) + 20
        result = generate(
            model, tokenizer, prompt=formatted, max_tokens=max_tokens,
        )

        # Take first line, strip quotes
        clean = result.strip().split("\n")[0].strip('"').strip()

        # Sanity check: result should be roughly the same length as input.
        # If the model hallucinated or failed, fall back to basic fixes.
        if not clean or len(clean) < len(text) * 0.5 or len(clean) > len(text) * 2:
            logger.warning("Punctuation model output looks wrong, using fallback")
            return _basic_capitalization(text)

        return clean

    except Exception as e:
        logger.warning("Punctuation restoration failed: %s", e)
        return _basic_capitalization(text)


def _basic_capitalization(text: str, keyterms: list = None) -> str:
    """Minimal fallback: capitalize first letter and fix 'i' pronoun."""
    import re
    if not text:
        return text
    result = text[0].upper() + text[1:] if len(text) > 1 else text.upper()
    result = re.sub(r"\bi'", "I'", result)
    result = re.sub(r"\bi\b", "I", result)
    result = re.sub(
        r"([.!?])\s+([a-z])",
        lambda m: m.group(1) + " " + m.group(2).upper(),
        result,
    )

    if keyterms:
        # Apply user-defined keyterms enforcing their exact casing
        for term in sorted(keyterms, key=len, reverse=True):
            if not term.strip():
                continue
            # Use word boundaries for safe replacement
            prefix = r'\b' if term[0].isalnum() else ''
            suffix = r'\b' if term[-1].isalnum() else ''
            pattern = re.compile(rf"{prefix}{re.escape(term)}{suffix}", re.IGNORECASE)
            result = pattern.sub(term, result)

    return result


def transcribe_local(audio_data: bytes, language: str = "", model_key: str = None, fast_mode: bool = False, keyterms: list = None) -> str:
    """Transcribe audio using a local on-device model.

    For Granite, long audio is split into ~7s chunks and transcribed
    separately to get proper punctuation on each segment, followed by
    post-processing to fix capitalization.

    Args:
        audio_data: WAV-formatted audio bytes (16kHz, mono, 16-bit).
        language: Optional ISO language code.
        model_key: Which model to use ("granite" or "parakeet").
        fast_mode: If True, bypass the LLM-based punctuation restoration.
        keyterms: List of specific words/phrases to capitalize exactly as provided.

    Returns:
        Transcribed text string.
    """
    if not _MLX_AVAILABLE:
        raise RuntimeError("mlx-audio is not installed. Install with: pip install mlx-audio")

    model_id = _get_model_id(model_key)
    if not is_model_downloaded(model_key):
        raise RuntimeError("Model not downloaded \u2014 open Settings to download")

    key = model_key or DEFAULT_MODEL
    tmp_paths = []

    try:
        logger.info("Local transcription starting (model=%s, audio=%d bytes)", model_id, len(audio_data))

        from mlx_audio.stt import load_model
        model = load_model(model_id)

        chunks = [audio_data]

        segments = []
        for i, chunk in enumerate(chunks):
            # Write chunk to temp file
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp.write(chunk)
                tmp_paths.append(tmp.name)

            if key == "granite":
                result = model.generate(
                    tmp_paths[-1]
                )
            else:
                # Parakeet and other CTC/TDT models
                result = model.generate(tmp_paths[-1])

            text = _extract_result_text(result)
            if text:
                logger.info("Chunk %d/%d: %s", i + 1, len(chunks), text[:60])
                segments.append(text)

        full_text = " ".join(segments)

        # Restore punctuation for models that don't produce it natively
        if MODELS.get(key, {}).get("needs_punct"):
            if fast_mode:
                full_text = _basic_capitalization(full_text, keyterms=keyterms)
            else:
                full_text = _restore_punctuation(full_text)

        logger.info("Local transcription complete (%d chars): %s", len(full_text), full_text[:80])
        return full_text

    finally:
        for p in tmp_paths:
            if os.path.exists(p):
                os.unlink(p)
