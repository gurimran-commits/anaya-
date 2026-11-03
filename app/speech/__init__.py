"""Speech and audio subsystem for Anaya personal voice assistant."""

from app.speech.models import (
    AudioData,
    AudioFormat,
    AudioValidationError,
    PipelineResult,
    SpeechBackendError,
    SpeechConfig,
    SpeechDeviceError,
    SpeechError,
    SpeechTimeoutError,
)
from app.speech.pipeline import VoicePipeline
from app.speech.protocols import (
    AudioPlayer,
    AudioRecorder,
    SpeechToText,
    TextToSpeech,
)

__all__ = [
    # Data models
    "AudioFormat",
    "AudioData",
    "SpeechConfig",
    "PipelineResult",
    # Exceptions
    "SpeechError",
    "AudioValidationError",
    "SpeechDeviceError",
    "SpeechBackendError",
    "SpeechTimeoutError",
    # Protocols
    "AudioRecorder",
    "SpeechToText",
    "TextToSpeech",
    "AudioPlayer",
    # Orchestrator
    "VoicePipeline",
    "create_mock_pipeline",
]


def create_mock_pipeline(
    assistant,
    transcript: str = "help",
    config: SpeechConfig | None = None,
) -> VoicePipeline:
    """Helper factory to assemble a VoicePipeline with in-memory mock adapters for testing."""
    from app.speech.adapters.mock import (
        MockAudioPlayer,
        MockAudioRecorder,
        MockSpeechToText,
        MockTextToSpeech,
    )

    recorder = MockAudioRecorder()
    stt = MockSpeechToText(next_transcript=transcript)
    tts = MockTextToSpeech()
    player = MockAudioPlayer()

    return VoicePipeline(
        recorder=recorder,
        stt=stt,
        assistant=assistant,
        tts=tts,
        player=player,
        config=config or SpeechConfig(),
    )
