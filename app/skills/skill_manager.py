from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.skills.discovery import (
    RejectedPackage,
    SkillDiscoveryResult,
    discover_external_skills,
    get_default_skills_root,
)
from app.skills.models import Skill, SkillMetadata, SkillPermission, SkillResult

if TYPE_CHECKING:
    from app.security.approval_service import ApprovalService
    from app.skills.skill_executor import SkillExecutor
    from app.skills.skill_registry import SkillRegistry
    from app.skills.supervisor import SkillProcessSupervisor

logger = logging.getLogger("anaya.skills")


@dataclass(frozen=True)
class SkillRefreshResult:
    """Structured result of an external skill refresh operation."""

    success: bool
    added: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    unmodified: tuple[str, ...] = ()
    rejected: tuple[RejectedPackage, ...] = ()
    retained_last_known_good: tuple[str, ...] = ()
    generation: int = 0
    error: str | None = None

    @property
    def total_active(self) -> int:
        """Total number of active external skills in the refreshed registry snapshot."""
        return len(self.added) + len(self.updated) + len(self.unmodified)


class SkillManager:
    """High-level facade for discovering, querying, and executing modular skills."""

    def __init__(
        self,
        registry: "SkillRegistry | None" = None,
        executor: "SkillExecutor | None" = None,
        approval_service: "ApprovalService | None" = None,
        skill_permission_grants: Mapping[str, tuple[str, ...]] | None = None,
        supervisor: "SkillProcessSupervisor | None" = None,
        isolated_skill_ids: set[str] | frozenset[str] | None = None,
        skills_root: Path | str | None = None,
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
                supervisor=supervisor,
                isolated_skill_ids=isolated_skill_ids,
            )

        self._registry = registry
        self._executor = executor
        self._supervisor = supervisor

        if skills_root is not None:
            self._skills_root = Path(skills_root).resolve()
        elif supervisor is not None and getattr(supervisor, "skills_root", None) is not None:
            self._skills_root = supervisor.skills_root
        else:
            self._skills_root = get_default_skills_root().resolve()

    @property
    def skills_root(self) -> Path:
        """Configured trusted skills root directory for external skills."""
        return self._skills_root

    @property
    def supervisor(self) -> "SkillProcessSupervisor | None":
        """Expose the optional supervisor."""
        return self._supervisor

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

    def update_external_skills(
        self,
        external_skills: Sequence[Skill],
        expected_generation: int | None = None,
    ) -> int:
        """Atomically replace external skills in the underlying registry."""
        return self._registry.update_external_skills(
            external_skills,
            expected_generation=expected_generation,
        )

    def refresh_skills(self, root_path: Path | str | None = None) -> SkillRefreshResult:
        """Scan skills root directory and dynamically refresh registered external skills.

        Guarantees:
            - Thread-safe serialization via registry.scan_lock.
            - Monotonic generation check prevents race conditions and stale publication.
            - Root-level access/security failures preserve the last-known-good registry.
            - Previously registered packages with malformed updates retain their last-known-good proxy.
            - Unregistered/new malformed packages are rejected and never registered.
            - Truly absent packages are removed only after a successful, complete scan.
            - Detailed delta reporting: added, updated, removed, unmodified, rejected.
        """
        effective_root = Path(root_path).resolve() if root_path is not None else self._skills_root

        with self._registry.scan_lock:
            start_gen = self._registry.generation
            prev_external = self._registry.get_external_skills()

            try:
                discovery_result = discover_external_skills(root_path=effective_root)
            except Exception as exc:
                logger.error("Skill discovery scan failed for '%s': %s", effective_root, exc)
                return SkillRefreshResult(
                    success=False,
                    added=(),
                    updated=(),
                    removed=(),
                    unmodified=tuple(sorted(prev_external.keys())),
                    rejected=(),
                    generation=start_gen,
                    error=f"Discovery scan exception: {exc}",
                )

            # Check for root-level failure (reparse point, inaccessible root, scan error)
            if discovery_result.is_root_error:
                root_err = discovery_result.root_error
                err_msg = root_err.reason if root_err is not None else "Root directory verification failed."
                logger.warning("External skills root verification failed for '%s': %s", effective_root, err_msg)
                return SkillRefreshResult(
                    success=False,
                    added=(),
                    updated=(),
                    removed=(),
                    unmodified=tuple(sorted(prev_external.keys())),
                    rejected=discovery_result.rejected,
                    generation=start_gen,
                    error=err_msg,
                )

            # Map discovered valid skills and rejected package directory names
            discovered_map = {s.metadata.id: s for s in discovery_result.skills}
            rejected_pkg_names = {r.package_name for r in discovery_result.rejected}

            added: list[str] = []
            updated: list[str] = []
            removed: list[str] = []
            unmodified: list[str] = []
            retained_lkg: list[str] = []
            new_snapshot: list[Skill] = []

            # 1. Process previously registered external skills
            for prev_id, prev_skill in prev_external.items():
                prev_pkg_name = prev_id.removeprefix("user.")
                if prev_pkg_name in rejected_pkg_names:
                    # Malformed update: retain last-known-good proxy
                    new_snapshot.append(prev_skill)
                    retained_lkg.append(prev_id)
                    unmodified.append(prev_id)
                elif prev_id in discovered_map:
                    new_skill = discovered_map[prev_id]
                    new_snapshot.append(new_skill)
                    prev_manifest = getattr(prev_skill, "manifest", None)
                    new_manifest = getattr(new_skill, "manifest", None)
                    if prev_skill.metadata != new_skill.metadata or prev_manifest != new_manifest:
                        updated.append(prev_id)
                    else:
                        unmodified.append(prev_id)
                else:
                    # Confirmed absent from disk
                    removed.append(prev_id)

            # 2. Process newly discovered skills
            for new_id, new_skill in discovered_map.items():
                if new_id not in prev_external:
                    added.append(new_id)
                    new_snapshot.append(new_skill)

            # 3. Publish new snapshot atomically
            try:
                new_gen = self._registry.update_external_skills(
                    new_snapshot,
                    expected_generation=start_gen,
                )
            except Exception as exc:
                logger.error("Failed to publish updated external skills snapshot: %s", exc)
                return SkillRefreshResult(
                    success=False,
                    added=(),
                    updated=(),
                    removed=(),
                    unmodified=tuple(sorted(prev_external.keys())),
                    rejected=discovery_result.rejected,
                    generation=start_gen,
                    error=f"Registry publication error: {exc}",
                )

            return SkillRefreshResult(
                success=True,
                added=tuple(sorted(added)),
                updated=tuple(sorted(updated)),
                removed=tuple(sorted(removed)),
                unmodified=tuple(sorted(unmodified)),
                rejected=discovery_result.rejected,
                retained_last_known_good=tuple(sorted(retained_lkg)),
                generation=new_gen,
                error=None,
            )


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
