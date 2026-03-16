"""Local speech-to-text using IBM Granite 4.0 1B Speech via mlx-audio."""

import io
import logging
import os
import tempfile
import wave

logger = logging.getLogger("scriber")

MODEL_ID = "ibm-granite/granite-4.0-1b-speech"

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
    # Strategy 1: certifi.where()
    try:
        import certifi
        path = certifi.where()
        if os.path.isfile(path):
            logger.debug("CA certs found via certifi.where(): %s", path)
            return path
        logger.debug("certifi.where() returned non-existent path: %s", path)

        # Strategy 2: look relative to certifi's actual __init__ location
        certifi_dir = os.path.dirname(os.path.abspath(certifi.__file__))
        fallback = os.path.join(certifi_dir, "cacert.pem")
        if os.path.isfile(fallback):
            logger.debug("CA certs found next to certifi module: %s", fallback)
            return fallback
        logger.debug("cacert.pem not found at: %s", fallback)
    except ImportError:
        logger.debug("certifi not importable")

    # Strategy 3: macOS system CA bundle (always present)
    macos_certs = "/etc/ssl/cert.pem"
    if os.path.isfile(macos_certs):
        logger.debug("Using macOS system CA certs: %s", macos_certs)
        return macos_certs

    logger.warning("No CA certificate bundle found!")
    return None


# Fix SSL for py2app: the bundled Python can't find system CA certs.
# We must patch ssl.create_default_context before httpx/huggingface_hub use it.
_CA_CERT_PATH = _find_ca_certs()
if _CA_CERT_PATH:
    import ssl
    _orig_create_default_context = ssl.create_default_context

    def _patched_create_default_context(purpose=ssl.Purpose.SERVER_AUTH, *, cafile=None, capath=None, cadata=None):
        if cafile is None and capath is None and cadata is None:
            cafile = _CA_CERT_PATH
        # Validate the cafile exists — py2app's OpenSSL may pass a
        # compiled-in default path that doesn't exist.
        if cafile and not os.path.isfile(cafile):
            logger.debug("SSL cafile %r not found, using %s", cafile, _CA_CERT_PATH)
            cafile = _CA_CERT_PATH
        # Build the SSL context manually instead of calling the original
        # create_default_context, because the py2app-bundled OpenSSL
        # references a non-existent default cert path.
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


def is_mlx_available() -> bool:
    """Check if mlx-audio is installed and importable."""
    return _MLX_AVAILABLE


def is_model_downloaded() -> bool:
    """Check if the Granite speech model is cached locally."""
    if not _MLX_AVAILABLE:
        return False
    try:
        from huggingface_hub import try_to_load_from_cache, scan_cache_dir
        # Check if key model files exist in cache
        cache_info = scan_cache_dir()
        for repo in cache_info.repos:
            if repo.repo_id == MODEL_ID:
                # Model directory exists in cache
                return True
        return False
    except Exception as e:
        logger.debug("Error checking model cache: %s", e)
        return False


def download_model(on_progress=None, on_complete=None, on_error=None):
    """Download the Granite speech model in a background thread.

    Args:
        on_progress: Optional callback(message: str) for status updates.
        on_complete: Optional callback() when download finishes.
        on_error: Optional callback(error_msg: str) on failure.
    """
    import threading

    def _download():
        try:
            if on_progress:
                on_progress("Downloading model…")
            # Disable hf-xet transport — its binary isn't available in py2app bundles
            os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "0"
            os.environ.setdefault("HF_HOME", os.path.expanduser("~/.cache/huggingface"))
            from huggingface_hub import snapshot_download
            snapshot_download(
                MODEL_ID,
                repo_type="model",
            )
            logger.info("Model download complete: %s", MODEL_ID)
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


def _capitalize_sentences(text: str) -> str:
    """Capitalize the first letter of each sentence."""
    import re
    # Capitalize after sentence-ending punctuation followed by space
    text = re.sub(r'(^|[.!?]\s+)([a-z])', lambda m: m.group(1) + m.group(2).upper(), text)
    # Capitalize "i" as standalone word
    text = re.sub(r'\bi\b', 'I', text)
    return text


def transcribe_local(audio_data: bytes, language: str = "") -> str:
    """Transcribe audio using the local Granite model.

    Args:
        audio_data: WAV-formatted audio bytes (16kHz, mono, 16-bit).
        language: Optional ISO language code (not used by Granite currently).

    Returns:
        Transcribed text string.

    Raises:
        RuntimeError: If mlx-audio is not available or model is not downloaded.
    """
    if not _MLX_AVAILABLE:
        raise RuntimeError("mlx-audio is not installed. Install with: pip install mlx-audio")

    if not is_model_downloaded():
        raise RuntimeError("Model not downloaded — open Settings to download")

    # Write WAV bytes to a temporary file (mlx-audio expects a file path)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(audio_data)
            tmp_path = tmp.name

        logger.info("Local transcription starting (audio: %d bytes)", len(audio_data))

        from mlx_audio.stt.generate import generate_transcription

        result = generate_transcription(
            model=MODEL_ID,
            audio=tmp_path,
            prompt="Transcribe the speech into a properly written format with correct punctuation and capitalization.",
        )

        # Result format varies — handle STTOutput, string, or dict
        if hasattr(result, "text"):
            # STTOutput object from mlx-audio
            text = result.text.strip()
        elif isinstance(result, str):
            text = result.strip()
        elif isinstance(result, dict):
            text = result.get("text", "").strip()
        elif isinstance(result, list):
            # Some models return list of segments
            text = " ".join(
                seg.get("text", "") if isinstance(seg, dict) else str(seg)
                for seg in result
            ).strip()
        else:
            text = str(result).strip()

        # Post-process: capitalize first letter of each sentence
        text = _capitalize_sentences(text)

        logger.info("Local transcription complete (%d chars): %s", len(text), text[:80])
        return text

    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
