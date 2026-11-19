"""Text-to-speech for breakdown segments, via the ElevenLabs API.

One request per segment, so each clip of speech can be placed exactly where
its freeze-frame starts. Audio is decoded to mono 16-bit PCM with ffmpeg; the
renderer mixes those buffers into one track.

Without ``ELEVENLABS_API_KEY`` nothing is synthesised and the renderer holds
each freeze for an estimated reading time, with the text as captions.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass

import httpx
import numpy as np

from app.config import settings

logger = logging.getLogger(__name__)

SAMPLE_RATE = 44100
ELEVENLABS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
# Average conversational pace. Used for caption-only freezes.
WORDS_PER_SECOND = 2.6


@dataclass
class Speech:
    """One segment's audio. ``pcm`` is None when no voice was synthesised."""

    pcm: np.ndarray | None
    duration_sec: float


def estimate_duration(text: str) -> float:
    return max(len(text.split()) / WORDS_PER_SECOND, 1.5)


def decode_to_pcm(encoded: bytes) -> np.ndarray:
    """Decode any ffmpeg-readable audio to mono int16 at ``SAMPLE_RATE``."""
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            "pipe:0",
            "-f",
            "s16le",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "pipe:1",
        ],
        input=encoded,
        capture_output=True,
        check=True,
    )
    return np.frombuffer(result.stdout, dtype=np.int16)


def _synthesise(client: httpx.Client, text: str) -> bytes:
    response = client.post(
        ELEVENLABS_URL.format(voice_id=settings.elevenlabs_voice_id),
        params={"output_format": "mp3_44100_128"},
        headers={"xi-api-key": settings.elevenlabs_api_key or ""},
        json={
            "text": text,
            "model_id": settings.elevenlabs_model_id,
            # Lower stability = more expressive delivery, which suits a pundit
            # far better than the flat default.
            "voice_settings": {"stability": 0.4, "similarity_boost": 0.8, "style": 0.35},
        },
    )
    response.raise_for_status()
    return response.content


def speak(texts: list[str]) -> tuple[list[Speech], str, str | None]:
    """Synthesise each text. Returns ``(speech, voice_source, error)``.

    A failure part-way through falls back to captions for *every* segment, so
    a breakdown never has a voice that cuts out half-way.
    """
    if not settings.elevenlabs_api_key:
        return (
            [Speech(None, estimate_duration(t)) for t in texts],
            "none",
            "ELEVENLABS_API_KEY not set",
        )
    try:
        out = []
        with httpx.Client(timeout=60.0) as client:
            for text in texts:
                pcm = decode_to_pcm(_synthesise(client, text))
                out.append(Speech(pcm, len(pcm) / SAMPLE_RATE))
        return out, f"elevenlabs:{settings.elevenlabs_voice_id}", None
    except Exception as exc:  # noqa: BLE001 - degrade to captions, never fail the render
        logger.exception("Voice-over synthesis failed")
        return (
            [Speech(None, estimate_duration(t)) for t in texts],
            "none",
            f"{type(exc).__name__}: {exc}"[:300],
        )
