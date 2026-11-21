"""Custom Whisper defaults for the personal fork."""
from __future__ import annotations

import os

DEFAULT_WHISPER_MODEL = "medium"


def resolve_whisper_model() -> str:
    """Return the configured Whisper model, defaulting to our fork's larger model."""
    return os.environ.get("VIRALCUT_WHISPER", DEFAULT_WHISPER_MODEL)
