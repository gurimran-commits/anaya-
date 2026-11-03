"""Protocol definitions for speech recording, transcription, synthesis, and playback."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.speech.models import AudioData


@runtime_checkable
class AudioRecorder(Protocol):
    """Protocol for capturing audio input from a microphone or synthetic source."""

    def record(self, duration: float | None = None) -> AudioData:
        """Record audio input up to the specified duration or configured limit.

        Args:
            duration: Maximum duration to record in seconds. If None, uses configured default.

        Returns:
            AudioData containing the captured audio buffer and format metadata.

        Raises:
            SpeechDeviceError: If the recording device is unavailable or errors occur.
            SpeechTimeoutError: If the recording operation times out unexpectedly.
            AudioValidationError: If invalid parameters are supplied.
        """
        ...


@runtime_checkable
class SpeechToText(Protocol):
    """Protocol for transcribing speech audio to text."""

    def transcribe(self, audio: AudioData) -> str:
        """Transcribe audio data into a text string.

        Args:
            audio: AudioData containing speech payload to transcribe.

        Returns:
            Normalized transcription string.

        Raises:
            AudioValidationError: If the audio format is unsupported or data is invalid.
            SpeechBackendError: If the STT engine fails or is misconfigured.
            SpeechTimeoutError: If the transcription process exceeds its execution timeout.
        """
        ...


@runtime_checkable
class TextToSpeech(Protocol):
    """Protocol for synthesizing speech audio from text."""

    def synthesize(self, text: str) -> AudioData:
        """Synthesize spoken audio from input text.

        Args:
            text: Plain text string to convert to speech.

        Returns:
            AudioData containing synthesized speech and accurate format metadata.

        Raises:
            AudioValidationError: If the input text is empty or invalid.
            SpeechBackendError: If the TTS engine fails or required assets are missing.
        """
        ...


@runtime_checkable
class AudioPlayer(Protocol):
    """Protocol for playing audio output to speakers or virtual sink."""

    def play(self, audio: AudioData) -> None:
        """Play audio data to the output device.

        Args:
            audio: AudioData containing waveform to play.

        Raises:
            AudioValidationError: If the audio payload is corrupted or format unsupported.
            SpeechDeviceError: If the output device is missing or playback fails.
        """
        ...
