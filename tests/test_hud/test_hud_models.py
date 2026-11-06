"""Unit tests for HUD domain models, events, and configuration."""

from dataclasses import FrozenInstanceError
import pytest

from app.hud.models import (
    HudErrorEvent,
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


def test_hud_status_enum_values() -> None:
    """Verify all required operational statuses are defined."""
    assert HudStatus.IDLE.value == "idle"
    assert HudStatus.LISTENING.value == "listening"
    assert HudStatus.PROCESSING.value == "processing"
    assert HudStatus.STOPPING.value == "stopping"
    assert HudStatus.ERROR.value == "error"


def test_hud_service_state_enum_values() -> None:
    """Verify all required lifecycle states are defined."""
    assert HudServiceState.STOPPED.value == "stopped"
    assert HudServiceState.STARTING.value == "starting"
    assert HudServiceState.RUNNING.value == "running"
    assert HudServiceState.STOPPING.value == "stopping"
    assert HudServiceState.ERROR.value == "error"


def test_input_type_enum_values() -> None:
    """Verify supported user input modalities."""
    assert InputType.TEXT.value == "text"
    assert InputType.VOICE.value == "voice"


def test_result_delivery_state_enum_values() -> None:
    """Verify delivery state classifications."""
    assert ResultDeliveryState.NONE.value == "none"
    assert ResultDeliveryState.DELIVERED.value == "delivered"
    assert ResultDeliveryState.RETAINED_UNDELIVERED.value == "retained_undelivered"


def test_voice_session_source_enum_values() -> None:
    """Verify supported microphone session sources."""
    assert VoiceSessionSource.GLOBAL_HOTKEY.value == "global_hotkey"
    assert VoiceSessionSource.HUD_BUTTON.value == "hud_button"
    assert VoiceSessionSource.SYSTEM_TRAY.value == "system_tray"


def test_event_immutability() -> None:
    """Verify that all HUD events are immutable frozen dataclasses."""
    status_evt = HudStatusChangedEvent(status=HudStatus.IDLE, details="All good")
    with pytest.raises(FrozenInstanceError):
        status_evt.status = HudStatus.PROCESSING  # type: ignore[misc]

    op_started = HudOperationStartedEvent(
        operation_id="op_1",
        input_type=InputType.TEXT,
        prompt="hello",
    )
    with pytest.raises(FrozenInstanceError):
        op_started.prompt = "changed"  # type: ignore[misc]

    op_done = HudOperationCompletedEvent(
        operation_id="op_1",
        success=True,
        message="Done",
    )
    with pytest.raises(FrozenInstanceError):
        op_done.success = False  # type: ignore[misc]

    err_evt = HudErrorEvent(
        operation_id="op_1",
        user_message="Failed",
        sanitized_details="details",
    )
    with pytest.raises(FrozenInstanceError):
        err_evt.user_message = "New error"  # type: ignore[misc]

    vis_evt = HudVisibilityChangedEvent(visible=True)
    with pytest.raises(FrozenInstanceError):
        vis_evt.visible = False  # type: ignore[misc]

    shutdown_evt = HudShutdownEvent()
    assert isinstance(shutdown_evt, HudShutdownEvent)


def test_operation_id_generation() -> None:
    """Verify operation ID format, uniqueness, and monotonicity."""
    id1 = generate_operation_id()
    id2 = generate_operation_id()

    assert id1.startswith("op_")
    assert id2.startswith("op_")
    assert id1 != id2

    # Verify ID can be sorted chronologically
    parts1 = id1.split("_")
    parts2 = id2.split("_")
    assert int(parts2[1]) >= int(parts1[1])


def test_hud_view_config_defaults() -> None:
    """Verify default view configuration values."""
    config = HudViewConfig()
    assert config.width == 420
    assert config.height == 320
    assert config.alpha == 0.95
    assert config.poll_interval_ms == 25
    assert config.always_on_top is True
    assert config.title == "Anaya HUD"


def test_hud_view_config_validation() -> None:
    """Verify boundary validation for HudViewConfig."""
    with pytest.raises(ValueError, match="width must be >= 200"):
        HudViewConfig(width=150)

    with pytest.raises(ValueError, match="height must be >= 150"):
        HudViewConfig(height=100)

    with pytest.raises(ValueError, match="alpha must be in"):
        HudViewConfig(alpha=0.05)

    with pytest.raises(ValueError, match="alpha must be in"):
        HudViewConfig(alpha=1.5)

    with pytest.raises(ValueError, match="poll_interval_ms must be >= 5"):
        HudViewConfig(poll_interval_ms=2)
