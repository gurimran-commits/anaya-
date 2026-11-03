from dataclasses import dataclass


@dataclass(frozen=True)
class AppConfig:
    """Default configuration for the Anaya assistant."""

    app_name: str = "Anaya"
    version: str = "0.1.0"

    # Internet access is disabled by default.
    internet_enabled: bool = False

    # Speech settings.
    tts_engine: str = "kokoro"
    tts_language: str = "en"
    tts_voice: str = "af_heart"
    speech_sample_rate: int = 24000

    # Interaction settings.
    push_to_talk: bool = True
    require_confirmation: bool = True

    # Local AI settings.
    ai_enabled: bool = True
    ai_provider: str = "ollama"
    ai_profile: str = "balanced"


DEFAULT_CONFIG = AppConfig()
