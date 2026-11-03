"""Tests for AudioFormat, AudioData, SpeechConfig, and PipelineResult models."""

import pytest

from app.speech.models import (
    AudioData,
    AudioFormat,
    AudioValidationError,
    PipelineResult,
    SpeechConfig,
)


class TestAudioFormat:
    """Test suite for AudioFormat validation and properties."""

    def test_default_format(self):
        fmt = AudioFormat()
        assert fmt.sample_rate == 16000
        assert fmt.channels == 1
        assert fmt.sample_width == 2
        assert fmt.encoding == "pcm_s16le"
        assert fmt.frame_size == 2
        assert fmt.is_pcm is True

    def test_custom_valid_formats(self):
        stereo_24k = AudioFormat(sample_rate=24000, channels=2, sample_width=2, encoding="pcm_s16le")
        assert stereo_24k.sample_rate == 24000
        assert stereo_24k.channels == 2
        assert stereo_24k.frame_size == 4

        float_32 = AudioFormat(sample_rate=48000, channels=1, sample_width=4, encoding="pcm_f32le")
        assert float_32.frame_size == 4
        assert float_32.is_pcm is True

        wav_fmt = AudioFormat(sample_rate=16000, channels=1, sample_width=2, encoding="wav")
        assert wav_fmt.is_pcm is False

    @pytest.mark.parametrize("invalid_rate", [0, -16000, 1000, 250000, "16000"])
    def test_invalid_sample_rate(self, invalid_rate):
        with pytest.raises(AudioValidationError):
            AudioFormat(sample_rate=invalid_rate)

    @pytest.mark.parametrize("invalid_channels", [0, 3, 5, -1])
    def test_invalid_channels(self, invalid_channels):
        with pytest.raises(AudioValidationError):
            AudioFormat(channels=invalid_channels)

    @pytest.mark.parametrize("invalid_width", [0, 3, 5, 8])
    def test_invalid_sample_width(self, invalid_width):
        with pytest.raises(AudioValidationError):
            AudioFormat(sample_width=invalid_width)

    @pytest.mark.parametrize("empty_encoding", ["", "   "])
    def test_empty_encoding(self, empty_encoding):
        with pytest.raises(AudioValidationError):
            AudioFormat(encoding=empty_encoding)


class TestAudioData:
    """Test suite for AudioData validation, encapsulation, and WAV conversion."""

    def test_valid_pcm_audio_data(self):
        fmt = AudioFormat(sample_rate=16000, channels=1, sample_width=2)
        # 16000 samples * 2 bytes = 32000 bytes = 1.0 second
        raw_pcm = b"\x01\x00" * 16000
        audio = AudioData(data=raw_pcm, format=fmt)

        assert len(audio) == 32000
        assert audio.duration == pytest.approx(1.0, rel=1e-3)
        assert audio.format == fmt
        assert audio.data == raw_pcm

    def test_buffer_isolation_and_immutability(self):
        fmt = AudioFormat(sample_rate=16000, channels=1, sample_width=2)
        mutable_buf = bytearray(b"\x00\x00" * 100)
        audio = AudioData(data=mutable_buf, format=fmt)

        # Mutate the source bytearray
        mutable_buf[0] = 0xFF
        # The internal buffer of AudioData must remain untouched
        assert audio.data[0] == 0x00
        assert isinstance(audio.data, bytes)

    def test_empty_payload_rejected(self):
        fmt = AudioFormat()
        with pytest.raises(AudioValidationError, match="cannot be empty"):
            AudioData(data=b"", format=fmt)

    def test_invalid_payload_type(self):
        fmt = AudioFormat()
        with pytest.raises(AudioValidationError, match="must be bytes or bytearray"):
            AudioData(data="invalid_string_audio", format=fmt)  # type: ignore

    def test_invalid_format_type(self):
        with pytest.raises(AudioValidationError, match="must be an AudioFormat instance"):
            AudioData(data=b"\x00\x00", format="not_a_format")  # type: ignore

    def test_explicit_duration_validation(self):
        fmt = AudioFormat()
        audio = AudioData(data=b"\x00\x00" * 100, format=fmt, duration=2.5)
        assert audio.duration == 2.5

        with pytest.raises(AudioValidationError, match="positive number"):
            AudioData(data=b"\x00\x00" * 100, format=fmt, duration=-1.0)

        with pytest.raises(AudioValidationError, match="positive number"):
            AudioData(data=b"\x00\x00" * 100, format=fmt, duration=0)

    def test_wav_conversion_roundtrip(self):
        fmt = AudioFormat(sample_rate=16000, channels=1, sample_width=2, encoding="pcm_s16le")
        raw_pcm = b"\x02\x01" * 8000  # 0.5s of audio
        audio = AudioData(data=raw_pcm, format=fmt)

        wav_bytes = audio.to_wav_bytes()
        assert wav_bytes.startswith(b"RIFF")
        assert b"WAVE" in wav_bytes[:12]

        # Reconstruct AudioData from WAV bytes
        reconstructed = AudioData.from_wav_bytes(wav_bytes)
        assert reconstructed.format.sample_rate == 16000
        assert reconstructed.format.channels == 1
        assert reconstructed.format.sample_width == 2
        assert reconstructed.format.encoding == "pcm_s16le"
        assert reconstructed.data == raw_pcm
        assert reconstructed.duration == pytest.approx(0.5, rel=1e-3)

    def test_invalid_wav_bytes_rejected(self):
        with pytest.raises(AudioValidationError, match="cannot be empty"):
            AudioData.from_wav_bytes(b"")

        with pytest.raises(AudioValidationError):
            AudioData.from_wav_bytes(b"NOT_A_VALID_WAV_HEADER_DATA")


class TestSpeechConfig:
    """Test suite for SpeechConfig defaults and parameter bounds."""

    def test_default_config(self):
        config = SpeechConfig()
        assert config.sample_rate == 16000
        assert config.channels == 1
        assert config.sample_width == 2
        assert config.max_record_seconds == 15.0
        assert config.push_to_talk is True
        assert config.stt_backend == "mock"
        assert config.tts_backend == "mock"
        assert config.tts_sample_rate == 24000

    @pytest.mark.parametrize("invalid_limit", [0, -5, 60.1, 100])
    def test_max_record_seconds_bounds(self, invalid_limit):
        with pytest.raises(AudioValidationError, match="max_record_seconds"):
            SpeechConfig(max_record_seconds=invalid_limit)

    @pytest.mark.parametrize("invalid_timeout", [0, -10, 120.1, 300])
    def test_whisper_timeout_bounds(self, invalid_timeout):
        with pytest.raises(AudioValidationError, match="whisper_cpp_timeout_seconds"):
            SpeechConfig(whisper_cpp_timeout_seconds=invalid_timeout)

    def test_invalid_sample_rate_in_config(self):
        with pytest.raises(AudioValidationError):
            SpeechConfig(sample_rate=1000)


class TestPipelineResult:
    """Test suite for PipelineResult."""

    def test_pipeline_result_immutability(self):
        res = PipelineResult(transcript="status", spoken_response=True, success=True)
        assert res.transcript == "status"
        assert res.spoken_response is True
        assert res.success is True
        assert res.error is None
