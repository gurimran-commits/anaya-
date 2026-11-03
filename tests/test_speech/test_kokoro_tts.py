"""Tests for Kokoro neural TTS adapter, lazy loading, and asset verification."""

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from app.speech.adapters.kokoro_tts import KokoroTTS
from app.speech.models import (
    AudioData,
    AudioFormat,
    AudioValidationError,
    SpeechBackendError,
)


class TestKokoroTTS:
    """Test suite for KokoroTTS neural speech synthesis adapter."""

    def test_missing_dependencies_raises_speech_backend_error(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "kokoro", None)
        tts = KokoroTTS()
        with pytest.raises(SpeechBackendError, match="Kokoro or PyTorch"):
            tts.synthesize("Hello")

    def test_missing_local_model_assets_raises_backend_error(self, tmp_path):
        tts = KokoroTTS(
            model_path=tmp_path / "non_existent_model.pth",
            config_path=tmp_path / "config.json",
        )
        with pytest.raises(SpeechBackendError, match="Kokoro model file not found"):
            tts.synthesize("Hello")

    def test_missing_local_config_file_raises_backend_error(self, tmp_path):
        model_file = tmp_path / "model.pth"
        model_file.write_bytes(b"dummy")

        tts = KokoroTTS(
            model_path=model_file,
            config_path=tmp_path / "non_existent_config.json",
        )
        with pytest.raises(SpeechBackendError, match="Kokoro config file not found"):
            tts.synthesize("Hello")

    def test_missing_voice_asset_raises_backend_error(self, tmp_path):
        model_file = tmp_path / "model.pth"
        model_file.write_bytes(b"dummy")
        config_file = tmp_path / "config.json"
        config_file.write_bytes(b"{}")

        tts = KokoroTTS(
            model_path=model_file,
            config_path=config_file,
            voice="af_bella",
            voices_dir=tmp_path / "voices",
        )
        with pytest.raises(SpeechBackendError, match="Kokoro voice asset 'af_bella.pt' not found"):
            tts.synthesize("Hello")

    def test_empty_or_whitespace_text_validation(self):
        tts = KokoroTTS(_pipeline=MagicMock())
        with pytest.raises(AudioValidationError, match="Cannot synthesize empty"):
            tts.synthesize("")

        with pytest.raises(AudioValidationError, match="Cannot synthesize empty"):
            tts.synthesize("   \n\t  ")

    def test_successful_synthesis_with_mocked_pipeline(self):
        # Create a fake pipeline producing 24 kHz float32 sine audio
        t = np.linspace(0, 0.5, int(24000 * 0.5), endpoint=False)
        fake_audio = np.sin(2 * np.pi * 440 * t).astype(np.float32)

        fake_result = SimpleNamespace(output=SimpleNamespace(audio=fake_audio))
        fake_pipeline = MagicMock(return_value=[fake_result])

        tts = KokoroTTS(_pipeline=fake_pipeline, voice="af_heart")
        audio = tts.synthesize("System ready")

        assert isinstance(audio, AudioData)
        assert audio.format.sample_rate == 24000
        assert audio.format.channels == 1
        assert audio.format.sample_width == 2
        assert audio.format.encoding == "pcm_s16le"
        assert audio.duration == pytest.approx(0.5, rel=1e-3)
        assert len(audio.data) == int(24000 * 0.5 * 2)

    def test_multi_chunk_audio_concatenation(self):
        chunk1 = np.full(12000, 0.5, dtype=np.float32)
        chunk2 = np.full(12000, -0.5, dtype=np.float32)

        res1 = SimpleNamespace(output=SimpleNamespace(audio=chunk1))
        res2 = SimpleNamespace(output=SimpleNamespace(audio=chunk2))
        fake_pipeline = MagicMock(return_value=[res1, res2])

        tts = KokoroTTS(_pipeline=fake_pipeline)
        audio = tts.synthesize("Two sentences concatenated together.")

        # 12000 + 12000 = 24000 samples = 1.0s at 24kHz
        assert audio.duration == pytest.approx(1.0, rel=1e-3)
        assert len(audio.data) == 48000

    def test_pipeline_yielding_no_audio_raises_backend_error(self):
        fake_pipeline = MagicMock(return_value=[])
        tts = KokoroTTS(_pipeline=fake_pipeline)

        with pytest.raises(SpeechBackendError, match="yielded no audio output"):
            tts.synthesize("Some text")

    def test_pipeline_exception_wrapped_safely(self):
        fake_pipeline = MagicMock(side_effect=RuntimeError("PyTorch CUDA out of memory"))
        tts = KokoroTTS(_pipeline=fake_pipeline)

        with pytest.raises(SpeechBackendError, match="Kokoro speech synthesis failed"):
            tts.synthesize("Failing synthesis")
