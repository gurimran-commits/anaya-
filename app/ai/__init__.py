"""Anaya Local AI Model Switcher module."""

from app.ai.history import ConversationHistory
from app.ai.manager import ModelManager
from app.ai.models import (
    AIError,
    ChatMessage,
    GenerationOptions,
    GenerationResult,
    HardwareProfile,
    ModelConnectionError,
    ModelDetails,
    ModelInfo,
    ModelNotFoundError,
    ModelProviderError,
    ModelSettings,
    ModelSwitchError,
    ModelTimeoutError,
    ModelValidationError,
)
from app.ai.protocols import ModelProvider
from app.ai.providers.ollama import OllamaProvider
from app.ai.registry import ModelRegistry

__all__ = [
    "AIError",
    "ChatMessage",
    "ConversationHistory",
    "GenerationOptions",
    "GenerationResult",
    "HardwareProfile",
    "ModelConnectionError",
    "ModelDetails",
    "ModelInfo",
    "ModelManager",
    "ModelNotFoundError",
    "ModelProvider",
    "ModelProviderError",
    "ModelRegistry",
    "ModelSettings",
    "ModelSwitchError",
    "ModelTimeoutError",
    "ModelValidationError",
    "OllamaProvider",
]
