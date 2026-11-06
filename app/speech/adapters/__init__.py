"""Speech adapters for hardware I/O, local neural engines, and mocks."""

from app.speech.adapters.kokoro_tts import KokoroTTS
from app.speech.adapters.mock import (
    MockAudioPlayer,
    MockAudioRecorder,
    MockSpeechToText,
    MockTextToSpeech,
)
from app.speech.adapters.null import (
    NullAudioPlayer,
    NullTextToSpeech,
)
from app.speech.adapters.sounddevice_io import (
    SoundDevicePlayer,
    SoundDeviceRecorder,
)
from app.speech.adapters.whisper_cpp import WhisperCppSTT

__all__ = [
    "MockAudioRecorder",
    "MockSpeechToText",
    "MockTextToSpeech",
    "MockAudioPlayer",
    "NullAudioPlayer",
    "NullTextToSpeech",
    "SoundDeviceRecorder",
    "SoundDevicePlayer",
    "WhisperCppSTT",
    "KokoroTTS",
]
