"""Floating Assistant HUD package for Anaya desktop integration."""

from app.hud.coordinator import VoiceLease, VoiceSessionCoordinator
from app.hud.models import (
    HudErrorEvent,
    HudEvent,
    HudOperationCompletedEvent,
    HudOperationStartedEvent,
    HudServiceState,
    HudShutdownEvent,
    HudStatus,
    HudStatusChangedEvent,
    HudViewConfig,
    HudVisibilityChangedEvent,
    InputType,
    ResultDeliveryState,
    VoiceSessionSource,
    generate_operation_id,
)
from app.hud.queue import HudEventMailbox
from app.hud.service import FloatingHudService

__all__ = [
    "FloatingHudService",
    "HudEventMailbox",
    "HudErrorEvent",
    "HudEvent",
    "HudOperationCompletedEvent",
    "HudOperationStartedEvent",
    "HudServiceState",
    "HudShutdownEvent",
    "HudStatus",
    "HudStatusChangedEvent",
    "HudViewConfig",
    "HudVisibilityChangedEvent",
    "InputType",
    "ResultDeliveryState",
    "VoiceLease",
    "VoiceSessionCoordinator",
    "VoiceSessionSource",
    "generate_operation_id",
]
