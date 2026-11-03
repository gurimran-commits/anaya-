"""Tests for WhisperCppSTT adapter, subprocess execution, and temp file management."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.speech.adapters.whisper_cpp import WhisperCppSTT
from app.speech.models import (
    AudioData,
    AudioFormat,
    AudioValidationError,
    SpeechBackendError,
    SpeechTimeoutError,
)


class TestWhisperCppSTT:
    """Test suite for Whisper.cpp CLI STT adapter."""

    def test_missing_executable_raises_speech_backend_error(self, tmp_path):
        model_file = tmp_path / "ggml-model.bin"
        model_file.write_bytes(b"dummy_model")

        stt = WhisperCppSTT(
            executable_path=tmp_path / "non_existent_whisper.exe",
            model_path=model_file,
        )
        audio = AudioData(b"\x00\x00" * 16000, AudioFormat(sample_rate=16000, channels=1))
        with pytest.raises(SpeechBackendError, match="executable not found"):
            stt.transcribe(audio)

    def test_missing_model_raises_speech_backend_error(self, tmp_path):
        exe_file = tmp_path / "whisper.exe"
        exe_file.write_bytes(b"dummy_exe")

        stt = WhisperCppSTT(
            executable_path=exe_file,
            model_path=tmp_path / "non_existent_model.bin",
        )
        audio = AudioData(b"\x00\x00" * 16000, AudioFormat(sample_rate=16000, channels=1))
        with pytest.raises(SpeechBackendError, match="model not found"):
            stt.transcribe(audio)

    def test_incompatible_audio_format_rejected(self, tmp_path):
        exe_file = tmp_path / "whisper.exe"
        exe_file.write_bytes(b"dummy_exe")
        model_file = tmp_path / "model.bin"
        model_file.write_bytes(b"dummy_model")

        stt = WhisperCppSTT(executable_path=exe_file, model_path=model_file)
        # 24 kHz audio instead of 16 kHz
        incompatible_audio = AudioData(b"\x00\x00" * 24000, AudioFormat(sample_rate=24000, channels=1))

        with pytest.raises(AudioValidationError, match="requires 16000 Hz mono"):
            stt.transcribe(incompatible_audio)

    def test_successful_transcription_and_temp_file_cleanup(self, tmp_path):
        exe_file = tmp_path / "whisper.exe"
        exe_file.write_bytes(b"dummy_exe")
        model_file = tmp_path / "model.bin"
        model_file.write_bytes(b"dummy_model")

        recorded_temp_wav: str | None = None

        def fake_runner(cmd, **kwargs):
            nonlocal recorded_temp_wav
            # Extract temp wav argument (-f <temp_wav>)
            recorded_temp_wav = cmd[cmd.index("-f") + 1]
            # Ensure the file exists while running
            assert Path(recorded_temp_wav).is_file()
            # Ensure shell=False is enforced
            assert kwargs.get("shell") is False
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout="[00:00:00.000 --> 00:00:02.500]  system status  \n",
                stderr="",
            )

        stt = WhisperCppSTT(
            executable_path=exe_file,
            model_path=model_file,
            _runner=fake_runner,
        )

        audio = AudioData(b"\x00\x00" * 16000, AudioFormat(sample_rate=16000, channels=1))
        transcript = stt.transcribe(audio)

        # Output normalized and timestamps stripped
        assert transcript == "system status"
        # Temporary file guaranteed deleted after transcribe()
        assert recorded_temp_wav is not None
        assert not Path(recorded_temp_wav).exists()

    def test_subprocess_non_zero_exit_code_and_cleanup(self, tmp_path):
        exe_file = tmp_path / "whisper.exe"
        exe_file.write_bytes(b"dummy_exe")
        model_file = tmp_path / "model.bin"
        model_file.write_bytes(b"dummy_model")

        recorded_temp_wav: str | None = None

        def fake_runner(cmd, **kwargs):
            nonlocal recorded_temp_wav
            recorded_temp_wav = cmd[cmd.index("-f") + 1]
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=1,
                stdout="",
                stderr="GPU out of memory or corrupted model",
            )

        stt = WhisperCppSTT(
            executable_path=exe_file,
            model_path=model_file,
            _runner=fake_runner,
        )

        audio = AudioData(b"\x00\x00" * 16000, AudioFormat(sample_rate=16000, channels=1))
        with pytest.raises(SpeechBackendError, match="return code 1"):
            stt.transcribe(audio)

        # Temporary file still cleaned up
        assert recorded_temp_wav is not None
        assert not Path(recorded_temp_wav).exists()

    def test_subprocess_timeout_and_cleanup(self, tmp_path):
        exe_file = tmp_path / "whisper.exe"
        exe_file.write_bytes(b"dummy_exe")
        model_file = tmp_path / "model.bin"
        model_file.write_bytes(b"dummy_model")

        recorded_temp_wav: str | None = None

        def fake_runner(cmd, **kwargs):
            nonlocal recorded_temp_wav
            recorded_temp_wav = cmd[cmd.index("-f") + 1]
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=5.0)

        stt = WhisperCppSTT(
            executable_path=exe_file,
            model_path=model_file,
            timeout_seconds=5.0,
            _runner=fake_runner,
        )

        audio = AudioData(b"\x00\x00" * 16000, AudioFormat(sample_rate=16000, channels=1))
        with pytest.raises(SpeechTimeoutError, match="timed out after 5.0 seconds"):
            stt.transcribe(audio)

        assert recorded_temp_wav is not None
        assert not Path(recorded_temp_wav).exists()
