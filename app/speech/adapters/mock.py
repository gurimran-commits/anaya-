"""Deterministic in-memory mock adapters for testing speech components without hardware."""

from __future__ import annotations

from typing import Sequence

from app.speech.models import (
    AudioData,
    AudioFormat,
    AudioValidationError,
)


class MockAudioRecorder:
    """Mock audio recorder producing deterministic synthetic audio buffers."""

    def __init__(
        self,
        next_audio: AudioData | None = None,
        fail_with: Exception | None = None,
        sample_rate: int = 16000,
        channels: int = 1,
    ) -> None:
        self.next_audio: AudioData | None = next_audio
        self.fail_with: Exception | None = fail_with
        self.sample_rate: int = sample_rate
        self.channels: int = channels
        self.record_calls: list[float | None] = []

    @property
    def call_count(self) -> int:
        return len(self.record_calls)

    def record(self, duration: float | None = None) -> AudioData:
        """Record synthetic audio or raise configured simulated exception."""
        if self.fail_with is not None:
            raise self.fail_with

        self.record_calls.append(duration)

        if self.next_audio is not None:
            return self.next_audio

        sec = duration if duration is not None and duration > 0 else 1.0
        # Generate clean synthetic 16-bit PCM silence/tone buffer
        frame_count = int(self.sample_rate * sec)
        data = b"\x00\x00" * frame_count * self.channels
        fmt = AudioFormat(
            sample_rate=self.sample_rate,
            channels=self.channels,
            sample_width=2,
            encoding="pcm_s16le",
        )
        return AudioData(data=data, format=fmt, duration=sec)


class MockSpeechToText:
    """Mock speech-to-text transcriber returning deterministic strings."""

    def __init__(
        self,
        next_transcript: str = "help",
        fail_with: Exception | None = None,
        transcript_sequence: Sequence[str] | None = None,
    ) -> None:
        self.next_transcript: str = next_transcript
        self.fail_with: Exception | None = fail_with
        self._transcript_queue: list[str] = list(transcript_sequence) if transcript_sequence else []
        self.transcribe_calls: list[AudioData] = []

    @property
    def call_count(self) -> int:
        return len(self.transcribe_calls)

    def transcribe(self, audio: AudioData) -> str:
        """Transcribe audio or raise configured simulated exception."""
        if not isinstance(audio, AudioData):
            raise AudioValidationError(f"Expected AudioData, got {type(audio).__name__}")

        if self.fail_with is not None:
            raise self.fail_with

        self.transcribe_calls.append(audio)

        if self._transcript_queue:
            return self._transcript_queue.pop(0)

        return self.next_transcript


class MockTextToSpeech:
    """Mock text-to-speech synthesizer generating deterministic synthetic audio."""

    def __init__(
        self,
        next_audio: AudioData | None = None,
        fail_with: Exception | None = None,
        sample_rate: int = 24000,
    ) -> None:
        self.next_audio: AudioData | None = next_audio
        self.fail_with: Exception | None = fail_with
        self.sample_rate: int = sample_rate
        self.synthesize_calls: list[str] = []

    @property
    def call_count(self) -> int:
        return len(self.synthesize_calls)

    def synthesize(self, text: str) -> AudioData:
        """Synthesize audio from text or raise configured simulated exception."""
        if not isinstance(text, str) or not text.strip():
            raise AudioValidationError("Cannot synthesize empty text")

        if self.fail_with is not None:
            raise self.fail_with

        self.synthesize_calls.append(text)

        if self.next_audio is not None:
            return self.next_audio

        # Produce 0.5s of synthetic 24kHz audio
        frame_count = int(self.sample_rate * 0.5)
        data = b"\x00\x00" * frame_count
        fmt = AudioFormat(
            sample_rate=self.sample_rate,
            channels=1,
            sample_width=2,
            encoding="pcm_s16le",
        )
        return AudioData(data=data, format=fmt, duration=0.5)


class MockAudioPlayer:
    """Mock audio player tracking played audio buffers."""

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.fail_with: Exception | None = fail_with
        self.play_calls: list[AudioData] = []

    @property
    def call_count(self) -> int:
        return len(self.play_calls)

    def play(self, audio: AudioData) -> None:
        """Track played audio or raise configured simulated exception."""
        if not isinstance(audio, AudioData):
            raise AudioValidationError(f"Expected AudioData, got {type(audio).__name__}")

        if self.fail_with is not None:
            raise self.fail_with

        self.play_calls.append(audio)
