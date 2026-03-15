"""ElevenLabs Scribe V2 batch transcription."""

import json
import logging
import os
from datetime import datetime

import requests

from .config import LOG_FILE, ensure_config_dir

logger = logging.getLogger("scriber")

API_URL = "https://api.elevenlabs.io/v1/speech-to-text"


def transcribe(
    audio_data: bytes,
    api_key: str,
    language: str = "",
    keyterms: list[str] | None = None,
) -> str:
    """Send audio to ElevenLabs Scribe V2 and return the transcript text.

    Returns the transcript exactly as the API returns it, with no modifications.
    """
    headers = {"xi-api-key": api_key}

    files = {
        "file": ("recording.wav", audio_data, "audio/wav"),
    }
    data = {
        "model_id": "scribe_v2",
        "timestamps_granularity": "none",
    }
    if language:
        data["language_code"] = language
    if keyterms:
        # API expects individual form fields for each term
        for i, term in enumerate(keyterms[:100]):
            data[f"keyterms[{i}]"] = term

    response = requests.post(API_URL, headers=headers, files=files, data=data, timeout=120)

    _log_response(response)

    response.raise_for_status()
    result = response.json()
    return result.get("text", "")


def _log_response(response: requests.Response):
    """Log API response metadata to the log file."""
    ensure_config_dir()
    try:
        body = response.json()
    except Exception:
        body = response.text

    entry = {
        "timestamp": datetime.now().isoformat(),
        "status_code": response.status_code,
        "headers": {
            k: v
            for k, v in response.headers.items()
            if k.lower().startswith(("x-", "ratelimit", "content-type"))
        },
        "body": body,
    }

    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")
