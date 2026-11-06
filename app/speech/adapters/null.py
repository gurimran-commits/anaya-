"""Null/no-op speech adapters for operating when audio I/O or synthesis is unavailable."""

from __future__ import annotations

import logging

from app.speech.models import (
    AudioData,
    AudioValidationError,
    SpeechBackendError,
    SpeechDeviceError,
)

logger = logging.getLogger(__name__)


class NullTextToSpeech:
    """No-op text-to-speech adapter that safely indicates TTS is unavailable."""

    def __init__(self, reason: str = "Text-to-speech is unavailable or not configured.") -> None:
        self.reason = reason

    def synthesize(self, text: str) -> AudioData:
        """Synthesize request safely raises SpeechBackendError caught by VoicePipeline."""
        if not isinstance(text, str) or not text.strip():
            raise AudioValidationError("Cannot synthesize empty or whitespace text")
        raise SpeechBackendError(self.reason)


class NullAudioPlayer:
    """No-op audio player that safely indicates audio output is unavailable."""

    def __init__(self, reason: str = "Audio player is unavailable or not configured.") -> None:
        self.reason = reason

    def play(self, audio: AudioData) -> None:
        """Playback request safely raises SpeechDeviceError caught by VoicePipeline."""
        if not isinstance(audio, AudioData):
            raise AudioValidationError(f"Expected AudioData instance, got {type(audio).__name__}")
        raise SpeechDeviceError(self.reason)
