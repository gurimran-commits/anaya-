from collections.abc import Mapping
import copy
import json
import logging
from typing import TYPE_CHECKING, Any

from app.skills.models import SkillPermission, SkillResult

if TYPE_CHECKING:
    from app.security.approval_service import ApprovalService
    from app.skills.models import Skill
    from app.skills.skill_registry import SkillRegistry

logger = logging.getLogger("anaya.skills")


class SkillExecutor:
    """Execute registered skills under strict validation, permissions, and isolation.

    Security Note:
        Permissions act as application-level execution gates and authorization checks.
        They do NOT provide operating system, process, or Python runtime sandboxing;
        all registered skills run in-process as trusted Python code.
    """

    def __init__(
        self,
        registry: "SkillRegistry",
        approval_service: "ApprovalService | None" = None,
        skill_permission_grants: Mapping[str, tuple[str, ...]] | None = None,
    ) -> None:
        self._registry = registry
        self._approval_service = approval_service
        self._skill_permission_grants: dict[str, frozenset[SkillPermission]] = {}
        if skill_permission_grants:
            for s_id, perms in skill_permission_grants.items():
                parsed_perms: set[SkillPermission] = set()
                for p in perms:
                    if isinstance(p, SkillPermission):
                        parsed_perms.add(p)
                    elif isinstance(p, str):
                        try:
                            parsed_perms.add(SkillPermission(p.strip().lower()))
                        except ValueError:
                            pass
                self._skill_permission_grants[s_id.strip()] = frozenset(parsed_perms)

    @property
    def registry(self) -> "SkillRegistry":
        """Access the underlying SkillRegistry."""
        return self._registry

    @property
    def approval_service(self) -> "ApprovalService | None":
        """Access the configured ApprovalService."""
        return self._approval_service

    @property
    def skill_permission_grants(self) -> dict[str, frozenset[SkillPermission]]:
        """Return a copy of the configured skill permission grants."""
        return dict(self._skill_permission_grants)

    def get_granted_permissions(self, skill_id: str) -> frozenset[SkillPermission]:
        """Return the runtime capability permissions granted to a specific skill ID."""
        if not isinstance(skill_id, str):
            return frozenset()
        return self._skill_permission_grants.get(skill_id.strip(), frozenset())

    def validate_inputs(
        self,
        skill: "Skill",
        inputs: dict[str, Any] | None,
    ) -> tuple[bool, dict[str, Any], str | None]:
        """Validate requested inputs against a skill's declared input specifications.

        Returns:
            (is_valid, processed_inputs, error_message)
        """
        raw_inputs = inputs or {}
        if not isinstance(raw_inputs, dict):
            return False, {}, "Skill inputs must be provided as a dictionary."

        specs_by_name = {spec.name: spec for spec in skill.metadata.inputs}

        # 1. Reject unexpected / unknown input fields to prevent input pollution
        unexpected_keys = set(raw_inputs.keys()) - set(specs_by_name.keys())
        if unexpected_keys:
            sorted_keys = ", ".join(sorted(unexpected_keys))
            return False, {}, f"Unexpected input field(s) for skill '{skill.metadata.id}': {sorted_keys}."

        processed_inputs: dict[str, Any] = {}

        # 2. Check each declared field spec
        for name, spec in specs_by_name.items():
            if name in raw_inputs:
                value = raw_inputs[name]
                is_valid, err = spec.validate(value)
                if not is_valid:
                    return False, {}, err
                processed_inputs[name] = copy.deepcopy(value)
            else:
                if spec.required:
                    return False, {}, f"Missing required input field: '{name}' for skill '{skill.metadata.id}'."
                processed_inputs[name] = copy.deepcopy(spec.default)

        return True, processed_inputs, None

    def execute(
        self,
        skill_id: str,
        inputs: dict[str, Any] | None = None,
        confirmation_id: str | None = None,
    ) -> SkillResult:
        """Execute a skill by ID with contract validation and authorization enforcement."""
        if not isinstance(skill_id, str) or not skill_id.strip():
            return SkillResult(
                success=False,
                error="invalid_skill_id",
                message="Skill ID must be a non-empty string.",
            )

        clean_id = skill_id.strip()

        # 1. Verify existence
        skill = self._registry.get(clean_id)
        if skill is None:
            return SkillResult(
                success=False,
                error="skill_not_found",
                message=f"Skill '{clean_id}' is not registered.",
            )

        # 2. Verify enabled status
        if not self._registry.is_enabled(clean_id):
            return SkillResult(
                success=False,
                error="skill_disabled",
                message=f"Skill '{clean_id}' is currently disabled.",
            )

        # 3. Validate inputs against contract
        is_valid, validated_inputs, val_error = self.validate_inputs(skill, inputs)
        if not is_valid:
            return SkillResult(
                success=False,
                error="validation_error",
                message=val_error or "Input validation failed.",
            )

        # 4. Check runtime capability permission grants (fail-closed execution gate)
        # Capabilities requiring explicit runtime grants are non-NONE, non-APPROVAL permissions.
        required_capabilities = {
            perm
            for perm in skill.metadata.required_permissions
            if perm not in (SkillPermission.NONE, SkillPermission.APPROVAL_REQUIRED)
        }
        if required_capabilities:
            granted_capabilities = self.get_granted_permissions(clean_id)
            missing = required_capabilities - granted_capabilities
            if missing:
                missing_str = ", ".join(sorted(p.value for p in missing))
                return SkillResult(
                    success=False,
                    error="permission_denied",
                    message=(
                        f"Skill '{clean_id}' requires permission(s) not granted by "
                        f"runtime configuration: {missing_str}."
                    ),
                )

        # 5. Check permissions and out-of-band approval
        requires_approval = (
            skill.metadata.requires_approval
            or SkillPermission.APPROVAL_REQUIRED in skill.metadata.required_permissions
        )

        if requires_approval:
            if self._approval_service is None:
                return SkillResult(
                    success=False,
                    error="missing_approval_service",
                    message=(
                        f"Skill '{clean_id}' requires explicit approval, but "
                        "no ApprovalService is configured."
                    ),
                )

            # Deterministic argument representation for authorization binding
            serialized_inputs = json.dumps(validated_inputs, sort_keys=True)
            approval_action = f"skill.{clean_id}"
            approval_args = (clean_id, serialized_inputs)

            if confirmation_id is None:
                try:
                    request = self._approval_service.request_approval(
                        approval_action,
                        approval_args,
                    )
                except Exception as exc:
                    return SkillResult(
                        success=False,
                        error="approval_request_failed",
                        message=f"Could not create approval request for skill '{clean_id}': {exc}",
                    )

                return SkillResult(
                    success=False,
                    confirmation_required=True,
                    confirmation_id=request.confirmation_id,
                    message=(
                        f"Confirmation required. Confirmation ID: {request.confirmation_id}"
                    ),
                )

            # Validate and consume one-time authorization token
            authorized = self._approval_service.consume_authorization(
                confirmation_id,
                approval_action,
                approval_args,
            )
            if not authorized:
                return SkillResult(
                    success=False,
                    error="unauthorized",
                    message="Invalid, expired, or unauthorized confirmation ID.",
                )

        # 6. Invoke skill implementation with complete exception isolation
        try:
            result = skill.execute(validated_inputs)
            if isinstance(result, SkillResult):
                return result
            # Normalize arbitrary return values to SkillResult
            return SkillResult(
                success=True,
                data=result,
                message=str(result) if result is not None else "",
            )
        except Exception as exc:
            logger.exception("Uncaught exception while executing skill '%s'", clean_id)
            return SkillResult(
                success=False,
                error=type(exc).__name__,
                message=f"Skill '{clean_id}' execution failed: {exc}",
            )
