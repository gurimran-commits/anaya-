"""Focused tests for speech subsystem bootstrap, adapter discovery, and graceful degradation."""

from __future__ import annotations

import os
from pathlib import Path
import sys
from unittest.mock import MagicMock, patch

import pytest

from app.bootstrap import (
    create_controller,
    resolve_kokoro_paths,
    resolve_whisper_paths,
)
from app.speech.adapters import (
    KokoroTTS,
    NullAudioPlayer,
    NullTextToSpeech,
    SoundDevicePlayer,
    SoundDeviceRecorder,
    WhisperCppSTT,
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


@pytest.fixture
def temp_db(tmp_path: Path):
    return {
        "mem_db": tmp_path / "test_mem.db",
        "tasks_db": tmp_path / "test_tasks.db",
    }


def test_speech_adapter_imports_and_protocols():
    """Verify speech adapter classes are exported with correct names and conform to protocols."""
    assert issubclass(SoundDeviceRecorder, AudioRecorder)
    assert issubclass(SoundDevicePlayer, AudioPlayer)
    assert issubclass(WhisperCppSTT, SpeechToText)
    assert issubclass(KokoroTTS, TextToSpeech)
    assert issubclass(NullAudioPlayer, AudioPlayer)
    assert issubclass(NullTextToSpeech, TextToSpeech)

    null_tts = NullTextToSpeech("TTS unavailable")
    with pytest.raises(SpeechBackendError, match="TTS unavailable"):
        null_tts.synthesize("Hello")

    with pytest.raises(AudioValidationError, match="Cannot synthesize empty"):
        null_tts.synthesize("   ")

    null_player = NullAudioPlayer("Player unavailable")
    fmt = AudioFormat(sample_rate=16000, channels=1, sample_width=2)
    with pytest.raises(SpeechDeviceError, match="Player unavailable"):
        null_player.play(AudioData(b"\x00" * 320, fmt))

    with pytest.raises(AudioValidationError, match="Expected AudioData instance"):
        null_player.play("not-audio")  # type: ignore[arg-type]


def test_null_adapters_do_not_falsely_report_spoken_output():
    """Verify that when NullAudioPlayer or NullTextToSpeech is active, speak() returns False."""
    from app.speech.pipeline import VoicePipeline

    mock_rec = MagicMock(spec=AudioRecorder)
    mock_stt = MagicMock(spec=SpeechToText)
    mock_assistant = MagicMock()
    null_tts = NullTextToSpeech("TTS offline")
    null_player = NullAudioPlayer("Audio sink offline")

    pipeline = VoicePipeline(
        recorder=mock_rec,
        stt=mock_stt,
        assistant=mock_assistant,
        tts=null_tts,
        player=null_player,
    )
    # speak() must return False, never falsely reporting spoken output
    assert pipeline.speak("Test speech output") is False


def test_whisper_path_resolution_via_parameters(tmp_path: Path):
    """Verify explicit parameters take precedence in resolve_whisper_paths."""
    dummy_exe = tmp_path / "custom-whisper.exe"
    dummy_exe.write_bytes(b"")
    dummy_model = tmp_path / "custom-model.bin"
    dummy_model.write_bytes(b"")

    exe, model = resolve_whisper_paths(
        executable_path=dummy_exe,
        model_path=dummy_model,
    )
    assert exe == dummy_exe.resolve()
    assert model == dummy_model.resolve()


def test_whisper_path_resolution_via_env_vars(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify environment variables are honored in resolve_whisper_paths."""
    dummy_exe = tmp_path / "env-whisper.exe"
    dummy_exe.write_bytes(b"")
    dummy_model = tmp_path / "env-model.bin"
    dummy_model.write_bytes(b"")

    monkeypatch.setenv("ANAYA_WHISPER_BIN", str(dummy_exe))
    monkeypatch.setenv("ANAYA_WHISPER_MODEL", str(dummy_model))

    exe, model = resolve_whisper_paths()
    assert exe == dummy_exe.resolve()
    assert model == dummy_model.resolve()


def test_whisper_missing_executable_handling(tmp_path: Path):
    """When the executable does not exist, resolve_whisper_paths returns None for exe."""
    dummy_model = tmp_path / "model.bin"
    dummy_model.write_bytes(b"")

    nonexistent_exe = tmp_path / "missing-whisper.exe"
    exe, model = resolve_whisper_paths(
        executable_path=nonexistent_exe,
        model_path=dummy_model,
    )
    assert exe is None
    assert model == dummy_model.resolve()


def test_whisper_missing_model_handling(tmp_path: Path):
    """When the model does not exist, resolve_whisper_paths returns None for model."""
    dummy_exe = tmp_path / "whisper.exe"
    dummy_exe.write_bytes(b"")

    nonexistent_model = tmp_path / "missing-model.bin"
    exe, model = resolve_whisper_paths(
        executable_path=dummy_exe,
        model_path=nonexistent_model,
    )
    assert exe == dummy_exe.resolve()
    assert model is None


def test_whisper_path_with_spaces(tmp_path: Path):
    """Paths containing whitespace resolve and execute cleanly via safe argument vectors."""
    space_dir = tmp_path / "path with spaces"
    space_dir.mkdir()
    dummy_exe = space_dir / "whisper cli.exe"
    dummy_exe.write_bytes(b"")
    dummy_model = space_dir / "ggml base.bin"
    dummy_model.write_bytes(b"")

    exe, model = resolve_whisper_paths(
        executable_path=dummy_exe,
        model_path=dummy_model,
    )
    assert exe == dummy_exe.resolve()
    assert model == dummy_model.resolve()

    mock_runner = MagicMock()
    mock_runner.return_value = MagicMock(returncode=0, stdout="hello world", stderr="")

    stt = WhisperCppSTT(
        executable_path=dummy_exe,
        model_path=dummy_model,
        _runner=mock_runner,
    )
    fmt = AudioFormat(sample_rate=16000, channels=1, sample_width=2)
    audio = AudioData(data=b"\x00" * 3200, format=fmt)

    text = stt.transcribe(audio)
    assert text == "hello world"
    assert mock_runner.called
    cmd_executed = mock_runner.call_args[0][0]
    assert cmd_executed[0] == str(dummy_exe.resolve())
    assert cmd_executed[2] == str(dummy_model.resolve())
    assert mock_runner.call_args[1].get("shell") is False


def test_path_resolution_independent_of_working_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Path discovery is anchored to source code and does not alter based on os.getcwd()."""
    foreign_dir = tmp_path / "foreign_workdir"
    foreign_dir.mkdir()
    monkeypatch.chdir(foreign_dir)

    exe, model = resolve_whisper_paths()
    repo_root = Path(__file__).resolve().parents[2]
    expected_model = repo_root / "models" / "whisper" / "ggml-base.en.bin"
    if expected_model.is_file():
        assert model == expected_model.resolve()


def test_stt_initialization_independent_of_tts(temp_db: dict[str, Path], tmp_path: Path):
    """STT initializes with NullTextToSpeech when Kokoro dependencies or models are absent."""
    dummy_exe = tmp_path / "whisper.exe"
    dummy_exe.write_bytes(b"")
    dummy_model = tmp_path / "model.bin"
    dummy_model.write_bytes(b"")

    mock_recorder = MagicMock(spec=AudioRecorder)
    mock_stt = MagicMock(spec=SpeechToText)
    mock_stt.transcribe.return_value = "help"

    with patch("app.speech.adapters.sounddevice_io.SoundDeviceRecorder", return_value=mock_recorder), \
         patch("app.speech.adapters.whisper_cpp.WhisperCppSTT", return_value=mock_stt), \
         patch("app.bootstrap.resolve_kokoro_paths", return_value=(None, None, None)):

        controller = create_controller(
            memory_db_path=temp_db["mem_db"],
            tasks_db_path=temp_db["tasks_db"],
            enable_speech=True,
            whisper_executable_path=dummy_exe,
            whisper_model_path=dummy_model,
        )
        try:
            assert controller.voice_pipeline is not None
            assert controller.voice_pipeline.stt is mock_stt
            assert isinstance(controller.voice_pipeline.tts, NullTextToSpeech)

            # Test audio processing succeeds without TTS
            fmt = AudioFormat(sample_rate=16000, channels=1, sample_width=2)
            audio = AudioData(data=b"\x00" * 3200, format=fmt)

            controller.start()
            res = controller.handle_audio(audio, speak_response=True)
            assert res.success is True
            assert res.transcript == "help"
            assert res.spoken_response is False
        finally:
            controller.stop()


def test_missing_sounddevice_graceful_degradation(temp_db: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """When sounddevice is unavailable, controller degrades gracefully to text-only mode."""
    dummy_exe = tmp_path / "whisper.exe"
    dummy_exe.write_bytes(b"")
    dummy_model = tmp_path / "model.bin"
    dummy_model.write_bytes(b"")

    def failing_recorder(*args, **kwargs):
        raise SpeechBackendError("The 'sounddevice' package is not installed.")

    with patch("app.speech.adapters.sounddevice_io.SoundDeviceRecorder", side_effect=failing_recorder):
        controller = create_controller(
            memory_db_path=temp_db["mem_db"],
            tasks_db_path=temp_db["tasks_db"],
            enable_speech=True,
            whisper_executable_path=dummy_exe,
            whisper_model_path=dummy_model,
        )
        try:
            assert controller.voice_pipeline is None
            controller.start()
            res = controller.handle_text("help")
            assert res.success is True
            assert "Available commands" in res.message
        finally:
            controller.stop()


def test_bootstrap_graceful_degradation_when_whisper_missing(temp_db: dict[str, Path]):
    """When Whisper executable and model are not found, bootstrap degrades to text-only mode."""
    with patch("app.bootstrap.resolve_whisper_paths", return_value=(None, None)):
        controller = create_controller(
            memory_db_path=temp_db["mem_db"],
            tasks_db_path=temp_db["tasks_db"],
            enable_speech=True,
        )
        try:
            assert controller.voice_pipeline is None
            controller.start()
            res = controller.handle_text("help")
            assert res.success is True
        finally:
            controller.stop()


def test_conversational_voice_reaches_brain(temp_db: dict[str, Path], tmp_path: Path):
    """Conversational voice input routes through TextAssistant to the AI provider/brain."""
    from app.ai import GenerationResult, ModelInfo, ModelProvider

    dummy_exe = tmp_path / "whisper.exe"
    dummy_exe.write_bytes(b"")
    dummy_model = tmp_path / "model.bin"
    dummy_model.write_bytes(b"")

    mock_recorder = MagicMock(spec=AudioRecorder)
    mock_stt = MagicMock(spec=SpeechToText)
    mock_stt.transcribe.return_value = "Hello Anaya, how are you today?"

    mock_provider = MagicMock(spec=ModelProvider)
    mock_provider.ping.return_value = True
    mock_provider.list_models.return_value = [ModelInfo(name="mock-model")]
    mock_provider.chat.return_value = GenerationResult(
        content="I am doing well! How can I assist you?",
        model="mock-model",
        success=True,
    )

    with patch("app.speech.adapters.sounddevice_io.SoundDeviceRecorder", return_value=mock_recorder), \
         patch("app.speech.adapters.whisper_cpp.WhisperCppSTT", return_value=mock_stt), \
         patch("app.bootstrap.resolve_kokoro_paths", return_value=(None, None, None)):

        controller = create_controller(
            memory_db_path=temp_db["mem_db"],
            tasks_db_path=temp_db["tasks_db"],
            enable_speech=True,
            enable_ai=True,
            custom_ai_provider=mock_provider,
            whisper_executable_path=dummy_exe,
            whisper_model_path=dummy_model,
        )
        try:
            controller.start()
            fmt = AudioFormat(sample_rate=16000, channels=1, sample_width=2)
            audio = AudioData(data=b"\x00" * 3200, format=fmt)

            res = controller.handle_audio(audio, speak_response=True)
            assert res.success is True
            assert res.transcript == "Hello Anaya, how are you today?"
            assert res.action_result is not None
            assert res.action_result.success is True
            assert "I am doing well" in res.action_result.message
            assert res.spoken_response is False
        finally:
            controller.stop()


def test_recognized_command_via_voice_follows_deterministic_path(temp_db: dict[str, Path], tmp_path: Path):
    """Recognized commands spoken via voice follow deterministic command execution, not AI fallback."""
    from app.ai import GenerationResult, ModelInfo, ModelProvider

    dummy_exe = tmp_path / "whisper.exe"
    dummy_exe.write_bytes(b"")
    dummy_model = tmp_path / "model.bin"
    dummy_model.write_bytes(b"")

    mock_recorder = MagicMock(spec=AudioRecorder)
    mock_stt = MagicMock(spec=SpeechToText)
    mock_stt.transcribe.return_value = "list tasks"

    mock_provider = MagicMock(spec=ModelProvider)
    mock_provider.ping.return_value = True
    mock_provider.list_models.return_value = [ModelInfo(name="mock-model")]

    with patch("app.speech.adapters.sounddevice_io.SoundDeviceRecorder", return_value=mock_recorder), \
         patch("app.speech.adapters.whisper_cpp.WhisperCppSTT", return_value=mock_stt), \
         patch("app.bootstrap.resolve_kokoro_paths", return_value=(None, None, None)):

        controller = create_controller(
            memory_db_path=temp_db["mem_db"],
            tasks_db_path=temp_db["tasks_db"],
            enable_speech=True,
            enable_ai=True,
            custom_ai_provider=mock_provider,
            whisper_executable_path=dummy_exe,
            whisper_model_path=dummy_model,
        )
        try:
            controller.start()
            fmt = AudioFormat(sample_rate=16000, channels=1, sample_width=2)
            audio = AudioData(data=b"\x00" * 3200, format=fmt)

            res = controller.handle_audio(audio)
            assert res.success is True
            assert res.transcript == "list tasks"
            assert res.action_result is not None
            assert res.action_result.success is True
            # AI provider must not be invoked for registered deterministic commands
            assert not mock_provider.chat.called
        finally:
            controller.stop()


def test_unknown_command_does_not_accidentally_execute(temp_db: dict[str, Path], tmp_path: Path):
    """Unknown commands do not execute accidentally and return informative safe resolution."""
    dummy_exe = tmp_path / "whisper.exe"
    dummy_exe.write_bytes(b"")
    dummy_model = tmp_path / "model.bin"
    dummy_model.write_bytes(b"")

    mock_recorder = MagicMock(spec=AudioRecorder)
    mock_stt = MagicMock(spec=SpeechToText)
    mock_stt.transcribe.return_value = "nonexistent_command_xyz"

    with patch("app.speech.adapters.sounddevice_io.SoundDeviceRecorder", return_value=mock_recorder), \
         patch("app.speech.adapters.whisper_cpp.WhisperCppSTT", return_value=mock_stt), \
         patch("app.bootstrap.resolve_kokoro_paths", return_value=(None, None, None)):

        controller = create_controller(
            memory_db_path=temp_db["mem_db"],
            tasks_db_path=temp_db["tasks_db"],
            enable_speech=True,
            enable_ai=False,
            whisper_executable_path=dummy_exe,
            whisper_model_path=dummy_model,
        )
        try:
            controller.start()
            fmt = AudioFormat(sample_rate=16000, channels=1, sample_width=2)
            audio = AudioData(data=b"\x00" * 3200, format=fmt)

            res = controller.handle_audio(audio)
            assert res.success is False
            assert "Command not recognized" in res.action_result.message
        finally:
            controller.stop()


def test_voice_pipeline_speak_no_warning_when_null_adapters(caplog: pytest.LogCaptureFixture):
    """VoicePipeline.speak() logs at DEBUG, not WARNING, when null adapters are configured."""
    import logging
    from app.speech.pipeline import VoicePipeline

    mock_rec = MagicMock(spec=AudioRecorder)
    mock_stt = MagicMock(spec=SpeechToText)
    mock_assistant = MagicMock()
    null_tts = NullTextToSpeech("Kokoro unavailable")
    null_player = NullAudioPlayer("Audio sink unavailable")

    pipeline = VoicePipeline(
        recorder=mock_rec,
        stt=mock_stt,
        assistant=mock_assistant,
        tts=null_tts,
        player=null_player,
    )

    with caplog.at_level(logging.DEBUG):
        spoken = pipeline.speak("Testing silent fallback for uninstalled TTS")

    assert spoken is False
    # Verify no warning or error was logged
    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warning_records) == 0

    # Verify debug message is present
    debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("Spoken response skipped" in r.message for r in debug_records)
