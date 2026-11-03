"""Optional physical audio input and playback adapters using sounddevice."""

from __future__ import annotations

from typing import Any

from app.speech.models import (
    AudioData,
    AudioFormat,
    AudioValidationError,
    SpeechBackendError,
    SpeechDeviceError,
)


def _get_sounddevice() -> Any:
    """Lazily import and return the sounddevice module, raising SpeechBackendError if missing."""
    try:
        import sounddevice as sd  # type: ignore
        return sd
    except ImportError as exc:
        raise SpeechBackendError(
            "The 'sounddevice' package is not installed. Install optional audio dependencies "
            "to enable physical microphone recording and speaker playback."
        ) from exc


def _get_numpy() -> Any:
    """Lazily import and return numpy, raising SpeechBackendError if missing."""
    try:
        import numpy as np
        return np
    except ImportError as exc:
        raise SpeechBackendError("The 'numpy' package is required for sounddevice audio processing.") from exc


class SoundDeviceRecorder:
    """Captures microphone audio using sounddevice with push-to-talk duration bounds."""

    def __init__(
        self,
        sample_rate: int = 16000,
        channels: int = 1,
        max_duration: float = 15.0,
        device: int | str | None = None,
    ) -> None:
        if sample_rate < 4000 or sample_rate > 192000:
            raise AudioValidationError(f"Invalid sample rate: {sample_rate}")
        if channels not in (1, 2):
            raise AudioValidationError(f"Invalid channel count: {channels}")
        if max_duration <= 0 or max_duration > 60.0:
            raise AudioValidationError(f"max_duration must be between 0 and 60.0s, got {max_duration}")

        self.sample_rate = sample_rate
        self.channels = channels
        self.max_duration = max_duration
        self.device = device

    def record(self, duration: float | None = None) -> AudioData:
        """Record audio explicitly from the microphone up to duration or max_duration.

        Enforces bounded duration to prevent unbounded buffering.
        """
        sd = _get_sounddevice()
        np = _get_numpy()

        effective_duration = duration if duration is not None and duration > 0 else self.max_duration
        if effective_duration > self.max_duration:
            effective_duration = self.max_duration

        frames_to_record = int(self.sample_rate * effective_duration)
        if frames_to_record <= 0:
            raise AudioValidationError("Calculated recording frames must be greater than zero")

        try:
            # Explicit, bounded recording into an in-memory int16 buffer
            audio_array = sd.rec(
                frames_to_record,
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="int16",
                device=self.device,
            )
            sd.wait()
        except Exception as exc:
            sd.stop()
            raise SpeechDeviceError(f"Audio recording failed on device '{self.device}': {exc}") from exc
        finally:
            try:
                sd.stop()
            except Exception:
                pass

        raw_bytes = bytes(audio_array.tobytes())
        fmt = AudioFormat(
            sample_rate=self.sample_rate,
            channels=self.channels,
            sample_width=2,
            encoding="pcm_s16le",
        )
        return AudioData(data=raw_bytes, format=fmt, duration=effective_duration)


class SoundDevicePlayer:
    """Plays audio buffers to speakers using sounddevice."""

    def __init__(self, device: int | str | None = None) -> None:
        self.device = device

    def play(self, audio: AudioData) -> None:
        """Play an AudioData instance through the configured sound device."""
        if not isinstance(audio, AudioData):
            raise AudioValidationError(f"Expected AudioData instance, got {type(audio).__name__}")

        sd = _get_sounddevice()
        np = _get_numpy()

        # If WAV formatted, convert to PCM
        if audio.format.encoding == "wav":
            pcm_audio = AudioData.from_wav_bytes(audio.data)
        elif audio.format.is_pcm:
            pcm_audio = audio
        else:
            raise AudioValidationError(f"Unsupported audio encoding for playback: {audio.format.encoding}")

        dtype_map = {1: np.int8, 2: np.int16, 4: np.int32}
        np_dtype = dtype_map.get(pcm_audio.format.sample_width)
        if np_dtype is None:
            raise AudioValidationError(f"Unsupported sample width: {pcm_audio.format.sample_width}")

        try:
            audio_array = np.frombuffer(pcm_audio.data, dtype=np_dtype)
            if pcm_audio.format.channels > 1:
                audio_array = audio_array.reshape(-1, pcm_audio.format.channels)

            sd.play(audio_array, samplerate=pcm_audio.format.sample_rate, device=self.device)
            sd.wait()
        except Exception as exc:
            sd.stop()
            raise SpeechDeviceError(f"Audio playback failed on device '{self.device}': {exc}") from exc
        finally:
            try:
                sd.stop()
            except Exception:
                pass
