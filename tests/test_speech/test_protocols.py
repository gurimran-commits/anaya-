"""Tests for speech protocol definitions and runtime compliance."""

import pytest

from app.speech.models import AudioData, AudioFormat
from app.speech.protocols import (
    AudioPlayer,
    AudioRecorder,
    SpeechToText,
    TextToSpeech,
)


class DummyRecorder:
    def record(self, duration: float | None = None) -> AudioData:
        return AudioData(b"\x00\x00" * 100, AudioFormat())


class DummySTT:
    def transcribe(self, audio: AudioData) -> str:
        return "dummy text"


class DummyTTS:
    def synthesize(self, text: str) -> AudioData:
        return AudioData(b"\x00\x00" * 100, AudioFormat())


class DummyPlayer:
    def play(self, audio: AudioData) -> None:
        pass


class NonCompliantRecorder:
    # Missing record method
    pass


class TestProtocols:
    """Test suite verifying protocol contracts and runtime checks."""

    def test_audio_recorder_protocol_compliance(self):
        recorder = DummyRecorder()
        assert isinstance(recorder, AudioRecorder)

        non_compliant = NonCompliantRecorder()
        assert not isinstance(non_compliant, AudioRecorder)

    def test_speech_to_text_protocol_compliance(self):
        stt = DummySTT()
        assert isinstance(stt, SpeechToText)

    def test_text_to_speech_protocol_compliance(self):
        tts = DummyTTS()
        assert isinstance(tts, TextToSpeech)

    def test_audio_player_protocol_compliance(self):
        player = DummyPlayer()
        assert isinstance(player, AudioPlayer)
