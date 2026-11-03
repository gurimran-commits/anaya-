"""Tests for SoundDeviceRecorder and SoundDevicePlayer adapters."""

import sys
from unittest.mock import MagicMock, patch

import pytest

from app.speech.adapters.sounddevice_io import (
    SoundDevicePlayer,
    SoundDeviceRecorder,
)
from app.speech.models import (
    AudioData,
    AudioFormat,
    AudioValidationError,
    SpeechBackendError,
    SpeechDeviceError,
)


class TestSoundDeviceAdapters:
    """Test suite for sounddevice hardware recording and playback adapters."""

    def test_missing_dependency_raises_speech_backend_error(self, monkeypatch):
        # Simulate sounddevice not being installed
        monkeypatch.setitem(sys.modules, "sounddevice", None)

        recorder = SoundDeviceRecorder()
        with pytest.raises(SpeechBackendError, match="The 'sounddevice' package is not installed"):
            recorder.record(1.0)

        player = SoundDevicePlayer()
        fmt = AudioFormat()
        audio = AudioData(b"\x00\x00" * 100, fmt)
        with pytest.raises(SpeechBackendError, match="The 'sounddevice' package is not installed"):
            player.play(audio)

    def test_recorder_parameter_validation(self):
        with pytest.raises(AudioValidationError, match="Invalid sample rate"):
            SoundDeviceRecorder(sample_rate=1000)

        with pytest.raises(AudioValidationError, match="Invalid channel count"):
            SoundDeviceRecorder(channels=3)

        with pytest.raises(AudioValidationError, match="max_duration"):
            SoundDeviceRecorder(max_duration=0)

        with pytest.raises(AudioValidationError, match="max_duration"):
            SoundDeviceRecorder(max_duration=120)

    def test_recorder_successful_capture_with_mocked_sd(self, monkeypatch):
        mock_sd = MagicMock()
        import numpy as np

        # 16000 samples * 1 ch
        fake_samples = np.zeros(16000, dtype=np.int16)
        mock_sd.rec.return_value = fake_samples

        monkeypatch.setattr("app.speech.adapters.sounddevice_io._get_sounddevice", lambda: mock_sd)

        recorder = SoundDeviceRecorder(sample_rate=16000, channels=1, max_duration=5.0)
        audio = recorder.record(duration=1.0)

        assert isinstance(audio, AudioData)
        assert audio.format.sample_rate == 16000
        assert audio.format.channels == 1
        assert audio.duration == pytest.approx(1.0, rel=1e-3)
        assert len(audio) == 32000

        mock_sd.rec.assert_called_once()
        mock_sd.wait.assert_called_once()
        mock_sd.stop.assert_called()

    def test_recorder_enforces_configured_max_duration(self, monkeypatch):
        mock_sd = MagicMock()
        import numpy as np

        fake_samples = np.zeros(16000 * 5, dtype=np.int16)
        mock_sd.rec.return_value = fake_samples

        monkeypatch.setattr("app.speech.adapters.sounddevice_io._get_sounddevice", lambda: mock_sd)

        recorder = SoundDeviceRecorder(sample_rate=16000, channels=1, max_duration=5.0)
        # Request 10s duration when max is 5.0s
        audio = recorder.record(duration=10.0)

        assert audio.duration == 5.0
        # Recorded frames should be 16000 * 5 = 80000
        mock_sd.rec.assert_called_once_with(80000, samplerate=16000, channels=1, dtype="int16", device=None)

    def test_recorder_device_error_handling(self, monkeypatch):
        mock_sd = MagicMock()
        mock_sd.rec.side_effect = RuntimeError("PortAudio device error")

        monkeypatch.setattr("app.speech.adapters.sounddevice_io._get_sounddevice", lambda: mock_sd)

        recorder = SoundDeviceRecorder()
        with pytest.raises(SpeechDeviceError, match="Audio recording failed"):
            recorder.record(1.0)

        mock_sd.stop.assert_called()

    def test_player_successful_playback(self, monkeypatch):
        mock_sd = MagicMock()
        monkeypatch.setattr("app.speech.adapters.sounddevice_io._get_sounddevice", lambda: mock_sd)

        player = SoundDevicePlayer()
        fmt = AudioFormat(sample_rate=24000, channels=1, sample_width=2, encoding="pcm_s16le")
        audio = AudioData(b"\x00\x00" * 24000, fmt)

        player.play(audio)
        mock_sd.play.assert_called_once()
        mock_sd.wait.assert_called_once()
        mock_sd.stop.assert_called()

    def test_player_wav_format_conversion_and_playback(self, monkeypatch):
        mock_sd = MagicMock()
        monkeypatch.setattr("app.speech.adapters.sounddevice_io._get_sounddevice", lambda: mock_sd)

        player = SoundDevicePlayer()
        # Create audio and convert to WAV bytes
        raw_pcm_audio = AudioData(b"\x00\x00" * 8000, AudioFormat(sample_rate=16000, channels=1))
        wav_bytes = raw_pcm_audio.to_wav_bytes()

        wav_audio = AudioData(wav_bytes, AudioFormat(sample_rate=16000, channels=1, encoding="wav"))
        player.play(wav_audio)

        mock_sd.play.assert_called_once()

    def test_player_device_error_handling(self, monkeypatch):
        mock_sd = MagicMock()
        mock_sd.play.side_effect = RuntimeError("Audio output device busy")
        monkeypatch.setattr("app.speech.adapters.sounddevice_io._get_sounddevice", lambda: mock_sd)

        player = SoundDevicePlayer()
        audio = AudioData(b"\x00\x00" * 100, AudioFormat())

        with pytest.raises(SpeechDeviceError, match="Audio playback failed"):
            player.play(audio)

        mock_sd.stop.assert_called()
