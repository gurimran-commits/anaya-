import json
from typing import Any

import pytest

from app.brain.text_assistant import TextAssistant
from app.commands.executor import CommandExecutor
from app.commands.registry import CommandRegistry
from app.commands.skill_commands import register_skill_commands
from app.security.approval_service import ApprovalService
from app.skills.builtin.example_skill import GreetingSkill, register_builtin_skills
from app.skills.models import (
    InputFieldSpec,
    Skill,
    SkillMetadata,
    SkillPermission,
    SkillResult,
    SkillStatus,
)
from app.skills.skill_executor import SkillExecutor
from app.skills.skill_manager import SkillManager
from app.skills.skill_registry import SkillRegistry


# -------------------------------------------------------------------------
# Test Helper Skills
# -------------------------------------------------------------------------

class CalculatorSkill(Skill):
    """Simple test skill requiring integer inputs."""

    metadata = SkillMetadata(
        id="test.calculator",
        name="Test Calculator",
        description="Adds two integers.",
        version="1.0.0",
        inputs=(
            InputFieldSpec(name="a", type=int, required=True, description="First number"),
            InputFieldSpec(name="b", type=int, required=True, description="Second number"),
        ),
        required_permissions=(SkillPermission.NONE,),
    )

    def execute(self, inputs: dict[str, Any]) -> SkillResult:
        a = inputs["a"]
        b = inputs["b"]
        total = a + b
        return SkillResult(success=True, data={"result": total}, message=f"Sum: {total}")


class FailingSkill(Skill):
    """Skill that raises an unexpected exception."""

    metadata = SkillMetadata(
        id="test.failing",
        name="Test Failing",
        description="Throws an unexpected error during execution.",
        version="0.1.0",
        inputs=(),
    )

    def execute(self, inputs: dict[str, Any]) -> SkillResult:
        raise RuntimeError("Unexpected internal crash inside skill implementation!")


class SensitiveSkill(Skill):
    """Skill that requires explicit out-of-band approval."""

    def __init__(self) -> None:
        self.invoked = False

    metadata = SkillMetadata(
        id="test.sensitive",
        name="Test Sensitive Operation",
        description="Simulates a sensitive administrative action.",
        version="1.0.0",
        inputs=(
            InputFieldSpec(name="target", type=str, required=True, description="Target entity"),
        ),
        required_permissions=(SkillPermission.APPROVAL_REQUIRED,),
        requires_approval=True,
    )

    def execute(self, inputs: dict[str, Any]) -> SkillResult:
        self.invoked = True
        return SkillResult(
            success=True,
            data={"status": "completed", "target": inputs["target"]},
            message=f"Sensitive action executed on {inputs['target']}.",
        )


class MutatingSkill(Skill):
    """Skill that attempts to mutate its inputs."""

    metadata = SkillMetadata(
        id="test.mutating",
        name="Test Mutating",
        description="Attempts to mutate its received input dictionary.",
        version="1.0.0",
        inputs=(
            InputFieldSpec(
                name="config",
                type=dict,
                required=False,
                default={"env": "prod"},
                description="Configuration dictionary",
            ),
        ),
    )

    def execute(self, inputs: dict[str, Any]) -> SkillResult:
        inputs["injected_field"] = "corrupted"
        if isinstance(inputs.get("config"), dict):
            inputs["config"]["tampered"] = True
        return SkillResult(success=True, message="Execution finished.")


# -------------------------------------------------------------------------
# 1. Registry Tests
# -------------------------------------------------------------------------

def test_registry_register_and_get():
    registry = SkillRegistry()
    skill = GreetingSkill()

    assert not registry.exists("example.greeting")
    registry.register(skill)

    assert registry.exists("example.greeting")
    retrieved = registry.get("example.greeting")
    assert retrieved is skill
    assert retrieved.metadata.name == "Example Greeting"


def test_registry_rejects_duplicate_registration():
    registry = SkillRegistry()
    skill = GreetingSkill()
    registry.register(skill)

    with pytest.raises(ValueError, match="Skill already registered"):
        registry.register(skill)


def test_registry_unregister():
    registry = SkillRegistry()
    skill = GreetingSkill()
    registry.register(skill)
    assert registry.exists("example.greeting")

    assert registry.unregister("example.greeting")
    assert not registry.exists("example.greeting")
    assert registry.get("example.greeting") is None
    assert not registry.unregister("example.greeting")


def test_registry_enable_and_disable():
    registry = SkillRegistry()
    skill = GreetingSkill()
    registry.register(skill)

    assert registry.is_enabled("example.greeting")
    assert len(registry.list_skills(enabled_only=True)) == 1

    assert registry.disable("example.greeting")
    assert not registry.is_enabled("example.greeting")
    assert len(registry.list_skills(enabled_only=True)) == 0
    assert len(registry.list_skills(enabled_only=False)) == 1

    assert registry.enable("example.greeting")
    assert registry.is_enabled("example.greeting")
    assert len(registry.list_skills(enabled_only=True)) == 1


def test_registry_invalid_skill_object():
    registry = SkillRegistry()
    with pytest.raises(TypeError, match="Skill must provide a valid 'metadata' attribute"):
        registry.register(object())  # type: ignore


# -------------------------------------------------------------------------
# 2. Metadata & Validation Tests
# -------------------------------------------------------------------------

def test_metadata_validation_rules():
    with pytest.raises(ValueError, match="Skill ID must be a non-empty string"):
        SkillMetadata(id="", name="Valid", description="Valid")

    with pytest.raises(ValueError, match="surrounding whitespace"):
        SkillMetadata(id=" skill.id ", name="Valid", description="Valid")

    with pytest.raises(ValueError, match="Skill display name must be a non-empty string"):
        SkillMetadata(id="skill.id", name="  ", description="Valid")

    with pytest.raises(ValueError, match="Skill description must be a string"):
        SkillMetadata(id="skill.id", name="Valid", description=None)  # type: ignore

    with pytest.raises(ValueError, match="Skill version must be a non-empty string"):
        SkillMetadata(id="skill.id", name="Valid", description="Valid", version="")


def test_input_field_spec_validation():
    spec = InputFieldSpec(name="count", type=int, required=True)

    # Valid
    ok, err = spec.validate(42)
    assert ok and err is None

    # Missing required
    ok, err = spec.validate(None)
    assert not ok
    assert "Missing required input field" in err

    # Type mismatch: string given for int
    ok, err = spec.validate("not_an_int")
    assert not ok
    assert "expected type 'int', got 'str'" in err

    # Bool given for int (in Python bool inherits from int)
    ok, err = spec.validate(True)
    assert not ok
    assert "expected int, got bool" in err


def test_executor_rejects_unexpected_input_fields():
    registry = SkillRegistry()
    skill = GreetingSkill()
    registry.register(skill)
    executor = SkillExecutor(registry)

    # GreetingSkill only defines 'name'
    result = executor.execute("example.greeting", {"name": "Alice", "extra_bad_field": 123})
    assert not result.success
    assert result.error == "validation_error"
    assert "Unexpected input field(s)" in result.message


def test_executor_applies_defaults_for_optional_fields():
    registry = SkillRegistry()
    skill = GreetingSkill()
    registry.register(skill)
    executor = SkillExecutor(registry)

    # No input provided: default 'friend' should be applied
    result = executor.execute("example.greeting", {})
    assert result.success
    assert result.message == "Hello, friend!"
    assert result.data["target"] == "friend"


# -------------------------------------------------------------------------
# 3. Execution & Isolation Tests
# -------------------------------------------------------------------------

def test_execute_greeting_skill_success():
    registry = SkillRegistry()
    skill = GreetingSkill()
    registry.register(skill)
    executor = SkillExecutor(registry)

    result = executor.execute("example.greeting", {"name": "Alice"})
    assert result.success
    assert result.message == "Hello, Alice!"
    assert result.data["greeting"] == "Hello, Alice!"


def test_execute_rejects_unknown_skill():
    registry = SkillRegistry()
    executor = SkillExecutor(registry)

    result = executor.execute("nonexistent.skill")
    assert not result.success
    assert result.error == "skill_not_found"
    assert "not registered" in result.message


def test_execute_rejects_disabled_skill():
    registry = SkillRegistry()
    skill = GreetingSkill()
    registry.register(skill)
    registry.disable("example.greeting")
    executor = SkillExecutor(registry)

    result = executor.execute("example.greeting")
    assert not result.success
    assert result.error == "skill_disabled"
    assert "disabled" in result.message


def test_execute_exception_isolation_does_not_crash():
    registry = SkillRegistry()
    skill = FailingSkill()
    registry.register(skill)
    executor = SkillExecutor(registry)

    # Should safely return SkillResult with error, not raise RuntimeError
    result = executor.execute("test.failing")
    assert not result.success
    assert result.error == "RuntimeError"
    assert "Unexpected internal crash" in result.message


def test_execute_calculator_skill_type_checking():
    registry = SkillRegistry()
    registry.register(CalculatorSkill())
    executor = SkillExecutor(registry)

    # Valid
    res = executor.execute("test.calculator", {"a": 10, "b": 20})
    assert res.success
    assert res.data["result"] == 30

    # Missing 'b'
    res_missing = executor.execute("test.calculator", {"a": 10})
    assert not res_missing.success
    assert "Missing required input field: 'b'" in res_missing.message

    # Invalid type for 'a'
    res_type = executor.execute("test.calculator", {"a": "ten", "b": 20})
    assert not res_type.success
    assert "expected type 'int', got 'str'" in res_type.message


# -------------------------------------------------------------------------
# 4. Security & Permissions Tests
# -------------------------------------------------------------------------

def test_sensitive_skill_requires_approval():
    registry = SkillRegistry()
    skill = SensitiveSkill()
    registry.register(skill)

    approvals = ApprovalService()
    executor = SkillExecutor(registry, approval_service=approvals)

    # 1. Execution without confirmation_id returns confirmation_required
    res1 = executor.execute("test.sensitive", {"target": "production_database"})
    assert not res1.success
    assert res1.confirmation_required
    assert res1.confirmation_id is not None
    assert "Confirmation required. Confirmation ID: " in res1.message

    conf_id = res1.confirmation_id
    req = approvals.get_request(conf_id)
    assert req is not None
    assert req.action == "skill.test.sensitive"

    # 2. Execution with invalid confirmation_id is rejected
    res_invalid = executor.execute(
        "test.sensitive",
        {"target": "production_database"},
        confirmation_id="fake_token_123",
    )
    assert not res_invalid.success
    assert res_invalid.error == "unauthorized"

    # 3. Out-of-band approval creates valid authorization
    assert approvals.approve(req)

    # 4. Execution with authorized confirmation_id succeeds
    res_approved = executor.execute(
        "test.sensitive",
        {"target": "production_database"},
        confirmation_id=conf_id,
    )
    assert res_approved.success
    assert "Sensitive action executed on production_database" in res_approved.message

    # 5. Replay attempt fails: confirmation is single-use
    res_replay = executor.execute(
        "test.sensitive",
        {"target": "production_database"},
        confirmation_id=conf_id,
    )
    assert not res_replay.success
    assert res_replay.error == "unauthorized"


def test_sensitive_skill_fails_if_no_approval_service():
    registry = SkillRegistry()
    registry.register(SensitiveSkill())
    executor = SkillExecutor(registry, approval_service=None)

    res = executor.execute("test.sensitive", {"target": "server"})
    assert not res.success
    assert res.error == "missing_approval_service"


# -------------------------------------------------------------------------
# 5. SkillManager Facade & Discovery Tests
# -------------------------------------------------------------------------

def test_skill_manager_discovery():
    manager = SkillManager()
    manager.register_skill(GreetingSkill())
    manager.register_skill(CalculatorSkill())

    discovered = manager.discover_skills()
    assert len(discovered) == 2
    ids = {d["id"] for d in discovered}
    assert ids == {"example.greeting", "test.calculator"}

    # Disabling a skill excludes it from discovery
    manager.disable_skill("test.calculator")
    discovered_after = manager.discover_skills()
    assert len(discovered_after) == 1
    assert discovered_after[0]["id"] == "example.greeting"


# -------------------------------------------------------------------------
# 6. Command Integration Tests (skills.list & skills.run)
# -------------------------------------------------------------------------

def test_skill_commands_registration_and_execution():
    registry = CommandRegistry()
    approvals = ApprovalService()
    cmd_executor = CommandExecutor(registry, approvals)

    skill_manager = SkillManager(approval_service=approvals)
    register_builtin_skills(skill_manager.registry)
    register_skill_commands(registry, skill_manager, cmd_executor)

    # 1. skills.list
    list_res = cmd_executor.execute("skills.list")
    assert list_res.success
    assert "example.greeting" in list_res.message
    assert "Example Greeting" in list_res.message

    # 2. skills.run with no inputs (uses default)
    run_default = cmd_executor.execute("skills.run", ("example.greeting",))
    assert run_default.success
    assert run_default.message == "Hello, friend!"

    # 3. skills.run with JSON input
    run_json = cmd_executor.execute("skills.run", ("example.greeting", '{"name": "Alice"}'))
    assert run_json.success
    assert run_json.message == "Hello, Alice!"

    # 4. skills.run with direct string mapped to single input field
    run_direct = cmd_executor.execute("skills.run", ("example.greeting", "Bob"))
    assert run_direct.success
    assert run_direct.message == "Hello, Bob!"

    # 5. skills.run with unknown skill
    run_unknown = cmd_executor.execute("skills.run", ("nonexistent.skill",))
    assert not run_unknown.success
    assert "Command execution failed." in run_unknown.message

    # Direct skill_manager.execute_skill provides the exact error
    direct_res = skill_manager.execute_skill("nonexistent.skill")
    assert not direct_res.success
    assert direct_res.error == "skill_not_found"
    assert "is not registered" in direct_res.message


# -------------------------------------------------------------------------
# 7. Brain & Natural Language Integration Tests
# -------------------------------------------------------------------------

def test_brain_skill_discovery():
    registry = CommandRegistry()
    approvals = ApprovalService()
    executor = CommandExecutor(registry, approvals)

    skill_manager = SkillManager(approval_service=approvals)
    register_builtin_skills(skill_manager.registry)

    assistant = TextAssistant(
        registry,
        executor,
        skill_manager=skill_manager,
    )

    for phrase in [
        "What skills are available?",
        "What skills do you have?",
        "List available skills.",
        "Show available skills",
        "list skills",
    ]:
        res = assistant.process(phrase)
        assert res.success, f"Failed for phrase: '{phrase}'"
        assert "example.greeting" in res.message
        assert "Example Greeting" in res.message


def test_brain_skill_execution_phrasings():
    registry = CommandRegistry()
    approvals = ApprovalService()
    executor = CommandExecutor(registry, approvals)

    skill_manager = SkillManager(approval_service=approvals)
    register_builtin_skills(skill_manager.registry)

    assistant = TextAssistant(
        registry,
        executor,
        skill_manager=skill_manager,
    )

    # 1. "Run the greeting skill." -> default
    res1 = assistant.process("Run the greeting skill.")
    assert res1.success
    assert res1.message == "Hello, friend!"

    # 2. "Run the greeting skill with name Alice"
    res2 = assistant.process("Run the greeting skill with name Alice")
    assert res2.success
    assert res2.message == "Hello, Alice!"

    # 3. "Greet Alice using the greeting skill"
    res3 = assistant.process("Greet Alice using the greeting skill")
    assert res3.success
    assert res3.message == "Hello, Alice!"

    # 4. "Greet 'Bob' with greeting skill"
    res4 = assistant.process("Greet 'Bob' with greeting skill")
    assert res4.success
    assert res4.message == "Hello, Bob!"

    # 5. Generic skill invocation: "Run skill example.greeting with {"name": "Charlie"}"
    res5 = assistant.process('Run skill example.greeting with {"name": "Charlie"}')
    assert res5.success
    assert res5.message == "Hello, Charlie!"


def test_brain_skills_precedence_over_pending_clarification():
    registry = CommandRegistry()
    approvals = ApprovalService()
    executor = CommandExecutor(registry, approvals)

    skill_manager = SkillManager(approval_service=approvals)
    register_builtin_skills(skill_manager.registry)

    assistant = TextAssistant(
        registry,
        executor,
        skill_manager=skill_manager,
    )

    # Start a file read clarification
    # Dummy file command registered for clarification test
    from app.commands.filesystem import FilesystemService
    from app.commands.filesystem_commands import register_filesystem_commands
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        fs = FilesystemService((Path(td),))
        register_filesystem_commands(registry, fs)

        assistant.process("read file")
        assert assistant.context.has_pending

        # Issuing "list available skills" should supersede the pending read clarification
        skills_res = assistant.process("list available skills")
        assert skills_res.success
        assert "example.greeting" in skills_res.message
        assert not assistant.context.has_pending

        fs.close()


# -------------------------------------------------------------------------
# 8. Hardening & Edge-Case Security Tests
# -------------------------------------------------------------------------

def test_direct_api_approval_enforcement():
    registry = SkillRegistry()
    skill = SensitiveSkill()
    registry.register(skill)

    approvals = ApprovalService()
    manager = SkillManager(registry=registry, approval_service=approvals)

    # 1. Direct call to SkillManager.execute_skill without confirmation_id
    res_unauth = manager.execute_skill("test.sensitive", {"target": "user_records"})
    assert not res_unauth.success
    assert res_unauth.confirmation_required
    assert res_unauth.confirmation_id is not None
    # Crucial: verify skill implementation was NEVER invoked
    assert not skill.invoked

    conf_id = res_unauth.confirmation_id
    req = approvals.get_request(conf_id)
    assert req is not None
    assert req.action == "skill.test.sensitive"

    # 2. Out-of-band approval
    assert approvals.approve(req)

    # 3. Execution with valid authorization succeeds and invokes implementation
    res_auth = manager.execute_skill(
        "test.sensitive",
        {"target": "user_records"},
        confirmation_id=conf_id,
    )
    assert res_auth.success
    assert skill.invoked
    assert "Sensitive action executed on user_records" in res_auth.message

    # 4. Replaying consumed authorization fails
    res_replay = manager.execute_skill(
        "test.sensitive",
        {"target": "user_records"},
        confirmation_id=conf_id,
    )
    assert not res_replay.success
    assert res_replay.error == "unauthorized"


def test_multiple_managers_sharing_registry():
    shared_registry = SkillRegistry()
    mgr1 = SkillManager(registry=shared_registry)
    mgr2 = SkillManager(registry=shared_registry)

    # Register skill through manager 1
    mgr1.register_skill(GreetingSkill())

    # Manager 2 can discover and query it
    assert mgr2.get_skill("example.greeting") is not None
    discovered = mgr2.discover_skills()
    assert len(discovered) == 1
    assert discovered[0]["id"] == "example.greeting"

    # Manager 2 can execute it
    res = mgr2.execute_skill("example.greeting", {"name": "Charlie"})
    assert res.success
    assert res.message == "Hello, Charlie!"


def test_simultaneous_manager_integration():
    import tempfile
    from pathlib import Path
    import gc

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
        temp_path = Path(temp_dir)
        db_path = temp_path / "simul_test.db"
        fs_path = temp_path / "fs"
        fs_path.mkdir()

        registry = CommandRegistry()
        approvals = ApprovalService()
        executor = CommandExecutor(registry, approvals)

        from app.memory.memory_manager import MemoryManager
        from app.commands.filesystem import FilesystemService
        from app.commands.filesystem_commands import register_filesystem_commands

        mem_mgr = MemoryManager(db_path=db_path)
        skill_mgr = SkillManager(approval_service=approvals)
        register_builtin_skills(skill_mgr.registry)

        fs_service = FilesystemService((fs_path,))
        register_filesystem_commands(registry, fs_service)

        # Initialize TextAssistant with BOTH MemoryManager and SkillManager
        assistant = TextAssistant(
            registry,
            executor,
            memory_manager=mem_mgr,
            skill_manager=skill_mgr,
        )

        # 1. Execute memory command
        mem_res = assistant.process("Remember that I prefer dark mode.")
        assert mem_res.success
        assert "Saved memory:" in mem_res.message

        # 2. Execute skill command
        skill_res = assistant.process("Run the greeting skill with name Dave")
        assert skill_res.success
        assert skill_res.message == "Hello, Dave!"

        # 3. Execute filesystem command
        fs_res = assistant.process("list files")
        assert fs_res.success

        # 4. Confirm idempotency: calling register again does not raise Command already registered
        from app.commands.skill_commands import register_skill_commands
        register_skill_commands(registry, skill_mgr, executor)

        fs_service.close()
        mem_mgr.close()
        gc.collect()


def test_malformed_natural_language_skill_input():
    registry = CommandRegistry()
    approvals = ApprovalService()
    executor = CommandExecutor(registry, approvals)

    skill_manager = SkillManager(approval_service=approvals)
    register_builtin_skills(skill_manager.registry)

    assistant = TextAssistant(
        registry,
        executor,
        skill_manager=skill_manager,
    )

    # 1. Unclosed JSON
    res_unclosed = assistant.process('run skill example.greeting with {"name": "Alice"')
    assert not res_unclosed.success
    assert res_unclosed.message == "Command execution failed."

    # 2. Invalid JSON syntax
    res_invalid = assistant.process("run skill example.greeting with {bad_json: 123}")
    assert not res_invalid.success
    assert res_invalid.message == "Command execution failed."

    # 3. Brain remains fully functional for subsequent commands
    res_healthy = assistant.process("Run the greeting skill.")
    assert res_healthy.success
    assert res_healthy.message == "Hello, friend!"


def test_input_mutation_isolation():
    registry = SkillRegistry()
    skill = MutatingSkill()
    registry.register(skill)
    executor = SkillExecutor(registry)

    # 1. Caller provides mutable dictionary
    caller_data = {"config": {"env": "staging", "workers": 4}}
    res = executor.execute("test.mutating", caller_data)
    assert res.success

    # Verify caller's dictionary was not mutated
    assert "injected_field" not in caller_data
    assert "tampered" not in caller_data["config"]
    assert caller_data["config"] == {"env": "staging", "workers": 4}

    # 2. Execute using defaults
    res_default1 = executor.execute("test.mutating", {})
    assert res_default1.success

    # 3. Execute again: verify metadata default itself was not corrupted
    res_default2 = executor.execute("test.mutating", {})
    assert res_default2.success

    meta_default = MutatingSkill.metadata.inputs[0].default
    assert "tampered" not in meta_default
    assert meta_default == {"env": "prod"}
