"""Protocol definitions for local AI model providers."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.ai.models import ChatMessage, GenerationOptions, GenerationResult, ModelDetails, ModelInfo


@runtime_checkable
class ModelProvider(Protocol):
    """Protocol for local model execution providers (e.g., Ollama)."""

    def ping(self) -> bool:
        """Check if the local provider daemon is responsive."""
        ...

    def list_models(self) -> list[ModelInfo]:
        """List all locally installed models."""
        ...

    def get_model_details(self, model_name: str) -> ModelDetails | None:
        """Retrieve technical details for a specific installed model."""
        ...

    def chat(
        self,
        messages: list[ChatMessage],
        model: str,
        options: GenerationOptions | None = None,
    ) -> GenerationResult:
        """Generate a chat response from the requested model."""
        ...

    def unload_model(self, model_name: str) -> bool:
        """Request the provider to evict a model from memory (RAM/VRAM)."""
        ...
