from typing import Any

from app.skills.models import (
    InputFieldSpec,
    Skill,
    SkillMetadata,
    SkillPermission,
    SkillResult,
)


class GreetingSkill(Skill):
    """A harmless example skill that generates a greeting message."""

    metadata = SkillMetadata(
        id="example.greeting",
        name="Example Greeting",
        description="Generates a friendly personalized greeting message.",
        version="1.0.0",
        inputs=(
            InputFieldSpec(
                name="name",
                type=str,
                required=False,
                default="friend",
                description="The name of the person to greet.",
            ),
        ),
        required_permissions=(SkillPermission.NONE,),
        enabled=True,
        requires_approval=False,
    )

    def execute(self, inputs: dict[str, Any]) -> SkillResult:
        name = inputs.get("name", "friend")
        if not isinstance(name, str) or not name.strip():
            name = "friend"
        else:
            name = name.strip()

        message = f"Hello, {name}!"
        return SkillResult(
            success=True,
            data={"greeting": message, "target": name},
            message=message,
        )


def register_builtin_skills(registry: Any) -> None:
    """Register all trusted built-in skills with the provided registry."""
    if not registry.exists("example.greeting"):
        registry.register(GreetingSkill())
