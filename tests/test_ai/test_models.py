"""Unit tests for AI data models, hardware profiles, and settings."""

import json
from pathlib import Path

import pytest

from app.ai.models import (
    ChatMessage,
    GenerationOptions,
    GenerationResult,
    HardwareProfile,
    ModelDetails,
    ModelInfo,
    ModelSettings,
    ModelValidationError,
)


class TestAIModels:
    """Test suite for AI model representations and validations."""

    def test_chat_message_valid(self):
        msg = ChatMessage(role="user", content="Hello Anaya")
        assert msg.role == "user"
        assert msg.content == "Hello Anaya"

        sys_msg = ChatMessage(role="system", content="Be helpful")
        assert sys_msg.role == "system"

        asst_msg = ChatMessage(role="assistant", content="Hi there!")
        assert asst_msg.role == "assistant"

    def test_chat_message_invalid_role(self):
        with pytest.raises(ModelValidationError, match="Invalid chat message role"):
            ChatMessage(role="admin", content="Hello")

    def test_chat_message_non_string_content(self):
        with pytest.raises(ModelValidationError, match="must be a string"):
            ChatMessage(role="user", content=123)  # type: ignore

    def test_model_info_and_details(self):
        details = ModelDetails(
            family="qwen2",
            parameter_size="1.5B",
            quantization_level="Q4_K_M",
            context_length=32768,
        )
        info = ModelInfo(
            name="qwen2.5:1.5b",
            size_bytes=986000000,
            modified_at="2026-11-01T12:00:00Z",
            details=details,
        )
        assert info.name == "qwen2.5:1.5b"
        assert info.size_bytes == 986000000
        assert info.details.parameter_size == "1.5B"
        assert info.details.quantization_level == "Q4_K_M"

    def test_model_info_invalid_name(self):
        with pytest.raises(ModelValidationError, match="non-empty string"):
            ModelInfo(name="")

    def test_generation_options_validation(self):
        opts = GenerationOptions(
            temperature=0.7,
            num_predict=512,
            num_ctx=2048,
            top_k=40,
            top_p=0.9,
            keep_alive="5m",
        )
        assert opts.temperature == 0.7
        assert opts.num_predict == 512

        with pytest.raises(ModelValidationError, match="temperature"):
            GenerationOptions(temperature=2.5)

        with pytest.raises(ModelValidationError, match="num_predict"):
            GenerationOptions(num_predict=0)

        with pytest.raises(ModelValidationError, match="num_ctx"):
            GenerationOptions(num_ctx=128)

        with pytest.raises(ModelValidationError, match="top_p"):
            GenerationOptions(top_p=1.5)

    def test_generation_result_dataclass(self):
        res = GenerationResult(
            success=True,
            content="I am ready.",
            model="qwen2.5:1.5b",
            total_duration_ns=1500000,
            eval_count=20,
        )
        assert res.success is True
        assert res.content == "I am ready."
        assert res.model == "qwen2.5:1.5b"

    def test_hardware_profile_from_str(self):
        assert HardwareProfile.from_str("lightweight") == HardwareProfile.LIGHTWEIGHT
        assert HardwareProfile.from_str("BALANCED") == HardwareProfile.BALANCED
        assert HardwareProfile.from_str("advanced") == HardwareProfile.ADVANCED
        assert HardwareProfile.from_str("custom") == HardwareProfile.CUSTOM
        assert HardwareProfile.from_str("unknown") == HardwareProfile.BALANCED
        assert HardwareProfile.from_str(None) == HardwareProfile.BALANCED


class TestModelSettings:
    """Test suite for ModelSettings persistence and defaults."""

    def test_default_settings(self):
        settings = ModelSettings()
        assert settings.enabled is True
        assert settings.provider == "ollama"
        assert settings.base_url == "http://127.0.0.1:11434"
        assert settings.profile == HardwareProfile.BALANCED.value
        assert settings.context_window == 2048
        assert settings.max_tokens == 512
        assert settings.temperature == 0.7
        assert settings.timeout_seconds == 30.0

    def test_settings_validation_failure(self):
        s1 = ModelSettings(context_window=100)
        with pytest.raises(ModelValidationError):
            s1.validate()

        s2 = ModelSettings(temperature=3.0)
        with pytest.raises(ModelValidationError):
            s2.validate()

    def test_to_dict_and_from_dict(self):
        s = ModelSettings(
            selected_model="qwen2.5:0.5b",
            profile="lightweight",
            max_tokens=256,
        )
        d = s.to_dict()
        assert d["selected_model"] == "qwen2.5:0.5b"
        assert d["profile"] == "lightweight"
        assert d["max_tokens"] == 256

        loaded = ModelSettings.from_dict(d)
        assert loaded.selected_model == "qwen2.5:0.5b"
        assert loaded.profile == "lightweight"
        assert loaded.max_tokens == 256

    def test_save_and_load_file(self, tmp_path):
        target = tmp_path / "settings.json"
        s = ModelSettings(
            selected_model="llama3.2:1b",
            profile="lightweight",
            temperature=0.5,
        )
        s.save(target)
        assert target.exists()

        loaded = ModelSettings.load(target)
        assert loaded.selected_model == "llama3.2:1b"
        assert loaded.profile == "lightweight"
        assert loaded.temperature == 0.5

    def test_load_non_existent_file_returns_defaults(self, tmp_path):
        target = tmp_path / "missing.json"
        loaded = ModelSettings.load(target)
        assert loaded.selected_model is None
        assert loaded.profile == "balanced"

    def test_load_corrupted_json_returns_defaults(self, tmp_path):
        target = tmp_path / "corrupt.json"
        target.write_text("NOT A JSON {{{", encoding="utf-8")
        loaded = ModelSettings.load(target)
        assert loaded.selected_model is None
        assert loaded.profile == "balanced"
