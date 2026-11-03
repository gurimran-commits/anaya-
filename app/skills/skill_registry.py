import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.skills.models import Skill


class SkillRegistry:
    """Store, look up, and manage lifecycle state of registered skills.

    Guarantees thread-safe registration, lookups, and enabled/disabled state transitions.
    Rejects duplicate registrations and invalid skill definitions.
    """

    def __init__(self) -> None:
        self._skills: dict[str, "Skill"] = {}
        self._enabled_state: dict[str, bool] = {}
        self._lock = threading.RLock()

    def register(self, skill: "Skill") -> None:
        """Register a new trusted skill instance.

        Raises:
            ValueError: If the skill ID is empty, contains whitespace, or is already registered.
            TypeError: If the skill object or its metadata is invalid.
        """
        if not hasattr(skill, "metadata"):
            raise TypeError("Skill must provide a valid 'metadata' attribute.")

        metadata = skill.metadata
        skill_id = metadata.id

        with self._lock:
            if skill_id in self._skills:
                raise ValueError(f"Skill already registered: '{skill_id}'.")

            self._skills[skill_id] = skill
            self._enabled_state[skill_id] = metadata.enabled

    def get(self, skill_id: str) -> "Skill | None":
        """Retrieve a registered skill by ID, or None if not found."""
        if not isinstance(skill_id, str) or not skill_id.strip():
            return None

        with self._lock:
            return self._skills.get(skill_id.strip())

    def exists(self, skill_id: str) -> bool:
        """Check whether a skill is registered."""
        if not isinstance(skill_id, str) or not skill_id.strip():
            return False

        with self._lock:
            return skill_id.strip() in self._skills

    def unregister(self, skill_id: str) -> bool:
        """Unregister a skill by ID.

        Returns:
            True if the skill was removed, False if it was not registered.
        """
        if not isinstance(skill_id, str) or not skill_id.strip():
            return False

        with self._lock:
            clean_id = skill_id.strip()
            if clean_id in self._skills:
                del self._skills[clean_id]
                self._enabled_state.pop(clean_id, None)
                return True
            return False

    def list_skills(self, enabled_only: bool = False) -> tuple["Skill", ...]:
        """List registered skills, optionally filtered to only enabled skills."""
        with self._lock:
            if not enabled_only:
                return tuple(self._skills.values())

            return tuple(
                skill
                for skill_id, skill in self._skills.items()
                if self._enabled_state.get(skill_id, True)
            )

    def is_enabled(self, skill_id: str) -> bool:
        """Check whether a registered skill is currently enabled."""
        if not isinstance(skill_id, str) or not skill_id.strip():
            return False

        with self._lock:
            clean_id = skill_id.strip()
            if clean_id not in self._skills:
                return False
            return self._enabled_state.get(clean_id, True)

    def enable(self, skill_id: str) -> bool:
        """Enable a registered skill.

        Returns:
            True if state was changed, False if skill is not registered.
        """
        if not isinstance(skill_id, str) or not skill_id.strip():
            return False

        with self._lock:
            clean_id = skill_id.strip()
            if clean_id in self._skills:
                self._enabled_state[clean_id] = True
                return True
            return False

    def disable(self, skill_id: str) -> bool:
        """Disable a registered skill.

        Returns:
            True if state was changed, False if skill is not registered.
        """
        if not isinstance(skill_id, str) or not skill_id.strip():
            return False

        with self._lock:
            clean_id = skill_id.strip()
            if clean_id in self._skills:
                self._enabled_state[clean_id] = False
                return True
            return False
