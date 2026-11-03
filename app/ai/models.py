"""Data models, settings, and hardware profiles for local AI model switching."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class HardwareProfile(Enum):
    """Target hardware profiles with recommended model parameter tiers."""

    LIGHTWEIGHT = "lightweight"  # i3 / 4 GB RAM: 0.5B - 1B
    BALANCED = "balanced"        # Standard PC / 8-16 GB: 1.5B - 3B
    ADVANCED = "advanced"        # Capable PC / GPU: 3.8B - 7B
    CUSTOM = "custom"            # Custom user-defined parameters

    @classmethod
    def from_str(cls, val: str | None) -> HardwareProfile:
        """Convert string to HardwareProfile with case-insensitive fallback."""
        if not val or not isinstance(val, str):
            return cls.BALANCED
        normalized = val.strip().lower()
        for member in cls:
            if member.value == normalized:
                return member
        return cls.BALANCED


class AIError(Exception):
    """Base exception for all AI-related errors."""


class ModelProviderError(AIError):
    """Exception raised when an AI provider fails during an operation."""


class ModelNotFoundError(AIError):
    """Exception raised when a requested model is not found in the local provider."""


class ModelConnectionError(AIError):
    """Exception raised when connection to the local provider daemon fails."""


class ModelTimeoutError(AIError):
    """Exception raised when a provider call times out."""


class ModelSwitchError(AIError):
    """Exception raised when a model switch operation fails."""


class ModelValidationError(AIError):
    """Exception raised when options or configuration settings are invalid."""


@dataclass(frozen=True)
class ChatMessage:
    """Immutable chat message for conversation turns."""

    role: str
    content: str

    def __post_init__(self) -> None:
        valid_roles = {"system", "user", "assistant"}
        if self.role not in valid_roles:
            raise ModelValidationError(
                f"Invalid chat message role '{self.role}'. Must be one of: {sorted(valid_roles)}."
            )
        if not isinstance(self.content, str):
            raise ModelValidationError("ChatMessage content must be a string.")


@dataclass(frozen=True)
class ModelDetails:
    """Architectural metadata for a locally installed model."""

    family: str = ""
    parameter_size: str = ""
    quantization_level: str = ""
    context_length: int | None = None


@dataclass(frozen=True)
class ModelInfo:
    """Summary information for an installed local model."""

    name: str
    size_bytes: int = 0
    modified_at: str | None = None
    details: ModelDetails = field(default_factory=ModelDetails)

    def __post_init__(self) -> None:
        if not self.name or not isinstance(self.name, str):
            raise ModelValidationError("ModelInfo name must be a non-empty string.")


@dataclass(frozen=True)
class GenerationOptions:
    """Inference options passed to the model provider."""

    temperature: float = 0.7
    num_predict: int = 512
    num_ctx: int = 2048
    top_k: int = 40
    top_p: float = 0.9
    keep_alive: str = "5m"

    def __post_init__(self) -> None:
        if not (0.0 <= self.temperature <= 2.0):
            raise ModelValidationError(
                f"temperature must be between 0.0 and 2.0, got {self.temperature}."
            )
        if self.num_predict <= 0:
            raise ModelValidationError(
                f"num_predict must be positive, got {self.num_predict}."
            )
        if self.num_ctx < 256:
            raise ModelValidationError(
                f"num_ctx must be at least 256, got {self.num_ctx}."
            )
        if not (0.0 <= self.top_p <= 1.0):
            raise ModelValidationError(
                f"top_p must be between 0.0 and 1.0, got {self.top_p}."
            )


@dataclass(frozen=True)
class GenerationResult:
    """Result returned by a model provider generation call."""

    success: bool
    content: str
    model: str
    total_duration_ns: int | None = None
    eval_count: int | None = None
    error_message: str | None = None


@dataclass
class ModelSettings:
    """Persistent configuration for AI model switching and generation settings."""

    enabled: bool = True
    provider: str = "ollama"
    selected_model: str | None = None
    profile: str = HardwareProfile.BALANCED.value
    base_url: str = "http://127.0.0.1:11434"
    context_window: int = 2048
    max_tokens: int = 512
    temperature: float = 0.7
    keep_alive: str = "5m"
    timeout_seconds: float = 30.0
    max_history_turns: int = 10

    def validate(self) -> None:
        """Validate settings integrity."""
        if not self.base_url or not isinstance(self.base_url, str):
            raise ModelValidationError("base_url must be a valid URL string.")
        if self.context_window < 256:
            raise ModelValidationError("context_window must be at least 256.")
        if self.max_tokens <= 0:
            raise ModelValidationError("max_tokens must be positive.")
        if not (0.0 <= self.temperature <= 2.0):
            raise ModelValidationError("temperature must be between 0.0 and 2.0.")
        if self.timeout_seconds <= 0:
            raise ModelValidationError("timeout_seconds must be positive.")
        if self.max_history_turns <= 0:
            raise ModelValidationError("max_history_turns must be positive.")

    def to_dict(self) -> dict[str, Any]:
        """Convert settings to a dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ModelSettings:
        """Instantiate settings from a dictionary, using defaults for missing fields."""
        if not isinstance(data, dict):
            return cls()

        valid_fields = cls.__dataclass_fields__.keys()
        filtered = {k: v for k, v in data.items() if k in valid_fields}

        settings = cls(**filtered)
        try:
            settings.validate()
        except ModelValidationError:
            return cls()
        return settings

    def save(self, path: Path | str | None = None) -> None:
        """Save settings to disk."""
        target_path = Path(path) if path else self.get_default_path()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        self.validate()
        with open(target_path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    def load(cls, path: Path | str | None = None) -> ModelSettings:
        """Load settings from disk, falling back to safe defaults if missing or invalid."""
        target_path = Path(path) if path else cls.get_default_path()
        if not target_path.exists():
            return cls()
        try:
            with open(target_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return cls.from_dict(data)
        except Exception:
            return cls()

    @staticmethod
    def get_default_path() -> Path:
        """Return the standard settings file path ~/.anaya/ai_settings.json."""
        return Path.home() / ".anaya" / "ai_settings.json"
