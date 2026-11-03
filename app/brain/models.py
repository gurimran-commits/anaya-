from dataclasses import dataclass
from enum import Enum, auto


class IntentStatus(Enum):
    """Classification of parsed intent resolution."""

    VALID = auto()
    MISSING_ARGUMENT = auto()
    AMBIGUOUS = auto()
    UNSUPPORTED = auto()
    INVALID = auto()
    CANCELLED = auto()
    RESET_CONTEXT = auto()


@dataclass(frozen=True)
class CommandIntent:
    """Explicit command invocation representation."""

    name: str
    arguments: tuple[str, ...]


@dataclass(frozen=True)
class IntentResolution:
    """Detailed resolution result from the natural language parser."""

    status: IntentStatus
    intent: CommandIntent | None = None
    message: str | None = None
    target: str | None = None
    directory_index: str = "0"
    missing_argument_name: str | None = None
