from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SkillPermission(str, Enum):
    """Permissions a skill may declare or require."""

    NONE = "none"
    READ_ONLY = "read_only"
    FILESYSTEM = "filesystem"
    APPROVAL_REQUIRED = "approval_required"


class SkillStatus(str, Enum):
    """Operational status of a registered skill."""

    ENABLED = "enabled"
    DISABLED = "disabled"


@dataclass(frozen=True)
class InputFieldSpec:
    """Contract specification for a single skill input parameter."""

    name: str
    type: type = str
    required: bool = False
    default: Any = None
    description: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Input field name must be a non-empty string.")
        if self.name != self.name.strip():
            raise ValueError("Input field name must not contain surrounding whitespace.")
        if not isinstance(self.type, type):
            raise ValueError("Input field type must be a valid Python type.")

    def validate(self, value: Any) -> tuple[bool, str | None]:
        """Validate an argument value against this field's contract."""
        if value is None:
            if self.required:
                return False, f"Missing required input field: '{self.name}'."
            return True, None

        # Type checking: bool is a subclass of int in Python, enforce exact bool check if expected
        if self.type is int and isinstance(value, bool):
            return False, f"Field '{self.name}' expected int, got bool."

        if not isinstance(value, self.type):
            return False, (
                f"Field '{self.name}' expected type '{self.type.__name__}', "
                f"got '{type(value).__name__}'."
            )

        return True, None


@dataclass(frozen=True)
class SkillMetadata:
    """Immutable metadata describing a registered skill."""

    id: str
    name: str
    description: str
    version: str = "1.0.0"
    inputs: tuple[InputFieldSpec, ...] = ()
    required_permissions: tuple[SkillPermission, ...] = (SkillPermission.NONE,)
    enabled: bool = True
    requires_approval: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("Skill ID must be a non-empty string.")
        if self.id != self.id.strip():
            raise ValueError("Skill ID must not contain surrounding whitespace.")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Skill display name must be a non-empty string.")
        if not isinstance(self.description, str):
            raise ValueError("Skill description must be a string.")
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("Skill version must be a non-empty string.")


@dataclass(frozen=True)
class SkillResult:
    """Consistent result contract returned by skill execution."""

    success: bool
    data: Any = None
    message: str = ""
    error: str | None = None
    confirmation_required: bool = False
    confirmation_id: str | None = None


class Skill:
    """Base class for all modular skills in Anaya."""

    metadata: SkillMetadata

    def execute(self, inputs: dict[str, Any]) -> SkillResult:
        """Execute the skill with validated inputs and return a SkillResult."""
        raise NotImplementedError("Subclasses of Skill must implement execute().")
