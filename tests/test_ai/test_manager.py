"""Unit tests for ModelManager lifecycle, model switching, and response generation."""

import pytest

from app.ai.history import ConversationHistory
from app.ai.manager import ModelManager
from app.ai.models import (
    ChatMessage,
    GenerationOptions,
    GenerationResult,
    HardwareProfile,
    ModelDetails,
    ModelInfo,
    ModelNotFoundError,
    ModelSettings,
    ModelSwitchError,
)
from app.ai.registry import ModelRegistry


class MockProvider:
    """Mock provider recording calls and simulating responses."""

    def __init__(self, installed_names: list[str], responsive: bool = True):
        self.installed = [
            ModelInfo(name=name, size_bytes=1000000, details=ModelDetails())
            for name in installed_names
        ]
        self.responsive = responsive
        self.unloaded_models: list[str] = []
        self.chat_calls: list[tuple[list[ChatMessage], str, GenerationOptions | None]] = []

    def ping(self) -> bool:
        return self.responsive

    def list_models(self) -> list[ModelInfo]:
        if not self.responsive:
            raise ConnectionError("Provider offline")
        return self.installed

    def get_model_details(self, model_name: str) -> ModelDetails | None:
        return ModelDetails()

    def chat(
        self,
        messages: list[ChatMessage],
        model: str,
        options: GenerationOptions | None = None,
    ) -> GenerationResult:
        if not self.responsive:
            raise ConnectionError("Provider offline")
        self.chat_calls.append((messages, model, options))
        return GenerationResult(
            success=True,
            content=f"Response from {model} for '{messages[-1].content}'",
            model=model,
        )

    def unload_model(self, model_name: str) -> bool:
        self.unloaded_models.append(model_name)
        return True


class TestModelManager:
    """Test suite for ModelManager operations."""

    def test_initial_state_and_discovery(self):
        provider = MockProvider(["qwen2.5:1.5b", "llama3.2:1b"])
        manager = ModelManager(provider=provider)

        assert manager.is_available() is True
        installed = manager.list_installed_models()
        assert len(installed) == 2
        assert manager.active_model is None

    def test_select_installed_model_success(self):
        provider = MockProvider(["qwen2.5:1.5b", "llama3.2:1b"])
        manager = ModelManager(provider=provider)

        success = manager.select_model("qwen2.5:1.5b")
        assert success is True
        assert manager.active_model == "qwen2.5:1.5b"
        assert manager.active_profile == HardwareProfile.BALANCED
        assert manager.settings.selected_model == "qwen2.5:1.5b"

    def test_select_uninstalled_model_raises_error(self):
        provider = MockProvider(["qwen2.5:1.5b"])
        manager = ModelManager(provider=provider)

        # Set active model first
        manager.select_model("qwen2.5:1.5b")
        assert manager.active_model == "qwen2.5:1.5b"

        # Attempt to select a non-existent model
        with pytest.raises(ModelNotFoundError, match="is not installed"):
            manager.select_model("non_existent:7b")

        # Active model must remain coherent and untouched
        assert manager.active_model == "qwen2.5:1.5b"
        # The active model was NOT unloaded
        assert "qwen2.5:1.5b" not in provider.unloaded_models

    def test_switching_models_unloads_outgoing_model(self):
        provider = MockProvider(["qwen2.5:1.5b", "llama3.2:1b"])
        manager = ModelManager(provider=provider)

        manager.select_model("qwen2.5:1.5b")
        assert manager.active_model == "qwen2.5:1.5b"

        manager.select_model("llama3.2:1b")
        assert manager.active_model == "llama3.2:1b"
        assert manager.active_profile == HardwareProfile.LIGHTWEIGHT
        # Outgoing model was explicitly unloaded
        assert "qwen2.5:1.5b" in provider.unloaded_models

    def test_select_profile_picks_installed_model(self):
        provider = MockProvider(["qwen2.5:0.5b", "qwen2.5:7b"])
        manager = ModelManager(provider=provider)

        # Selecting Lightweight profile picks qwen2.5:0.5b
        chosen = manager.select_profile(HardwareProfile.LIGHTWEIGHT)
        assert chosen == "qwen2.5:0.5b"
        assert manager.active_model == "qwen2.5:0.5b"
        assert manager.settings.max_tokens == 256

        # Selecting Advanced profile picks qwen2.5:7b
        chosen_adv = manager.select_profile(HardwareProfile.ADVANCED)
        assert chosen_adv == "qwen2.5:7b"
        assert manager.active_model == "qwen2.5:7b"
        assert manager.settings.max_tokens == 1024

    def test_generate_response_success(self):
        provider = MockProvider(["qwen2.5:1.5b"])
        manager = ModelManager(provider=provider)
        manager.select_model("qwen2.5:1.5b")

        resp = manager.generate_response("What is the speed of light?")
        assert resp is not None
        assert "qwen2.5:1.5b" in resp
        assert "speed of light" in resp

        # Check history contains both user and assistant turns
        msgs = manager.history.get_messages()
        # System + user + assistant
        assert len(msgs) == 3
        assert msgs[1].content == "What is the speed of light?"
        assert msgs[2].content == resp

    def test_generate_response_fallback_when_disabled(self):
        provider = MockProvider(["qwen2.5:1.5b"])
        manager = ModelManager(provider=provider)
        manager.select_model("qwen2.5:1.5b")
        manager.disable()

        assert manager.is_enabled is False
        resp = manager.generate_response("Hello")
        assert resp is None
        assert len(provider.chat_calls) == 0

    def test_generate_response_fallback_when_provider_offline(self):
        provider = MockProvider(["qwen2.5:1.5b"], responsive=False)
        manager = ModelManager(provider=provider)
        manager._active_model = "qwen2.5:1.5b"

        # Offline provider must not raise an unhandled exception, returns None
        resp = manager.generate_response("Hello")
        assert resp is None
        # Failed generation must NOT leave a dangling user turn in history
        assert manager.history.turn_count == 0

    def test_selecting_same_model_is_noop(self):
        provider = MockProvider(["qwen2.5:1.5b"])
        manager = ModelManager(provider=provider)
        manager.select_model("qwen2.5:1.5b")
        assert len(provider.unloaded_models) == 0

        # Selecting same model again does not trigger an unload
        assert manager.select_model("qwen2.5:1.5b") is True
        assert len(provider.unloaded_models) == 0

    def test_unload_active_model(self):
        provider = MockProvider(["qwen2.5:1.5b"])
        manager = ModelManager(provider=provider)
        manager.select_model("qwen2.5:1.5b")

        assert manager.unload_active_model() is True
        assert "qwen2.5:1.5b" in provider.unloaded_models

    def test_reset_history(self):
        provider = MockProvider(["qwen2.5:1.5b"])
        manager = ModelManager(provider=provider)
        manager.select_model("qwen2.5:1.5b")

        manager.generate_response("Question 1")
        assert manager.history.turn_count == 2

        manager.reset_history()
        assert manager.history.turn_count == 0

    def test_switch_failure_after_unload_leaves_active_model_none_and_recovers(self, monkeypatch):
        provider = MockProvider(["qwen2.5:1.5b", "llama3.2:1b", "qwen2.5:7b"])
        manager = ModelManager(provider=provider)

        # 1. Activate initial model
        assert manager.select_model("qwen2.5:1.5b") is True
        assert manager.active_model == "qwen2.5:1.5b"
        assert len(provider.unloaded_models) == 0

        # 2. Simulate target activation failure when switching to llama3.2:1b
        orig_setattr = ModelSettings.__setattr__

        def bad_setattr(self, name, val):
            if name == "selected_model" and val == "llama3.2:1b":
                raise RuntimeError("Simulated target activation crash")
            return orig_setattr(self, name, val)

        monkeypatch.setattr(ModelSettings, "__setattr__", bad_setattr)

        with pytest.raises(ModelSwitchError, match="Failed to activate target model 'llama3.2:1b'"):
            manager.select_model("llama3.2:1b")

        # 3. Old model was unloaded first
        assert "qwen2.5:1.5b" in provider.unloaded_models

        # 4. Active model is strictly None (never claims previous model is still loaded)
        assert manager.active_model is None

        # 5. Restore normal behavior and verify clean recovery
        monkeypatch.undo()

        assert manager.select_model("qwen2.5:7b") is True
        assert manager.active_model == "qwen2.5:7b"
        assert manager.active_profile == HardwareProfile.ADVANCED
