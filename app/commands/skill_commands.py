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
