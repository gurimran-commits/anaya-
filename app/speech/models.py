"""Speech and audio data models, configuration, and exception definitions."""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.action_result import ActionResult


# ============================================================================
# Exception Hierarchy
# ============================================================================

class SpeechError(Exception):
    """Base exception for all speech and audio errors."""
    pass


class AudioValidationError(SpeechError):
    """Raised when audio data or parameters fail validation."""
    pass


class SpeechDeviceError(SpeechError):
    """Raised when an audio input or output device fails or is unavailable."""
    pass


class SpeechBackendError(SpeechError):
    """Raised when an STT or TTS engine encounters an error or is unconfigured."""
    pass


class SpeechTimeoutError(SpeechError):
    """Raised when an audio recording or transcription operation times out."""
    pass


# ============================================================================
# Audio Format and Audio Data
# ============================================================================

@dataclass(frozen=True)
class AudioFormat:
    """Metadata describing the representation of raw or encoded audio data.

    Attributes:
        sample_rate: Sampling rate in Hz (e.g. 16000 for speech, 24000 for Kokoro).
        channels: Channel count (1 for mono, 2 for stereo).
        sample_width: Number of bytes per sample (e.g. 2 for 16-bit PCM).
        encoding: Encoding format identifier (e.g. 'pcm_s16le', 'pcm_f32le', 'wav').
    """

    sample_rate: int = 16000
    channels: int = 1
    sample_width: int = 2
    encoding: str = "pcm_s16le"

    def __post_init__(self) -> None:
        if not isinstance(self.sample_rate, int) or self.sample_rate < 4000 or self.sample_rate > 192000:
            raise AudioValidationError(
                f"Sample rate must be an integer between 4000 and 192000 Hz, got {self.sample_rate}"
            )
        if self.channels not in (1, 2):
            raise AudioValidationError(f"Channels must be 1 (mono) or 2 (stereo), got {self.channels}")
        if self.sample_width not in (1, 2, 4):
            raise AudioValidationError(
                f"Sample width must be 1, 2, or 4 bytes, got {self.sample_width}"
            )
        if not isinstance(self.encoding, str) or not self.encoding.strip():
            raise AudioValidationError("Audio encoding identifier cannot be empty")

    @property
    def frame_size(self) -> int:
        """Size of one audio frame across all channels in bytes."""
        return self.channels * self.sample_width

    @property
    def is_pcm(self) -> bool:
        """True if the encoding represents uncompressed linear PCM."""
        return self.encoding.startswith("pcm_")


class AudioData:
    """Encapsulated, immutable audio buffer with associated format metadata.

    The payload is stored immutably as bytes to prevent unintended modification.
    """

    def __init__(
        self,
        data: bytes | bytearray,
        format: AudioFormat,
        duration: float | None = None,
    ) -> None:
        if not isinstance(data, (bytes, bytearray)):
            raise AudioValidationError(f"Audio payload must be bytes or bytearray, got {type(data).__name__}")
        if len(data) == 0:
            raise AudioValidationError("Audio payload cannot be empty")
        if not isinstance(format, AudioFormat):
            raise AudioValidationError(f"format must be an AudioFormat instance, got {type(format).__name__}")

        self._data: bytes = bytes(data)
        self._format: AudioFormat = format

        if duration is not None:
            if not isinstance(duration, (int, float)) or duration <= 0:
                raise AudioValidationError(f"Audio duration must be a positive number, got {duration}")
            self._duration: float = float(duration)
        elif self._format.is_pcm:
            bytes_per_second = self._format.sample_rate * self._format.frame_size
            self._duration = len(self._data) / bytes_per_second
        elif self._format.encoding == "wav":
            try:
                with wave.open(io.BytesIO(self._data), "rb") as wf:
                    frames = wf.getnframes()
                    rate = wf.getframerate()
                    self._duration = frames / rate if rate > 0 else 0.0
            except Exception:
                self._duration = 0.0
        else:
            self._duration = 0.0

    @property
    def data(self) -> bytes:
        """The immutable audio byte buffer."""
        return self._data

    @property
    def format(self) -> AudioFormat:
        """The format metadata for this audio."""
        return self._format

    @property
    def duration(self) -> float:
        """The duration of the audio in seconds."""
        return self._duration

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return (
            f"AudioData(size={len(self._data)} bytes, "
            f"format={self._format.encoding}/{self._format.sample_rate}Hz/{self._format.channels}ch, "
            f"duration={self._duration:.3f}s)"
        )

    def to_wav_bytes(self) -> bytes:
        """Convert the audio payload to standard RIFF/WAV format bytes.

        If already WAV-encoded, returns the raw data. If PCM, generates a canonical WAV header.
        """
        if self._format.encoding == "wav":
            return self._data

        if not self._format.is_pcm:
            raise AudioValidationError(
                f"Cannot serialize encoding '{self._format.encoding}' directly to WAV without decoder"
            )

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wf:
            wf.setnchannels(self._format.channels)
            wf.setsampwidth(self._format.sample_width)
            wf.setframerate(self._format.sample_rate)
            wf.writeframes(self._data)
        return buffer.getvalue()

    @classmethod
    def from_wav_bytes(cls, wav_bytes: bytes | bytearray) -> AudioData:
        """Parse standard RIFF/WAV bytes into an AudioData instance with PCM payload."""
        if not isinstance(wav_bytes, (bytes, bytearray)) or len(wav_bytes) == 0:
            raise AudioValidationError("WAV payload cannot be empty")

        try:
            with wave.open(io.BytesIO(bytes(wav_bytes)), "rb") as wf:
                channels = wf.getnchannels()
                sample_width = wf.getsampwidth()
                sample_rate = wf.getframerate()
                n_frames = wf.getnframes()
                raw_pcm = wf.readframes(n_frames)
        except wave.Error as exc:
            raise AudioValidationError(f"Invalid WAV header or corrupted data: {exc}") from exc
        except Exception as exc:
            raise AudioValidationError(f"Failed to read WAV data: {exc}") from exc

        encoding_map = {1: "pcm_u8", 2: "pcm_s16le", 4: "pcm_s32le"}
        encoding = encoding_map.get(sample_width, f"pcm_{sample_width * 8}bit")

        audio_format = AudioFormat(
            sample_rate=sample_rate,
            channels=channels,
            sample_width=sample_width,
            encoding=encoding,
        )
        return cls(data=raw_pcm, format=audio_format)


# ============================================================================
# Speech Configuration
# ============================================================================

@dataclass(frozen=True)
class SpeechConfig:
    """Configuration settings for speech recording, transcription, and synthesis.

    Defaults align with the Mitra technical design: 16 kHz mono recording with a 15-second cap.
    """

    sample_rate: int = 16000
    channels: int = 1
    sample_width: int = 2
    max_record_seconds: float = 15.0
    push_to_talk: bool = True

    # STT configuration
    stt_backend: str = "mock"
    whisper_cpp_executable: str | None = None
    whisper_cpp_model: str | None = None
    whisper_cpp_timeout_seconds: float = 30.0

    # TTS configuration
    tts_backend: str = "mock"
    kokoro_model_path: str | None = None
    kokoro_config_path: str | None = None
    kokoro_voices_dir: str | None = None
    tts_voice: str = "af_heart"
    tts_language: str = "en"
    tts_sample_rate: int = 24000

    def __post_init__(self) -> None:
        if not isinstance(self.sample_rate, int) or self.sample_rate < 4000 or self.sample_rate > 192000:
            raise AudioValidationError(f"Invalid recording sample rate: {self.sample_rate}")
        if self.channels not in (1, 2):
            raise AudioValidationError(f"Invalid channel count: {self.channels}")
        if self.sample_width not in (1, 2, 4):
            raise AudioValidationError(f"Invalid sample width: {self.sample_width}")
        if not isinstance(self.max_record_seconds, (int, float)) or self.max_record_seconds <= 0 or self.max_record_seconds > 60.0:
            raise AudioValidationError(
                f"max_record_seconds must be between 0 and 60.0 seconds, got {self.max_record_seconds}"
            )
        if not isinstance(self.whisper_cpp_timeout_seconds, (int, float)) or self.whisper_cpp_timeout_seconds <= 0 or self.whisper_cpp_timeout_seconds > 120.0:
            raise AudioValidationError(
                f"whisper_cpp_timeout_seconds must be between 0 and 120.0 seconds, got {self.whisper_cpp_timeout_seconds}"
            )
        if not isinstance(self.tts_sample_rate, int) or self.tts_sample_rate < 4000 or self.tts_sample_rate > 192000:
            raise AudioValidationError(f"Invalid TTS sample rate: {self.tts_sample_rate}")


# ============================================================================
# Pipeline Result
# ============================================================================

@dataclass(frozen=True)
class PipelineResult:
    """The result of processing an audio input through the speech and brain pipeline.

    Attributes:
        transcript: The text transcribed by STT (None if transcription failed or was skipped).
        action_result: The ActionResult returned by the Brain TextAssistant (None if STT failed).
        spoken_response: True if the assistant's response was successfully synthesized and played.
        success: True if the overall pipeline flow succeeded.
        error: Human-readable error message if any stage failed.
    """

    transcript: str | None = None
    action_result: ActionResult | None = None
    spoken_response: bool = False
    success: bool = True
    error: str | None = None
