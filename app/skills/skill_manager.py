from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from app.skills.models import Skill, SkillMetadata, SkillPermission, SkillResult

if TYPE_CHECKING:
    from app.security.approval_service import ApprovalService
    from app.skills.skill_executor import SkillExecutor
    from app.skills.skill_registry import SkillRegistry


class SkillManager:
    """High-level facade for discovering, querying, and executing modular skills."""

    def __init__(
        self,
        registry: "SkillRegistry | None" = None,
        executor: "SkillExecutor | None" = None,
        approval_service: "ApprovalService | None" = None,
        skill_permission_grants: Mapping[str, tuple[str, ...]] | None = None,
    ) -> None:
        if registry is None:
            from app.skills.skill_registry import SkillRegistry

            registry = SkillRegistry()

        if executor is None:
            from app.skills.skill_executor import SkillExecutor

            executor = SkillExecutor(
                registry=registry,
                approval_service=approval_service,
                skill_permission_grants=skill_permission_grants,
            )

        self._registry = registry
        self._executor = executor

    @property
    def registry(self) -> "SkillRegistry":
        """Expose the underlying registry."""
        return self._registry

    @property
    def executor(self) -> "SkillExecutor":
        """Expose the underlying executor."""
        return self._executor

    def get_granted_permissions(self, skill_id: str) -> frozenset[SkillPermission]:
        """Return the granted permissions for a specific skill ID."""
        return self._executor.get_granted_permissions(skill_id)

    def register_skill(self, skill: Skill) -> None:
        """Register a new trusted skill."""
        self._registry.register(skill)

    def unregister_skill(self, skill_id: str) -> bool:
        """Unregister a skill by ID."""
        return self._registry.unregister(skill_id)

    def get_skill(self, skill_id: str) -> Skill | None:
        """Retrieve a registered skill by ID."""
        return self._registry.get(skill_id)

    def get_metadata(self, skill_id: str) -> SkillMetadata | None:
        """Retrieve metadata for a skill by ID."""
        skill = self._registry.get(skill_id)
        if skill is None:
            return None
        return skill.metadata

    def is_enabled(self, skill_id: str) -> bool:
        """Check whether a skill is registered and enabled."""
        return self._registry.is_enabled(skill_id)

    def enable_skill(self, skill_id: str) -> bool:
        """Enable a registered skill."""
        return self._registry.enable(skill_id)

    def disable_skill(self, skill_id: str) -> bool:
        """Disable a registered skill."""
        return self._registry.disable(skill_id)

    def list_skills(self, enabled_only: bool = True) -> tuple[Skill, ...]:
        """List registered skills (enabled by default)."""
        return self._registry.list_skills(enabled_only=enabled_only)

    def discover_skills(self) -> list[dict[str, Any]]:
        """Return a structured summary of available enabled skills."""
        skills = self.list_skills(enabled_only=True)
        summary = []
        for s in skills:
            meta = s.metadata
            summary.append(
                {
                    "id": meta.id,
                    "name": meta.name,
                    "description": meta.description,
                    "version": meta.version,
                    "permissions": [p.value for p in meta.required_permissions],
                    "requires_approval": meta.requires_approval,
                    "inputs": [
                        {
                            "name": inp.name,
                            "type": inp.type.__name__,
                            "required": inp.required,
                            "default": inp.default,
                            "description": inp.description,
                        }
                        for inp in meta.inputs
                    ],
                }
            )
        return summary

    def validate_inputs(
        self,
        skill_id: str,
        inputs: dict[str, Any] | None,
    ) -> tuple[bool, dict[str, Any], str | None]:
        """Validate proposed inputs against a skill's declared contract."""
        skill = self._registry.get(skill_id)
        if skill is None:
            return False, {}, f"Skill '{skill_id}' is not registered."
        return self._executor.validate_inputs(skill, inputs)

    def execute_skill(
        self,
        skill_id: str,
        inputs: dict[str, Any] | None = None,
        confirmation_id: str | None = None,
    ) -> SkillResult:
        """Execute a skill via the controlled executor."""
        return self._executor.execute(
            skill_id=skill_id,
            inputs=inputs,
            confirmation_id=confirmation_id,
        )
