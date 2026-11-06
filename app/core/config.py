import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.skills.models import SkillPermission


class FrozenDict(dict[str, tuple[str, ...]]):
    """Immutable dictionary preserving standard dict interface while rejecting mutation.

    Security Note:
        Protects against ordinary application-level mutation (e.g. indexing, updates,
        operators, clearing). Like all Python built-in subclasses, unbound descriptor calls
        on the base class (e.g. dict.__setitem__) are bypassed at C level; runtime security
        is enforced via SkillExecutor's defensive snapshot copy.
    """

    def __setitem__(self, key: Any, value: Any) -> None:
        raise TypeError(f"'{type(self).__name__}' object does not support item assignment")

    def __delitem__(self, key: Any) -> None:
        raise TypeError(f"'{type(self).__name__}' object does not support item deletion")

    def __ior__(self, other: Any) -> "FrozenDict":
        raise TypeError(
            f"'{type(self).__name__}' object does not support in-place mutation"
        )

    def clear(self) -> None:
        raise TypeError(f"'{type(self).__name__}' object does not support clearing")

    def pop(self, *args: Any, **kwargs: Any) -> Any:
        raise TypeError(f"'{type(self).__name__}' object does not support popping")

    def popitem(self) -> Any:
        raise TypeError(f"'{type(self).__name__}' object does not support popping items")

    def setdefault(self, *args: Any, **kwargs: Any) -> Any:
        raise TypeError(f"'{type(self).__name__}' object does not support setdefault")

    def update(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError(f"'{type(self).__name__}' object does not support updating")

    def __copy__(self) -> "FrozenDict":
        return self

    def __deepcopy__(self, memo: Any) -> "FrozenDict":
        return self

    def __reduce__(self) -> tuple[Any, ...]:
        return (FrozenDict, (dict(self),))


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

    # Task & reminder settings.
    task_scheduling_enabled: bool = True
    task_scheduler_poll_interval: float = 15.0
    task_recover_missed_reminders: bool = True

    # Global hotkey settings (Phase 9B).
    hotkey_enabled: bool = False
    hotkey_combination: str = "win+alt+a"
    hotkey_voice_duration: float = 5.0

    # Skill capability grants mapping skill_id to tuple of granted permission names.
    skill_permission_grants: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.hotkey_enabled, bool):
            raise TypeError(
                f"hotkey_enabled must be a boolean, got {type(self.hotkey_enabled).__name__}."
            )

        if isinstance(self.hotkey_voice_duration, bool) or not isinstance(
            self.hotkey_voice_duration, (int, float)
        ):
            raise TypeError(
                f"hotkey_voice_duration must be a float or int, got {type(self.hotkey_voice_duration).__name__}."
            )

        if not math.isfinite(self.hotkey_voice_duration):
            raise ValueError(
                f"hotkey_voice_duration must be a finite number, got {self.hotkey_voice_duration}."
            )

        if self.hotkey_voice_duration <= 0.0 or self.hotkey_voice_duration > 60.0:
            raise ValueError(
                f"hotkey_voice_duration must be positive and at most 60.0s, got {self.hotkey_voice_duration}."
            )

        object.__setattr__(
            self, "hotkey_voice_duration", float(self.hotkey_voice_duration)
        )

        if not isinstance(self.hotkey_combination, str):
            raise TypeError(
                f"hotkey_combination must be a string, got {type(self.hotkey_combination).__name__}."
            )

        from app.hotkey.models import HotkeyCombination

        parsed_combo = HotkeyCombination.parse(self.hotkey_combination)
        object.__setattr__(self, "hotkey_combination", parsed_combo.canonical_string)

        if not isinstance(self.skill_permission_grants, (dict, Mapping)):
            raise TypeError("skill_permission_grants must be a dictionary or mapping.")

        valid_permissions = {p.value for p in SkillPermission}
        normalized: dict[str, tuple[str, ...]] = {}

        for skill_id, perms in self.skill_permission_grants.items():
            if not isinstance(skill_id, str) or not skill_id.strip():
                raise ValueError("Skill permission grant key must be a non-empty string.")
            if skill_id != skill_id.strip():
                raise ValueError(
                    f"Skill permission grant key '{skill_id}' must not contain surrounding whitespace."
                )

            if isinstance(perms, (str, bytes)) or not hasattr(perms, "__iter__"):
                raise ValueError(
                    f"Skill permission grant values for '{skill_id}' must be a sequence of permission names."
                )

            parsed_perms: list[str] = []
            for p in perms:
                if isinstance(p, SkillPermission):
                    p_val = p.value
                elif isinstance(p, str):
                    p_val = p.strip().lower()
                else:
                    raise ValueError(
                        f"Invalid permission value {p!r} for skill '{skill_id}'."
                    )

                if p_val not in valid_permissions:
                    raise ValueError(
                        f"Unknown skill permission '{p}' for skill '{skill_id}'. "
                        f"Valid permissions are: {sorted(valid_permissions)}."
                    )
                parsed_perms.append(p_val)

            normalized[skill_id] = tuple(parsed_perms)

        object.__setattr__(self, "skill_permission_grants", FrozenDict(normalized))


DEFAULT_CONFIG = AppConfig()
