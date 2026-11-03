"""Unit tests for ModelRegistry and hardware profile recommendations."""

from app.ai.models import HardwareProfile, ModelDetails, ModelInfo
from app.ai.registry import ModelRegistry


class DummyProvider:
    """Mock provider returning fixed installed models."""

    def __init__(self, installed_names: list[str]):
        self.installed = [
            ModelInfo(name=name, size_bytes=1000000, details=ModelDetails())
            for name in installed_names
        ]

    def list_models(self) -> list[ModelInfo]:
        return self.installed


class TestModelRegistry:
    """Test suite for ModelRegistry catalog and profile mappings."""

    def test_catalog_recommendations(self):
        registry = ModelRegistry()
        lightweight = registry.get_recommendations(HardwareProfile.LIGHTWEIGHT)
        assert "qwen2.5:0.5b" in lightweight
        assert "llama3.2:1b" in lightweight

        balanced = registry.get_recommendations(HardwareProfile.BALANCED)
        assert "qwen2.5:1.5b" in balanced
        assert "llama3.2:3b" in balanced

        advanced = registry.get_recommendations(HardwareProfile.ADVANCED)
        assert "qwen2.5:7b" in advanced
        assert "mistral:7b" in advanced

    def test_profile_classification(self):
        registry = ModelRegistry()
        assert registry.get_profile_for_model("qwen2.5:0.5b") == HardwareProfile.LIGHTWEIGHT
        assert registry.get_profile_for_model("llama3.2:1b") == HardwareProfile.LIGHTWEIGHT
        assert registry.get_profile_for_model("qwen2.5:1.5b") == HardwareProfile.BALANCED
        assert registry.get_profile_for_model("gemma2:2b") == HardwareProfile.BALANCED
        assert registry.get_profile_for_model("qwen2.5:7b") == HardwareProfile.ADVANCED
        assert registry.get_profile_for_model("mistral:7b") == HardwareProfile.ADVANCED

    def test_profile_defaults(self):
        registry = ModelRegistry()
        light_opts = registry.get_profile_defaults(HardwareProfile.LIGHTWEIGHT)
        assert light_opts.num_predict == 256
        assert light_opts.num_ctx == 2048

        bal_opts = registry.get_profile_defaults(HardwareProfile.BALANCED)
        assert bal_opts.num_predict == 512
        assert bal_opts.num_ctx == 4096

        adv_opts = registry.get_profile_defaults(HardwareProfile.ADVANCED)
        assert adv_opts.num_predict == 1024
        assert adv_opts.num_ctx == 8192

    def test_discover_installed_and_is_installed(self):
        provider = DummyProvider(["qwen2.5:1.5b", "llama3.2:1b"])
        registry = ModelRegistry()

        installed = registry.discover_installed(provider)
        assert len(installed) == 2
        assert registry.is_installed(provider, "qwen2.5:1.5b") is True
        assert registry.is_installed(provider, "llama3.2:1b") is True
        assert registry.is_installed(provider, "qwen2.5:7b") is False
