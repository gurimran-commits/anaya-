from collections.abc import Sequence
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
        self._scan_lock = threading.Lock()
        self._generation: int = 0

    @property
    def scan_lock(self) -> threading.Lock:
        """Lock for serializing external skill scan-and-register operations."""
        return self._scan_lock

    @property
    def generation(self) -> int:
        """Monotonic snapshot generation counter for external skills."""
        with self._lock:
            return self._generation

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

    def update_external_skills(
        self,
        external_skills: Sequence["Skill"],
        expected_generation: int | None = None,
    ) -> int:
        """Atomically replace all registered external skills with a new validated snapshot.

        Guarantees:
            - Thread-safe atomic replacement under self._lock.
            - Monotonic generation counter prevents stale snapshot publication.
            - Built-in skills (and non-external skills) remain untouched.
            - Preserves enabled/disabled state for external skills that remain present.
            - Cleans up state for external skills removed in the new snapshot.
            - Validates all input skills before modifying any registry state.
              If validation fails, raises without modifying existing state.

        Returns:
            The new snapshot generation counter.
        """
        new_map: dict[str, "Skill"] = {}
        for skill in external_skills:
            if not hasattr(skill, "metadata"):
                raise TypeError("External skill must provide a valid 'metadata' attribute.")
            metadata = skill.metadata
            skill_id = metadata.id
            if not isinstance(skill_id, str) or not skill_id.strip():
                raise ValueError("External skill ID must be a non-empty string.")
            clean_id = skill_id.strip()
            if not clean_id.startswith("user."):
                raise ValueError(f"External skill '{clean_id}' must start with 'user.' prefix.")
            if clean_id in new_map:
                raise ValueError(f"Duplicate external skill ID in snapshot: '{clean_id}'.")
            new_map[clean_id] = skill

        with self._lock:
            if expected_generation is not None and expected_generation != self._generation:
                raise ValueError(
                    f"Stale snapshot: expected generation {expected_generation}, "
                    f"current generation is {self._generation}."
                )

            # Check collision with non-external (built-in or host) skills
            for clean_id in new_map:
                existing = self._skills.get(clean_id)
                if existing is not None and not getattr(existing, "is_external", False):
                    raise ValueError(
                        f"External skill ID '{clean_id}' collides with registered non-external skill."
                    )

            # Identify old external skill IDs to remove
            old_external_ids = [
                s_id
                for s_id, s in self._skills.items()
                if getattr(s, "is_external", False) or s_id.startswith("user.")
            ]

            # Build new state dictionaries
            updated_skills = dict(self._skills)
            updated_enabled = dict(self._enabled_state)

            for old_id in old_external_ids:
                updated_skills.pop(old_id, None)
                if old_id not in new_map:
                    updated_enabled.pop(old_id, None)

            for clean_id, skill in new_map.items():
                updated_skills[clean_id] = skill
                if clean_id not in updated_enabled:
                    updated_enabled[clean_id] = skill.metadata.enabled

            # Atomic swap and generation advance
            self._generation += 1
            self._skills = updated_skills
            self._enabled_state = updated_enabled
            return self._generation
