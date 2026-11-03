"""Curated model catalog and installed-model discovery for Anaya."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from app.ai.models import GenerationOptions, HardwareProfile, ModelInfo

if TYPE_CHECKING:
    from app.ai.protocols import ModelProvider


# Curated catalog mapping hardware profiles to recommended model tags
RECOMMENDED_CATALOG: dict[HardwareProfile, list[str]] = {
    HardwareProfile.LIGHTWEIGHT: [
        "qwen2.5:0.5b",
        "llama3.2:1b",
    ],
    HardwareProfile.BALANCED: [
        "qwen2.5:1.5b",
        "llama3.2:3b",
        "gemma2:2b",
    ],
    HardwareProfile.ADVANCED: [
        "qwen2.5:7b",
        "mistral:7b",
        "phi-3-mini:3.8b",
    ],
}

# Profile-tuned generation defaults
PROFILE_DEFAULTS: dict[HardwareProfile, GenerationOptions] = {
    HardwareProfile.LIGHTWEIGHT: GenerationOptions(
        temperature=0.7,
        num_predict=256,
        num_ctx=2048,
        keep_alive="2m",
    ),
    HardwareProfile.BALANCED: GenerationOptions(
        temperature=0.7,
        num_predict=512,
        num_ctx=4096,
        keep_alive="5m",
    ),
    HardwareProfile.ADVANCED: GenerationOptions(
        temperature=0.7,
        num_predict=1024,
        num_ctx=8192,
        keep_alive="15m",
    ),
    HardwareProfile.CUSTOM: GenerationOptions(
        temperature=0.7,
        num_predict=512,
        num_ctx=2048,
        keep_alive="5m",
    ),
}


class ModelRegistry:
    """Catalog of recommended models and discoverer of installed provider models."""

    def __init__(
        self,
        custom_catalog: dict[HardwareProfile, list[str]] | None = None,
    ) -> None:
        self._catalog = custom_catalog or RECOMMENDED_CATALOG

    def get_recommendations(self, profile: HardwareProfile | None = None) -> list[str]:
        """Return recommended models for a given profile, or all recommended models."""
        if profile is not None:
            return list(self._catalog.get(profile, []))
        all_models: list[str] = []
        for models in self._catalog.values():
            all_models.extend(models)
        return all_models

    def get_profile_defaults(self, profile: HardwareProfile) -> GenerationOptions:
        """Return tuned generation defaults for a hardware profile."""
        return PROFILE_DEFAULTS.get(profile, PROFILE_DEFAULTS[HardwareProfile.BALANCED])

    def get_profile_for_model(self, model_name: str) -> HardwareProfile:
        """Classify a model name or tag into a recommended hardware profile."""
        clean = model_name.strip().lower()

        # 1. Exact catalog match
        for profile, models in self._catalog.items():
            if clean in models:
                return profile

        # 2. Infer based on parameter size tag (e.g. 0.5b, 1b, 1.5b, 3b, 7b)
        param_match = re.search(r"[:\-_](\d+(?:\.\d+)?)[b]\b", clean)
        if param_match:
            try:
                size_num = float(param_match.group(1))
                if size_num <= 1.2:
                    return HardwareProfile.LIGHTWEIGHT
                if size_num <= 3.5:
                    return HardwareProfile.BALANCED
                if size_num <= 8.0:
                    return HardwareProfile.ADVANCED
            except ValueError:
                pass

        # 3. Base model family match if tag is missing or generic (e.g. "qwen2.5")
        base_name = clean.split(":")[0]
        for profile, models in self._catalog.items():
            for m in models:
                if m.split(":")[0] == base_name:
                    return profile

        return HardwareProfile.BALANCED

    def discover_installed(self, provider: ModelProvider) -> list[ModelInfo]:
        """Query the provider for currently installed models without triggering any downloads."""
        try:
            return provider.list_models()
        except Exception:
            return []

    def is_installed(self, provider: ModelProvider, model_name: str) -> bool:
        """Check if a specific model tag is installed in the provider."""
        clean = model_name.strip().lower()
        installed = self.discover_installed(provider)
        for info in installed:
            installed_name = info.name.lower()
            if installed_name == clean or installed_name.split(":")[0] == clean:
                return True
        return False
