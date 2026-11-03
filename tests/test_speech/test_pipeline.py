"""Tests for VoicePipeline orchestrator, failure modes, and security invariants."""

import pytest

from app.brain.text_assistant import TextAssistant
from app.commands.executor import CommandExecutor
from app.commands.registry import CommandRegistry, CommandSpec
from app.core.action_result import ActionResult
from app.security.approval_service import ApprovalService
from app.speech import create_mock_pipeline
from app.speech.adapters.mock import (
    MockAudioPlayer,
    MockAudioRecorder,
    MockSpeechToText,
    MockTextToSpeech,
)
from app.speech.models import (
    AudioData,
    AudioFormat,
    PipelineResult,
    SpeechBackendError,
    SpeechDeviceError,
)
from app.speech.pipeline import VoicePipeline


@pytest.fixture
def clean_assistant():
    """Fixture providing a fresh TextAssistant with test commands registered."""
    registry = CommandRegistry()
    registry.register(
        CommandSpec(
            name="system.status",
            description="Check system status",
            handler=lambda: "System operational",
            validate_arguments=lambda args: len(args) == 0,
        )
    )
    registry.register(
        CommandSpec(
            name="system.version",
            description="Check system version",
            handler=lambda: "Anaya 0.5.0",
            validate_arguments=lambda args: len(args) == 0,
        )
    )
    registry.register(
        CommandSpec(
            name="demo.delete",
            description="Test a protected action",
            handler=lambda value: f"Deleted {value}",
            requires_confirmation=True,
            validate_arguments=lambda args: len(args) == 1,
        )
    )

    approval = ApprovalService()
    executor = CommandExecutor(registry, approval_service=approval)
    return TextAssistant(registry=registry, executor=executor)


@pytest.fixture
def mock_pipeline(clean_assistant):
    """Fixture providing a fully configured VoicePipeline with mock adapters."""
    recorder = MockAudioRecorder()
    stt = MockSpeechToText(next_transcript="system.status")
    tts = MockTextToSpeech()
    player = MockAudioPlayer()

    return VoicePipeline(
        recorder=recorder,
        stt=stt,
        assistant=clean_assistant,
        tts=tts,
        player=player,
    )


class TestVoicePipeline:
    """Test suite for VoicePipeline operations, error handling, and security boundaries."""

    def test_complete_successful_flow(self, mock_pipeline):
        result = mock_pipeline.listen_and_process(duration=1.0, speak_response=True)

        assert isinstance(result, PipelineResult)
        assert result.success is True
        assert result.transcript == "system.status"
        assert result.action_result is not None
        assert "System operational" in result.action_result.message
        assert result.spoken_response is True

        # Verify adapter interactions
        assert mock_pipeline.recorder.call_count == 1
        assert mock_pipeline.stt.call_count == 1
        assert mock_pipeline.tts.call_count == 1
        assert mock_pipeline.player.call_count == 1

    def test_process_audio_without_speech_response(self, mock_pipeline):
        fmt = AudioFormat()
        audio = AudioData(b"\x00\x00" * 100, fmt)

        result = mock_pipeline.process_audio(audio, speak_response=False)
        assert result.success is True
        assert result.transcript == "system.status"
        assert result.spoken_response is False

        # TTS and player should NOT have been invoked
        assert mock_pipeline.tts.call_count == 0
        assert mock_pipeline.player.call_count == 0

    def test_recording_failure_handles_gracefully(self, clean_assistant):
        recorder = MockAudioRecorder(fail_with=SpeechDeviceError("Mic unavailable"))
        stt = MockSpeechToText()
        tts = MockTextToSpeech()
        player = MockAudioPlayer()

        pipeline = VoicePipeline(
            recorder=recorder,
            stt=stt,
            assistant=clean_assistant,
            tts=tts,
            player=player,
        )

        result = pipeline.listen_and_process()
        assert result.success is False
        assert result.action_result is None
        assert "Recording failed" in str(result.error)
        assert stt.call_count == 0

    def test_stt_failure_prevents_brain_invocation(self, clean_assistant):
        recorder = MockAudioRecorder()
        stt = MockSpeechToText(fail_with=SpeechBackendError("STT crash"))
        tts = MockTextToSpeech()
        player = MockAudioPlayer()

        pipeline = VoicePipeline(
            recorder=recorder,
            stt=stt,
            assistant=clean_assistant,
            tts=tts,
            player=player,
        )

        result = pipeline.listen_and_process()
        assert result.success is False
        assert result.action_result is None
        assert "Transcription failed" in str(result.error)
        assert clean_assistant.context.last_command is None

    def test_empty_transcription_rejected_before_brain(self, clean_assistant):
        recorder = MockAudioRecorder()
        stt = MockSpeechToText(next_transcript="   ")  # Whitespace only
        tts = MockTextToSpeech()
        player = MockAudioPlayer()

        pipeline = VoicePipeline(
            recorder=recorder,
            stt=stt,
            assistant=clean_assistant,
            tts=tts,
            player=player,
        )

        result = pipeline.listen_and_process()
        assert result.success is False
        assert result.action_result is None
        assert "empty or whitespace" in str(result.error)
        assert clean_assistant.context.last_command is None

    def test_tts_failure_does_not_undo_successful_brain_action(self, clean_assistant):
        recorder = MockAudioRecorder()
        stt = MockSpeechToText(next_transcript="system.status")
        tts = MockTextToSpeech(fail_with=SpeechBackendError("Synthesis failed"))
        player = MockAudioPlayer()

        pipeline = VoicePipeline(
            recorder=recorder,
            stt=stt,
            assistant=clean_assistant,
            tts=tts,
            player=player,
        )

        result = pipeline.listen_and_process(speak_response=True)
        # Brain operation succeeded!
        assert result.success is True
        assert result.action_result is not None
        assert result.action_result.success is True
        # Spoken response flag is False due to TTS error
        assert result.spoken_response is False

    def test_playback_failure_does_not_crash(self, clean_assistant):
        recorder = MockAudioRecorder()
        stt = MockSpeechToText(next_transcript="system.status")
        tts = MockTextToSpeech()
        player = MockAudioPlayer(fail_with=SpeechDeviceError("Audio device lost"))

        pipeline = VoicePipeline(
            recorder=recorder,
            stt=stt,
            assistant=clean_assistant,
            tts=tts,
            player=player,
        )

        result = pipeline.listen_and_process(speak_response=True)
        assert result.success is True
        assert result.action_result.success is True
        assert result.spoken_response is False

    def test_invalid_audio_input_to_process_audio(self, mock_pipeline):
        result = mock_pipeline.process_audio("not_audio_data")  # type: ignore
        assert result.success is False
        assert "Invalid audio input" in str(result.error)

    def test_brain_processing_exception_handled_safely(self, clean_assistant, monkeypatch):
        pipeline = create_mock_pipeline(clean_assistant, transcript="system.status")
        monkeypatch.setattr(
            clean_assistant,
            "process",
            lambda text: (_ for _ in ()).throw(RuntimeError("Unexpected brain crash")),
        )

        result = pipeline.listen_and_process()
        assert result.success is False
        assert "Brain processing failed" in str(result.error)

    def test_speak_helper_method(self, mock_pipeline):
        assert mock_pipeline.speak("Hello there") is True
        assert mock_pipeline.tts.call_count == 1
        assert mock_pipeline.player.call_count == 1

        # Empty text returns False
        assert mock_pipeline.speak("") is False
        assert mock_pipeline.speak("   ") is False

        # When TTS fails, speak returns False without crashing
        mock_pipeline.tts.fail_with = SpeechBackendError("TTS failure")
        assert mock_pipeline.speak("Hello") is False

    def test_security_in_band_approval_rejection_via_voice(self, clean_assistant):
        """Verify that spoken confirmations ('yes', 'confirm', 'approve') cannot authorize operations."""
        pipeline = create_mock_pipeline(clean_assistant, transcript="yes")

        result = pipeline.listen_and_process()
        assert result.success is False
        # Text confirmation is disabled, preventing in-band verbal approval of pending requests
        assert "confirmation is disabled" in result.action_result.message

    def test_helper_create_mock_pipeline(self, clean_assistant):
        pipeline = create_mock_pipeline(clean_assistant, transcript="system.version")
        result = pipeline.listen_and_process()
        assert result.success is True
        assert result.transcript == "system.version"
        assert "Anaya 0.5.0" in result.action_result.message
