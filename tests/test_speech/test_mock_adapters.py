"""Tests for deterministic in-memory mock adapters."""

import pytest

from app.speech.adapters.mock import (
    MockAudioPlayer,
    MockAudioRecorder,
    MockSpeechToText,
    MockTextToSpeech,
)
from app.speech.models import (
    AudioData,
    AudioFormat,
    AudioValidationError,
    SpeechBackendError,
    SpeechDeviceError,
)
from app.speech.protocols import (
    AudioPlayer,
    AudioRecorder,
    SpeechToText,
    TextToSpeech,
)


class TestMockAdapters:
    """Test suite for MockAudioRecorder, MockSpeechToText, MockTextToSpeech, and MockAudioPlayer."""

    def test_protocol_conformance(self):
        recorder = MockAudioRecorder()
        stt = MockSpeechToText()
        tts = MockTextToSpeech()
        player = MockAudioPlayer()

        assert isinstance(recorder, AudioRecorder)
        assert isinstance(stt, SpeechToText)
        assert isinstance(tts, TextToSpeech)
        assert isinstance(player, AudioPlayer)

    def test_mock_audio_recorder_defaults_and_tracking(self):
        recorder = MockAudioRecorder(sample_rate=16000, channels=1)
        assert recorder.call_count == 0

        audio = recorder.record(duration=2.0)
        assert recorder.call_count == 1
        assert recorder.record_calls == [2.0]
        assert audio.format.sample_rate == 16000
        assert audio.format.channels == 1
        assert audio.duration == pytest.approx(2.0, rel=1e-3)

    def test_mock_audio_recorder_custom_audio(self):
        custom_fmt = AudioFormat(sample_rate=24000, channels=1, sample_width=2)
        custom_audio = AudioData(b"\x12\x34" * 100, custom_fmt)
        recorder = MockAudioRecorder(next_audio=custom_audio)

        result = recorder.record()
        assert result == custom_audio

    def test_mock_audio_recorder_failure_simulation(self):
        recorder = MockAudioRecorder(fail_with=SpeechDeviceError("Mic unplugged"))
        with pytest.raises(SpeechDeviceError, match="Mic unplugged"):
            recorder.record()

    def test_mock_speech_to_text_tracking_and_queue(self):
        stt = MockSpeechToText(transcript_sequence=["first line", "second line"])
        fmt = AudioFormat()
        audio = AudioData(b"\x00\x00" * 100, fmt)

        assert stt.transcribe(audio) == "first line"
        assert stt.transcribe(audio) == "second line"
        # When queue is empty, returns next_transcript
        assert stt.transcribe(audio) == "help"
        assert stt.call_count == 3

    def test_mock_speech_to_text_validation_and_failure(self):
        stt = MockSpeechToText(fail_with=SpeechBackendError("STT crash"))
        fmt = AudioFormat()
        audio = AudioData(b"\x00\x00" * 100, fmt)

        with pytest.raises(SpeechBackendError, match="STT crash"):
            stt.transcribe(audio)

        with pytest.raises(AudioValidationError):
            stt.transcribe("invalid_audio")  # type: ignore

    def test_mock_text_to_speech_tracking_and_synthesis(self):
        tts = MockTextToSpeech()
        audio = tts.synthesize("Hello world")

        assert tts.call_count == 1
        assert tts.synthesize_calls == ["Hello world"]
        assert audio.format.sample_rate == 24000
        assert audio.duration == pytest.approx(0.5, rel=1e-3)

    def test_mock_text_to_speech_validation_and_failure(self):
        tts = MockTextToSpeech()
        with pytest.raises(AudioValidationError, match="Cannot synthesize empty text"):
            tts.synthesize("")

        with pytest.raises(AudioValidationError, match="Cannot synthesize empty text"):
            tts.synthesize("   ")

        tts_fail = MockTextToSpeech(fail_with=SpeechBackendError("Model load error"))
        with pytest.raises(SpeechBackendError, match="Model load error"):
            tts_fail.synthesize("test")

    def test_mock_audio_player_tracking_and_failure(self):
        player = MockAudioPlayer()
        fmt = AudioFormat()
        audio = AudioData(b"\x00\x00" * 100, fmt)

        player.play(audio)
        assert player.call_count == 1
        assert player.play_calls == [audio]

        player_fail = MockAudioPlayer(fail_with=SpeechDeviceError("Speaker disconnected"))
        with pytest.raises(SpeechDeviceError, match="Speaker disconnected"):
            player_fail.play(audio)

        with pytest.raises(AudioValidationError):
            player.play("invalid_audio")  # type: ignore
