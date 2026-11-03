"""Core model manager for active model selection, switching, and generation."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.ai.history import ConversationHistory
from app.ai.models import (
    GenerationOptions,
    HardwareProfile,
    ModelInfo,
    ModelNotFoundError,
    ModelSettings,
    ModelSwitchError,
)
from app.ai.registry import ModelRegistry

if TYPE_CHECKING:
    from app.ai.protocols import ModelProvider

logger = logging.getLogger(__name__)


class ModelManager:
    """Manages active local model lifecycle, profiles, and conversational generation.

    Resource Management Strategy:
        When switching models, ModelManager validates that the target model is installed
        and accessible in the provider before changing state.
        Once the target model is validated, ModelManager unloads the previous active model
        (via keep_alive: 0) before engaging the new model. This trade-off prioritizes
        system stability and prevents concurrent VRAM/RAM exhaustion on low-spec PCs,
        at the cost of a few seconds' warm-up latency on the new model.
    """

    def __init__(
        self,
        provider: ModelProvider,
        registry: ModelRegistry | None = None,
        history: ConversationHistory | None = None,
        settings: ModelSettings | None = None,
    ) -> None:
        self.provider = provider
        self.registry = registry or ModelRegistry()
        self.settings = settings or ModelSettings()
        self.history = history or ConversationHistory(max_turns=self.settings.max_history_turns)

        self._active_model: str | None = self.settings.selected_model
        self._active_profile: HardwareProfile = HardwareProfile.from_str(self.settings.profile)

    @property
    def active_model(self) -> str | None:
        """Return the currently selected model tag, or None if no model is active."""
        return self._active_model

    @property
    def active_profile(self) -> HardwareProfile:
        """Return the current hardware profile preset."""
        return self._active_profile

    @property
    def is_enabled(self) -> bool:
        """Return True if local AI conversation is enabled in settings."""
        return self.settings.enabled

    def enable(self) -> None:
        """Enable local AI conversation."""
        self.settings.enabled = True

    def disable(self) -> None:
        """Disable local AI conversation."""
        self.settings.enabled = False

    def is_available(self) -> bool:
        """Check whether the model provider daemon is reachable and responding."""
        return self.provider.ping()

    def list_installed_models(self) -> list[ModelInfo]:
        """Discover all models currently installed in the local provider."""
        return self.registry.discover_installed(self.provider)

    def select_model(self, model_name: str) -> bool:
        """Select and activate an installed model safely.

        Follows an unload-first pattern to avoid GPU/RAM spikes:
        1. Validates target model presence before changing state.
        2. If switching away from an existing model, unloads the outgoing model
           and immediately clears active_model to None so an unloaded model is
           never reported as active.
        3. Transitions state to the target model. If activation fails, active_model
           remains None and ModelSwitchError is raised.
        4. A subsequent selection of an installed model recovers normally.
        """
        if not model_name or not isinstance(model_name, str):
            raise ModelSwitchError("Model name must be a non-empty string.")

        clean = model_name.strip()
        outgoing_model = self._active_model
        if outgoing_model == clean:
            return True

        # Step 1: Validate target model exists in local provider
        if not self.registry.is_installed(self.provider, clean):
            raise ModelNotFoundError(
                f"Model '{clean}' is not installed in the local provider. "
                f"Run 'ollama pull {clean}' in your terminal to install it first."
            )

        target_profile = self.registry.get_profile_for_model(clean)

        # Step 2: If switching away from an existing model, unload the outgoing model first
        if outgoing_model:
            try:
                self.provider.unload_model(outgoing_model)
            except Exception as exc:
                logger.debug("Non-fatal: failed to explicitly unload '%s': %s", outgoing_model, exc)
            self._active_model = None

        # Step 3: Transition state
        try:
            self._active_model = clean
            self.settings.selected_model = clean
            self._active_profile = target_profile
            self.settings.profile = target_profile.value
        except Exception as exc:
            self._active_model = None
            raise ModelSwitchError(f"Failed to activate target model '{clean}': {exc}") from exc

        logger.info("Activated model '%s' under profile '%s'.", clean, self._active_profile.value)
        return True

    def select_profile(self, profile: HardwareProfile) -> str | None:
        """Activate a hardware profile and select an installed model matching the tier."""
        self._active_profile = profile
        self.settings.profile = profile.value

        # Apply profile-tuned generation options to settings
        defaults = self.registry.get_profile_defaults(profile)
        self.settings.temperature = defaults.temperature
        self.settings.max_tokens = defaults.num_predict
        self.settings.context_window = defaults.num_ctx
        self.settings.keep_alive = defaults.keep_alive

        # Find first matching installed model from recommendations
        recommendations = self.registry.get_recommendations(profile)
        installed = [m.name for m in self.list_installed_models()]

        for rec in recommendations:
            for inst in installed:
                if inst == rec or (":" not in rec and inst.split(":")[0] == rec):
                    self.select_model(inst)
                    return inst

        return None

    def get_generation_options(self) -> GenerationOptions:
        """Build GenerationOptions from current settings."""
        return GenerationOptions(
            temperature=self.settings.temperature,
            num_predict=self.settings.max_tokens,
            num_ctx=self.settings.context_window,
            keep_alive=self.settings.keep_alive,
        )

    def generate_response(self, user_text: str) -> str | None:
        """Generate a conversational response from the active local model.

        Returns None if AI is disabled, no model is active, or provider is unreachable,
        enabling clean fallback to deterministic command handling.
        """
        if not self.is_enabled:
            return None

        if not self._active_model:
            # Auto-detect an installed model if none selected yet
            installed = self.list_installed_models()
            if installed:
                self.select_model(installed[0].name)
            else:
                return None

        # Add user message to history
        self.history.add_user_message(user_text)

        options = self.get_generation_options()

        try:
            result = self.provider.chat(
                messages=self.history.get_messages(),
                model=self._active_model,
                options=options,
            )

            if result.success and result.content:
                self.history.add_assistant_message(result.content)
                return result.content

            self.history.pop_last_user_message()
            return None

        except Exception as exc:
            logger.warning("Local AI generation failed for model '%s': %s", self._active_model, exc)
            self.history.pop_last_user_message()
            return None

    def unload_active_model(self) -> bool:
        """Request eviction of the active model from RAM/VRAM."""
        if not self._active_model:
            return False
        return self.provider.unload_model(self._active_model)

    def reset_history(self) -> None:
        """Clear the conversational chat history."""
        self.history.clear()
