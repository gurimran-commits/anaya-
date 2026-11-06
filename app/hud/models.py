"""Domain models and event definitions for the Floating Assistant HUD."""

from dataclasses import dataclass
from enum import Enum
import time
import uuid


class HudStatus(str, Enum):
    """Operational status displayed on the HUD interface."""

    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING = "processing"
    STOPPING = "stopping"
    ERROR = "error"


class HudServiceState(str, Enum):
    """Lifecycle states of the FloatingHudService facade."""

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"


class InputType(str, Enum):
    """Input modality through which an operation was requested."""

    TEXT = "text"
    VOICE = "voice"


class ResultDeliveryState(str, Enum):
    """Terminal result delivery state within the event mailbox."""

    NONE = "none"
    DELIVERED = "delivered"
    RETAINED_UNDELIVERED = "retained_undelivered"


class VoiceSessionSource(str, Enum):
    """Input sources capable of acquiring a voice microphone session."""

    GLOBAL_HOTKEY = "global_hotkey"
    HUD_BUTTON = "hud_button"
    SYSTEM_TRAY = "system_tray"


def generate_operation_id() -> str:
    """Generate a monotonic, unique correlation identifier for a user operation."""
    return f"op_{int(time.monotonic() * 1000)}_{uuid.uuid4().hex[:8]}"


# ============================================================================
# Typed UI Events (Immutable Frozen Dataclasses)
# ============================================================================


@dataclass(frozen=True)
class HudStatusChangedEvent:
    """Indicates an update to the HUD visual operational status."""

    status: HudStatus
    details: str = ""


@dataclass(frozen=True)
class HudVisibilityChangedEvent:
    """Requests a change in the HUD window visibility state."""

    visible: bool


@dataclass(frozen=True)
class HudOperationStartedEvent:
    """Signals that a user operation has commenced."""

    operation_id: str
    input_type: InputType
    prompt: str


@dataclass(frozen=True)
class HudOperationCompletedEvent:
    """Terminal outcome of an authorized user operation."""

    operation_id: str
    success: bool
    message: str


@dataclass(frozen=True)
class HudErrorEvent:
    """Terminal error condition occurring during operation processing."""

    operation_id: str | None
    user_message: str
    sanitized_details: str = ""


@dataclass(frozen=True)
class HudShutdownEvent:
    """Signals the GUI thread to break its event loop and destroy Tk resources."""

    pass


HudEvent = (
    HudStatusChangedEvent
    | HudVisibilityChangedEvent
    | HudOperationStartedEvent
    | HudOperationCompletedEvent
    | HudErrorEvent
    | HudShutdownEvent
)


# ============================================================================
# HUD View Configuration
# ============================================================================


@dataclass(frozen=True)
class HudViewConfig:
    """Configuration options governing HUD window appearance and polling."""

    width: int = 420
    height: int = 320
    alpha: float = 0.95
    poll_interval_ms: int = 25
    always_on_top: bool = True
    title: str = "Anaya HUD"

    def __post_init__(self) -> None:
        if self.width < 200:
            raise ValueError(f"HUD width must be >= 200, got {self.width}")
        if self.height < 150:
            raise ValueError(f"HUD height must be >= 150, got {self.height}")
        if not (0.1 <= self.alpha <= 1.0):
            raise ValueError(f"HUD alpha must be in [0.1, 1.0], got {self.alpha}")
        if self.poll_interval_ms < 5:
            raise ValueError(f"HUD poll_interval_ms must be >= 5, got {self.poll_interval_ms}")
