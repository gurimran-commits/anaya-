import json
from typing import TYPE_CHECKING, Any

from app.commands.registry import CommandRegistry, CommandSpec

if TYPE_CHECKING:
    from app.commands.executor import CommandExecutor
    from app.skills.skill_manager import SkillManager


def register_skill_commands(
    registry: CommandRegistry,
    skill_manager: "SkillManager",
    executor: "CommandExecutor | None" = None,
) -> None:
    """Register skill operations with the CommandRegistry."""

    def list_skills() -> object:
        skills = skill_manager.list_skills(enabled_only=True)
        if not skills:
            return "No skills available."
        lines = [
            f"- {s.metadata.id} (v{s.metadata.version}): {s.metadata.name} - {s.metadata.description}"
            for s in skills
        ]
        return "\n".join(lines)

    def run_skill(*args: str) -> object:
        if not args:
            raise ValueError("Skill ID is required.")

        skill_id = args[0].strip()
        parsed_inputs: dict[str, Any] = {}

        if len(args) > 1:
            raw_input = args[1].strip()
            if raw_input:
                if raw_input.startswith("{") or raw_input.endswith("}"):
                    try:
                        parsed = json.loads(raw_input)
                        if not isinstance(parsed, dict):
                            raise ValueError("JSON inputs must be an object/dictionary.")
                        parsed_inputs = parsed
                    except json.JSONDecodeError as exc:
                        raise ValueError(f"Malformed JSON input: {exc.msg}") from exc
                else:
                    # If single field expected by skill, map raw string to that field
                    skill = skill_manager.get_skill(skill_id)
                    if skill and len(skill.metadata.inputs) == 1:
                        field_name = skill.metadata.inputs[0].name
                        parsed_inputs = {field_name: raw_input}
                    else:
                        raise ValueError(
                            "Inputs must be provided as a JSON object string, e.g. '{\"name\": \"Alice\"}'."
                        )

        result = skill_manager.execute_skill(skill_id, parsed_inputs)

        if result.confirmation_required and result.confirmation_id:
            return f"Confirmation required. Confirmation ID: {result.confirmation_id}"

        if not result.success:
            raise ValueError(result.message or result.error or f"Skill '{skill_id}' failed.")

        return result.message or "Skill executed successfully."

    def refresh_skills() -> object:
        result = skill_manager.refresh_skills()
        if not result.success:
            err = result.error or "Refresh failed."
            if result.rejected:
                reasons = "; ".join(f"{r.package_name}: {r.reason}" for r in result.rejected)
                raise ValueError(f"Skill refresh failed: {err} (Rejections: {reasons})")
            raise ValueError(f"Skill refresh failed: {err}")

        parts = [
            f"Skills refreshed (Generation {result.generation}): "
            f"{len(result.added)} added, {len(result.updated)} updated, "
            f"{len(result.removed)} removed, {len(result.unmodified)} unchanged."
        ]
        if result.retained_last_known_good:
            parts.append(
                f"Retained {len(result.retained_last_known_good)} last-known-good package(s) "
                f"after malformed update ({', '.join(result.retained_last_known_good)})."
            )
        if result.rejected:
            reasons = "; ".join(f"{r.package_name} ({r.error_code})" for r in result.rejected)
            parts.append(f"Warning: {len(result.rejected)} package(s) rejected: {reasons}.")

        return "\n".join(parts)

    if registry.get("skills.list") is None:
        registry.register(
            CommandSpec(
                name="skills.list",
                description="List all available and enabled modular skills",
                handler=list_skills,
                validate_arguments=lambda args: len(args) == 0,
            )
        )

    if registry.get("skills.run") is None:
        registry.register(
            CommandSpec(
                name="skills.run",
                description="Execute a registered modular skill with optional arguments",
                handler=run_skill,
                validate_arguments=lambda args: (
                    len(args) in (1, 2)
                    and all(isinstance(a, str) and bool(a.strip()) for a in args)
                ),
            )
        )

    if registry.get("skills.refresh") is None:
        registry.register(
            CommandSpec(
                name="skills.refresh",
                description="Rescan external skills directory and dynamically refresh registered skills",
                handler=refresh_skills,
                validate_arguments=lambda args: len(args) == 0,
            )
        )
