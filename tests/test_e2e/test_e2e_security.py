"""Phase 8B Step 2E: End-to-end security tests.

Validates security boundaries across command execution, approval lifecycle,
filesystem sandboxing, modular skills isolation, AI generation isolation,
and runtime data integrity using real AssistantController instances.
"""

from __future__ import annotations

import copy
from dataclasses import FrozenInstanceError, asdict
from datetime import timedelta
import json
from pathlib import Path
import pickle
from typing import Any

import pytest

from app.ai.models import (
    ChatMessage,
    GenerationOptions,
    GenerationResult,
    ModelDetails,
    ModelInfo,
)
from app.core.config import AppConfig
from app.skills.models import (
    InputFieldSpec,
    Skill,
    SkillMetadata,
    SkillPermission,
    SkillResult,
)
from app.skills.skill_executor import SkillExecutor
from app.skills.skill_registry import SkillRegistry
from app.tasks.models import TaskNotFoundError
from app.tasks.task_manager import TaskManager
from tests.test_e2e.conftest import (
    ControllerFactory,
    FakeClock,
    TempAnayaPaths,
)


class FakeAdversarialAIProvider:
    """Deterministic fake ModelProvider returning adversarial command payloads."""

    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.chat_called = False

    def ping(self) -> bool:
        return True

    def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(name="qwen2.5:1.5b", size_bytes=1000)]

    def get_model_details(self, model_name: str) -> ModelDetails | None:
        return ModelDetails(family="qwen2.5", parameter_size="1.5B", quantization_level="Q4")

    def chat(
        self,
        messages: list[ChatMessage],
        model: str,
        options: GenerationOptions | None = None,
    ) -> GenerationResult:
        self.chat_called = True
        return GenerationResult(
            success=True,
            content=self.payload,
            model=model,
            total_duration_ns=100,
            eval_count=10,
        )

    def unload_model(self, model_name: str) -> bool:
        return True


class SensitiveWipeSkill(Skill):
    """Test skill requiring explicit out-of-band approval for destructive execution."""

    def __init__(self) -> None:
        self.invoked = False
        self.invoked_with: dict[str, Any] | None = None

    @property
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            id="test.security_wipe",
            name="Security Wipe Skill",
            description="Destructive wipe operation requiring explicit approval.",
            requires_approval=True,
            required_permissions=(SkillPermission.APPROVAL_REQUIRED,),
            inputs=[
                InputFieldSpec(
                    name="target",
                    type=str,
                    required=True,
                    description="Target resource to wipe",
                )
            ],
        )

    def execute(self, inputs: dict[str, Any]) -> SkillResult:
        self.invoked = True
        self.invoked_with = inputs
        return SkillResult(
            success=True,
            message=f"Successfully wiped target: {inputs.get('target')}",
        )


class CrashingFaultySkill(Skill):
    """Test skill that raises an unhandled exception during execution."""

    @property
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            id="test.crashing_faulty",
            name="Crashing Skill",
            description="Skill that crashes intentionally to test fault isolation.",
            requires_approval=False,
            inputs=[],
        )

    def execute(self, inputs: dict[str, Any]) -> SkillResult:
        raise RuntimeError("Simulated unexpected fault in third-party skill.")


class UnprivilegedSkill(Skill):
    """Test skill declaring only NONE permission and requiring no grants."""

    def __init__(self) -> None:
        self.invoked = False

    @property
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            id="test.unprivileged",
            name="Unprivileged Skill",
            description="Requires no special capability permissions.",
            required_permissions=(SkillPermission.NONE,),
            requires_approval=False,
            inputs=[],
        )

    def execute(self, inputs: dict[str, Any]) -> SkillResult:
        self.invoked = True
        return SkillResult(success=True, message="Executed without elevated permissions")


class ReadOnlySkill(Skill):
    """Test skill declaring READ_ONLY capability permission."""

    def __init__(self) -> None:
        self.invoked = False

    @property
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            id="test.read_only",
            name="Read-Only Skill",
            description="Reads data requiring READ_ONLY capability.",
            required_permissions=(SkillPermission.READ_ONLY,),
            requires_approval=False,
            inputs=[],
        )

    def execute(self, inputs: dict[str, Any]) -> SkillResult:
        self.invoked = True
        return SkillResult(success=True, message="Read-only data accessed.")


class FilesystemSkill(Skill):
    """Test skill declaring FILESYSTEM capability permission."""

    def __init__(self) -> None:
        self.invoked = False

    @property
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            id="test.filesystem",
            name="Filesystem Skill",
            description="Performs operations requiring FILESYSTEM capability.",
            required_permissions=(SkillPermission.FILESYSTEM,),
            requires_approval=False,
            inputs=[],
        )

    def execute(self, inputs: dict[str, Any]) -> SkillResult:
        self.invoked = True
        return SkillResult(success=True, message="Filesystem accessed.")


class MultiCapabilitySkill(Skill):
    """Test skill declaring multiple required capabilities (READ_ONLY + FILESYSTEM)."""

    def __init__(self) -> None:
        self.invoked = False

    @property
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            id="test.multi_capability",
            name="Multi Capability Skill",
            description="Requires both READ_ONLY and FILESYSTEM permissions.",
            required_permissions=(SkillPermission.READ_ONLY, SkillPermission.FILESYSTEM),
            requires_approval=False,
            inputs=[],
        )

    def execute(self, inputs: dict[str, Any]) -> SkillResult:
        self.invoked = True
        return SkillResult(success=True, message="Both capabilities exercised.")


class ApprovalAndFilesystemSkill(Skill):
    """Test skill requiring both FILESYSTEM capability and explicit user approval."""

    def __init__(self) -> None:
        self.invoked = False
        self.invoked_with: dict[str, Any] | None = None

    @property
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            id="test.approval_and_fs",
            name="Approval and Filesystem Skill",
            description="Filesystem modification requiring user approval.",
            required_permissions=(SkillPermission.FILESYSTEM, SkillPermission.APPROVAL_REQUIRED),
            requires_approval=True,
            inputs=[
                InputFieldSpec(
                    name="filepath",
                    type=str,
                    required=True,
                    description="Target filepath to write",
                )
            ],
        )

    def execute(self, inputs: dict[str, Any]) -> SkillResult:
        self.invoked = True
        self.invoked_with = inputs
        return SkillResult(success=True, message=f"Wrote to {inputs.get('filepath')}")


# ---------------------------------------------------------------------------
# 1. Command Execution Security
# ---------------------------------------------------------------------------


def test_command_execution_security_boundaries(
    e2e_controller_factory: ControllerFactory,
    fake_clock: FakeClock,
) -> None:
    """Security Boundary 1: Invalid/malformed commands and confirmations cannot bypass security."""
    ctrl = e2e_controller_factory(clock=fake_clock)
    ctrl.start()

    try:
        # 1. Invalid and unsupported command attempts are rejected
        res_unknown = ctrl.handle_text("system.shutdown")
        assert res_unknown.success is False
        assert "not recognized" in res_unknown.message

        res_exec = ctrl.handle_text("exec rm -rf /")
        assert res_exec.success is False

        # Direct executor execution of an unknown command
        res_direct_unknown = ctrl.runtime.executor.execute("unregistered.dangerous.action", ())
        assert res_direct_unknown.success is False
        assert "Unknown command" in res_direct_unknown.message

        # 2. Malformed arguments are safely rejected without crashing
        res_malformed_task = ctrl.handle_text("tasks.delete not_a_valid_id")
        assert res_malformed_task.success is False

        res_missing_arg = ctrl.runtime.executor.execute("tasks.create", ())
        assert res_missing_arg.success is False
        assert "arguments are invalid" in res_missing_arg.message

        # 3. Destructive commands cannot bypass confirmation
        task = ctrl.task_manager.create_task(title="Protected Production Record")
        assert task.id is not None

        res_delete_req = ctrl.handle_text(f"tasks.delete {task.id}")
        assert res_delete_req.success is False
        assert "Confirmation required. Confirmation ID:" in res_delete_req.message

        # Verify task remains intact in SQLite
        persisted_task = ctrl.task_manager.get_task(task.id)
        assert persisted_task is not None
        assert persisted_task.status == "pending"

        # 4. Conversational confirmation text strictly rejected by security barrier
        for confirm_phrase in ["confirm", "approve", "yes", "confirm 1", f"confirm {task.id}"]:
            res_barrier = ctrl.handle_text(confirm_phrase)
            assert res_barrier.success is False
            assert "Text confirmation is disabled" in res_barrier.message

        # Verify task is STILL intact in SQLite
        persisted_still_pending = ctrl.task_manager.get_task(task.id)
        assert persisted_still_pending is not None
        assert persisted_still_pending.status == "pending"

    finally:
        ctrl.stop(timeout=2.0)
        assert ctrl.is_cleanly_stopped is True


# ---------------------------------------------------------------------------
# 2. Approval Lifecycle Security
# ---------------------------------------------------------------------------


def test_approval_lifecycle_and_single_use_authorization(
    e2e_controller_factory: ControllerFactory,
    fake_clock: FakeClock,
    tmp_path: Path,
) -> None:
    """Security Boundary 2: Approval authorizations are strictly bounded, one-time, and action-bound."""
    fs_sandbox = tmp_path / "app_fs_sandbox"
    fs_sandbox.mkdir()

    ctrl = e2e_controller_factory(
        clock=fake_clock,
        allowed_directories=(fs_sandbox,),
    )
    ctrl.start()

    try:
        task1 = ctrl.task_manager.create_task(title="Task Alpha")
        task2 = ctrl.task_manager.create_task(title="Task Beta")

        approval_service = ctrl.runtime.approval_service
        # Tie approval service expiration clock to fake clock
        approval_service._confirmations._clock = fake_clock.now

        # 1. Request approval for deleting Task Alpha
        req = approval_service.request_approval("tasks.delete", (str(task1.id),))
        assert req.confirmation_id is not None
        assert req.action == "tasks.delete"
        assert req.arguments == (str(task1.id),)

        # 2. Approval alone must NOT execute the action
        approved = approval_service.approve(req)
        assert approved is True

        # Task Alpha must still exist in SQLite (approval merely authorized, did not execute)
        assert ctrl.task_manager.get_task(task1.id) is not None

        # 3. Authorization cannot be consumed for a mismatched action (e.g. files.delete)
        # Note: consume_authorization immediately pops/invalidates the token for security.
        mismatched_action_res = ctrl.runtime.executor.execute(
            "files.delete",
            ("0", "some_file.txt"),
            confirmation_id=req.confirmation_id,
        )
        assert mismatched_action_res.success is False
        assert "invalid, expired, mismatched" in mismatched_action_res.message
        assert ctrl.task_manager.get_task(task1.id) is not None

        # 4. Authorization cannot be consumed for mismatched arguments
        req_args = approval_service.request_approval("tasks.delete", (str(task1.id),))
        assert approval_service.approve(req_args) is True
        mismatched_arg_res = ctrl.runtime.executor.execute(
            "tasks.delete",
            (str(task2.id),),
            confirmation_id=req_args.confirmation_id,
        )
        assert mismatched_arg_res.success is False
        assert "invalid, expired, mismatched" in mismatched_arg_res.message
        assert ctrl.task_manager.get_task(task1.id) is not None
        assert ctrl.task_manager.get_task(task2.id) is not None

        # 5. Authorization consumes successfully for exact matching action and arguments
        req_valid = approval_service.request_approval("tasks.delete", (str(task1.id),))
        assert approval_service.approve(req_valid) is True
        valid_res = ctrl.runtime.executor.execute(
            "tasks.delete",
            (str(task1.id),),
            confirmation_id=req_valid.confirmation_id,
        )
        assert valid_res.success is True
        with pytest.raises(TaskNotFoundError):
            ctrl.task_manager.get_task(task1.id)

        # 6. Replay attack: authorization cannot be reused
        replay_res = ctrl.runtime.executor.execute(
            "tasks.delete",
            (str(task1.id),),
            confirmation_id=req_valid.confirmation_id,
        )
        assert replay_res.success is False
        assert "invalid, expired, mismatched, or already used" in replay_res.message

        # 7. Unknown or rejected confirmation IDs fail
        unknown_res = ctrl.runtime.executor.execute(
            "tasks.delete",
            (str(task2.id),),
            confirmation_id="totally-unknown-id",
        )
        assert unknown_res.success is False

        req_rejected = approval_service.request_approval("tasks.delete", (str(task2.id),))
        rejected = approval_service.reject(req_rejected)
        assert rejected is True

        rejected_res = ctrl.runtime.executor.execute(
            "tasks.delete",
            (str(task2.id),),
            confirmation_id=req_rejected.confirmation_id,
        )
        assert rejected_res.success is False
        assert ctrl.task_manager.get_task(task2.id) is not None

        # 8. Expired confirmation IDs cannot authorize execution
        req_expired = approval_service.request_approval("tasks.delete", (str(task2.id),))
        # Advance time past 60s expiration limit
        fake_clock.advance(timedelta(seconds=120))

        # Expired approval request cannot be approved
        assert approval_service.approve(req_expired) is False
        expired_res = ctrl.runtime.executor.execute(
            "tasks.delete",
            (str(task2.id),),
            confirmation_id=req_expired.confirmation_id,
        )
        assert expired_res.success is False
        assert ctrl.task_manager.get_task(task2.id) is not None

    finally:
        ctrl.stop(timeout=2.0)
        assert ctrl.is_cleanly_stopped is True


# ---------------------------------------------------------------------------
# 3. Filesystem Security & Symlink Containment
# ---------------------------------------------------------------------------


def test_filesystem_security_path_traversal_and_directory_boundaries(
    e2e_controller_factory: ControllerFactory,
    fake_clock: FakeClock,
    tmp_path: Path,
) -> None:
    """Security Boundary 3: Filesystem operations strictly enforce path traversal and directory sandboxing."""
    authorized_dir = tmp_path / "sandbox_permitted"
    authorized_dir.mkdir()

    sensitive_dir = tmp_path / "forbidden_outside"
    sensitive_dir.mkdir()
    sensitive_file = sensitive_dir / "master_secret.key"
    sensitive_file.write_text("SUPER_SECRET_KEY=9999", encoding="utf-8")

    ctrl = e2e_controller_factory(
        clock=fake_clock,
        allowed_directories=(authorized_dir,),
    )
    ctrl.start()

    try:
        # Verify filesystem service registered
        assert ctrl.filesystem_service is not None
        assert ctrl.runtime.registry.get("files.read") is not None

        # 1. Path traversal reading attempts outside authorized root are rejected
        traversal_read_1 = ctrl.handle_text("files.read 0 ../forbidden_outside/master_secret.key")
        assert traversal_read_1.success is False

        traversal_read_2 = ctrl.handle_text("files.read 0 ..\\forbidden_outside\\master_secret.key")
        assert traversal_read_2.success is False

        traversal_read_3 = ctrl.handle_text("files.read 0 .")
        assert traversal_read_3.success is False

        traversal_read_4 = ctrl.handle_text("files.read 0 ..")
        assert traversal_read_4.success is False

        # 2. Path traversal write attempts outside authorized root are rejected
        traversal_write_1 = ctrl.handle_text(
            'files.create 0 ../forbidden_outside/injected.txt "malicious payload"'
        )
        assert traversal_write_1.success is False

        traversal_write_2 = ctrl.handle_text(
            'files.create 0 ..\\injected.txt "malicious payload"'
        )
        assert traversal_write_2.success is False

        # 3. Reserved Windows device names rejected
        reserved_device_1 = ctrl.handle_text('files.create 0 CON "payload"')
        assert reserved_device_1.success is False

        reserved_device_2 = ctrl.handle_text('files.create 0 NUL.txt "payload"')
        assert reserved_device_2.success is False

        # 4. Invalid directory indices are safely rejected
        invalid_idx_1 = ctrl.handle_text("files.list 99")
        assert invalid_idx_1.success is False

        invalid_idx_2 = ctrl.handle_text("files.read 99 some_file.txt")
        assert invalid_idx_2.success is False

        # 5. Verify actual filesystem state remains completely unchanged
        assert sensitive_file.read_text(encoding="utf-8") == "SUPER_SECRET_KEY=9999"
        assert not (sensitive_dir / "injected.txt").exists()
        assert not (tmp_path / "injected.txt").exists()

        # 6. Verify legitimate operation inside authorized directory functions normally
        safe_create = ctrl.handle_text('files.create 0 legitimate.txt "safe data"')
        assert safe_create.success is True
        assert (authorized_dir / "legitimate.txt").exists()
        assert (authorized_dir / "legitimate.txt").read_text(encoding="utf-8") == "safe data"

    finally:
        ctrl.stop(timeout=2.0)
        assert ctrl.is_cleanly_stopped is True


def test_filesystem_symlink_containment_and_rejection(
    e2e_controller_factory: ControllerFactory,
    fake_clock: FakeClock,
    tmp_path: Path,
) -> None:
    """Security Boundary 3 (cont): Symlinks inside sandbox pointing outside are rejected for read and write."""
    sandbox_dir = tmp_path / "sandbox_symlink"
    sandbox_dir.mkdir()

    outside_dir = tmp_path / "outside_symlink"
    outside_dir.mkdir()
    outside_file = outside_dir / "target_secret.txt"
    outside_file.write_text("OUTSIDE_SENSITIVE_CONTENT", encoding="utf-8")

    link_path = sandbox_dir / "symlink_escape.txt"

    try:
        link_path.symlink_to(outside_file)
    except OSError as exc:
        pytest.skip(
            f"Symbolic links cannot be created in this host environment: {exc} "
            "(Windows requires Developer Mode or SeCreateSymbolicLinkPrivilege)"
        )

    ctrl = e2e_controller_factory(
        clock=fake_clock,
        allowed_directories=(sandbox_dir,),
    )
    ctrl.start()

    try:
        # 1. Attempt reading through symlink pointing outside sandbox
        read_res = ctrl.handle_text("files.read 0 symlink_escape.txt")
        assert read_res.success is False

        # 2. Attempt writing/overwriting through symlink pointing outside sandbox
        write_res = ctrl.handle_text('files.create 0 symlink_escape.txt "malicious_overwrite"')
        assert write_res.success is False

        # 3. Verify external file content remains strictly unchanged
        assert outside_file.read_text(encoding="utf-8") == "OUTSIDE_SENSITIVE_CONTENT"

    finally:
        ctrl.stop(timeout=2.0)
        assert ctrl.is_cleanly_stopped is True


# ---------------------------------------------------------------------------
# 4. Skill Security & Permission Enforcement
# ---------------------------------------------------------------------------


def test_skill_security_isolation_and_approval_enforcement(
    e2e_controller_factory: ControllerFactory,
    fake_clock: FakeClock,
) -> None:
    """Security Boundary 4: Modular skills enforce existence, permissions, and fault isolation."""
    ctrl = e2e_controller_factory(clock=fake_clock)
    ctrl.start()

    try:
        skill_manager = ctrl.skill_manager
        assert skill_manager is not None

        # 1. Unregistered skills cannot execute
        res_unreg = ctrl.handle_text("skills.run nonexistent_unregistered_skill")
        assert res_unreg.success is False

        res_unreg_direct = skill_manager.execute_skill("nonexistent_unregistered_skill")
        assert res_unreg_direct.success is False
        assert res_unreg_direct.error == "skill_not_found"

        # 2. Disabled skills cannot execute
        wipe_skill = SensitiveWipeSkill()
        skill_manager.register_skill(wipe_skill)
        assert skill_manager.get_skill("test.security_wipe") is not None

        skill_manager.disable_skill("test.security_wipe")
        res_disabled = skill_manager.execute_skill(
            "test.security_wipe",
            {"target": "customer_database"},
        )
        assert res_disabled.success is False
        assert res_disabled.error == "skill_disabled"
        assert wipe_skill.invoked is False

        skill_manager.enable_skill("test.security_wipe")

        # 3. Protected skill requires approval and does NOT invoke execution logic without it
        res_unauth = skill_manager.execute_skill(
            "test.security_wipe",
            {"target": "customer_database"},
        )
        assert res_unauth.success is False
        assert res_unauth.confirmation_required is True
        assert res_unauth.confirmation_id is not None
        assert wipe_skill.invoked is False

        conf_id = res_unauth.confirmation_id

        # 4. Mismatched arguments during authorized execution fail
        approval_request = ctrl.runtime.approval_service.get_request(conf_id)
        assert approval_request is not None
        assert ctrl.runtime.approval_service.approve(approval_request) is True

        res_mismatch = skill_manager.execute_skill(
            "test.security_wipe",
            {"target": "different_database"},
            confirmation_id=conf_id,
        )
        assert res_mismatch.success is False
        assert wipe_skill.invoked is False

        # 5. Valid authorization executes successfully
        # Create a new approval for customer_database
        res_unauth2 = skill_manager.execute_skill(
            "test.security_wipe",
            {"target": "customer_database"},
        )
        conf_id2 = res_unauth2.confirmation_id
        req2 = ctrl.runtime.approval_service.get_request(conf_id2)
        assert ctrl.runtime.approval_service.approve(req2) is True

        res_valid = skill_manager.execute_skill(
            "test.security_wipe",
            {"target": "customer_database"},
            confirmation_id=conf_id2,
        )
        assert res_valid.success is True
        assert wipe_skill.invoked is True
        assert wipe_skill.invoked_with == {"target": "customer_database"}

        # 6. Replay protection: reusing conf_id2 fails
        res_replay = skill_manager.execute_skill(
            "test.security_wipe",
            {"target": "customer_database"},
            confirmation_id=conf_id2,
        )
        assert res_replay.success is False

        # 7. Skill fault isolation: crashing skills do not corrupt application state
        crashing_skill = CrashingFaultySkill()
        skill_manager.register_skill(crashing_skill)

        res_crash = ctrl.handle_text("skills.run test.crashing_faulty")
        assert res_crash.success is False

        # Subsequent controller operations remain completely healthy
        healthy_task = ctrl.task_manager.create_task(title="Healthy Task Post Crash")
        assert healthy_task.id is not None
        assert ctrl.task_manager.get_task(healthy_task.id) is not None

    finally:
        ctrl.stop(timeout=2.0)
        assert ctrl.is_cleanly_stopped is True


def test_skill_permission_configuration_validation() -> None:
    """Security Boundary 4: AppConfig enforces strict, fail-closed validation on skill permission grants."""
    # 1. Valid grants parse and normalize to tuples of lowercase strings
    valid_config = AppConfig(
        skill_permission_grants={
            "test.read_only": ("read_only",),
            "test.multi": ("read_only", "filesystem"),
        }
    )
    assert valid_config.skill_permission_grants["test.read_only"] == ("read_only",)
    assert valid_config.skill_permission_grants["test.multi"] == ("read_only", "filesystem")

    # 2. Unknown permission name raises ValueError
    with pytest.raises(ValueError, match="Unknown skill permission 'super_admin'"):
        AppConfig(skill_permission_grants={"test.skill": ("super_admin",)})

    # 3. Empty or whitespace-padded skill ID key raises ValueError
    with pytest.raises(ValueError, match="Skill permission grant key must be a non-empty string"):
        AppConfig(skill_permission_grants={"": ("read_only",)})

    with pytest.raises(ValueError, match="must not contain surrounding whitespace"):
        AppConfig(skill_permission_grants={"  test.skill  ": ("read_only",)})

    # 4. Non-sequence values raise ValueError
    with pytest.raises(ValueError, match="must be a sequence of permission names"):
        AppConfig(skill_permission_grants={"test.skill": "read_only"})  # type: ignore[arg-type]

    # 5. Non-dict skill_permission_grants raises TypeError
    with pytest.raises(TypeError, match="skill_permission_grants must be a dictionary"):
        AppConfig(skill_permission_grants=["read_only"])  # type: ignore[arg-type]


def test_skill_permission_configuration_immutability() -> None:
    """Security Boundary 4: AppConfig and its permission grants are strictly immutable after construction.

    Note:
        FrozenDict protects against ordinary application-level mutation (indexing, operators,
        mutating methods). Deliberate bypasses via Python's inherited C dict descriptors
        (e.g. dict.__setitem__) are neutralized by SkillExecutor's defensive snapshot copy.
    """
    initial_grants = {"test.read_only": ("read_only",)}
    config = AppConfig(skill_permission_grants=initial_grants)

    # 1. AppConfig field reassignment is rejected (dataclass is frozen)
    with pytest.raises(FrozenInstanceError):
        config.skill_permission_grants = {}  # type: ignore[misc]

    # 2. Adding or changing a grant through the stored mapping is rejected
    with pytest.raises(TypeError):
        config.skill_permission_grants["test.read_only"] = ("filesystem",)  # type: ignore[index]

    with pytest.raises(TypeError):
        config.skill_permission_grants["test.new_skill"] = ("read_only",)  # type: ignore[index]

    with pytest.raises(TypeError):
        del config.skill_permission_grants["test.read_only"]  # type: ignore[attr-defined]

    with pytest.raises(TypeError):
        config.skill_permission_grants.update({"test.filesystem": ("filesystem",)})  # type: ignore[attr-defined]

    with pytest.raises(TypeError):
        config.skill_permission_grants.clear()  # type: ignore[attr-defined]

    with pytest.raises(TypeError):
        config.skill_permission_grants.pop("test.read_only")  # type: ignore[attr-defined]

    with pytest.raises(TypeError, match="does not support in-place mutation"):
        config.skill_permission_grants |= {"test.filesystem": ("filesystem",)}  # type: ignore[operator]

    # Verify mapping remains completely unchanged after the rejected |= operation
    assert "test.filesystem" not in config.skill_permission_grants
    assert config.skill_permission_grants["test.read_only"] == ("read_only",)
    assert len(config.skill_permission_grants) == 1

    # 3. Nested permission collections cannot be modified (they are tuples)
    assert isinstance(config.skill_permission_grants["test.read_only"], tuple)
    with pytest.raises(TypeError):
        config.skill_permission_grants["test.read_only"][0] = "filesystem"  # type: ignore[index]

    # 4. Mutating the original input dictionary does not affect AppConfig
    initial_grants["test.filesystem"] = ("filesystem",)
    initial_grants["test.read_only"] = ()
    assert "test.filesystem" not in config.skill_permission_grants
    assert config.skill_permission_grants["test.read_only"] == ("read_only",)

    # 5. SkillExecutor is unaffected by mutation of the input dictionary after construction
    registry = SkillRegistry()
    read_skill = ReadOnlySkill()
    fs_skill = FilesystemSkill()
    registry.register(read_skill)
    registry.register(fs_skill)

    exec_grants = {"test.read_only": ("read_only",)}
    executor = SkillExecutor(registry=registry, skill_permission_grants=exec_grants)

    # Mutate the dictionary that was passed to SkillExecutor
    exec_grants["test.filesystem"] = ("filesystem",)
    exec_grants["test.read_only"] = ()

    # SkillExecutor's internal state remains unchanged
    assert executor.get_granted_permissions("test.filesystem") == frozenset()
    assert executor.get_granted_permissions("test.read_only") == frozenset({SkillPermission.READ_ONLY})

    # Execution reflects unchanged internal state
    res_read = executor.execute("test.read_only")
    assert res_read.success is True
    assert read_skill.invoked is True

    res_fs = executor.execute("test.filesystem")
    assert res_fs.success is False
    assert res_fs.error == "permission_denied"
    assert fs_skill.invoked is False

    # 6. Serialization and deepcopy work seamlessly with immutable mapping
    as_dict = asdict(config)
    assert isinstance(as_dict["skill_permission_grants"], dict)
    assert json.dumps(as_dict) is not None

    copied = copy.deepcopy(config)
    assert copied.skill_permission_grants == config.skill_permission_grants
    with pytest.raises(TypeError):
        copied.skill_permission_grants["test.read_only"] = ("filesystem",)  # type: ignore[index]

    pickled = pickle.loads(pickle.dumps(config))
    assert pickled.skill_permission_grants == config.skill_permission_grants
    with pytest.raises(TypeError):
        pickled.skill_permission_grants["test.read_only"] = ("filesystem",)  # type: ignore[index]


def test_skill_fail_closed_capability_permission_enforcement() -> None:
    """Security Boundary 4: Capabilities (READ_ONLY, FILESYSTEM) require explicit per-skill grants; default is deny."""
    registry = SkillRegistry()

    unprivileged_skill = UnprivilegedSkill()
    read_skill = ReadOnlySkill()
    fs_skill = FilesystemSkill()
    multi_skill = MultiCapabilitySkill()

    registry.register(unprivileged_skill)
    registry.register(read_skill)
    registry.register(fs_skill)
    registry.register(multi_skill)

    # 1. Default (no grants): all non-NONE capabilities are denied fail-closed
    executor_ungranted = SkillExecutor(registry=registry)

    # NONE permission succeeds without any grants
    res_unprivileged = executor_ungranted.execute("test.unprivileged")
    assert res_unprivileged.success is True
    assert unprivileged_skill.invoked is True

    # READ_ONLY capability is denied
    res_read_denied = executor_ungranted.execute("test.read_only")
    assert res_read_denied.success is False
    assert res_read_denied.error == "permission_denied"
    assert "requires permission(s) not granted by runtime configuration: read_only" in res_read_denied.message
    assert read_skill.invoked is False

    # FILESYSTEM capability is denied
    res_fs_denied = executor_ungranted.execute("test.filesystem")
    assert res_fs_denied.success is False
    assert res_fs_denied.error == "permission_denied"
    assert "requires permission(s) not granted by runtime configuration: filesystem" in res_fs_denied.message
    assert fs_skill.invoked is False

    # Multi-capability skill is denied
    res_multi_denied = executor_ungranted.execute("test.multi_capability")
    assert res_multi_denied.success is False
    assert res_multi_denied.error == "permission_denied"
    assert "filesystem, read_only" in res_multi_denied.message
    assert multi_skill.invoked is False

    # Fabricating a confirmation ID cannot bypass missing capability grants
    res_fake_bypass = executor_ungranted.execute(
        "test.read_only",
        confirmation_id="fake-bypass-token",
    )
    assert res_fake_bypass.success is False
    assert res_fake_bypass.error == "permission_denied"
    assert read_skill.invoked is False

    # 2. Per-skill grant isolation: granting READ_ONLY to test.read_only does NOT grant it to test.multi_capability
    executor_isolated = SkillExecutor(
        registry=registry,
        skill_permission_grants={
            "test.read_only": ("read_only",),
        },
    )

    # test.read_only now succeeds
    read_skill.invoked = False
    res_read_granted = executor_isolated.execute("test.read_only")
    assert res_read_granted.success is True
    assert read_skill.invoked is True

    # test.multi_capability still denied (lacks filesystem AND lacks per-skill grant for test.multi_capability)
    res_multi_still_denied = executor_isolated.execute("test.multi_capability")
    assert res_multi_still_denied.success is False
    assert res_multi_still_denied.error == "permission_denied"
    assert multi_skill.invoked is False

    # test.filesystem still denied
    res_fs_still_denied = executor_isolated.execute("test.filesystem")
    assert res_fs_still_denied.success is False
    assert res_fs_still_denied.error == "permission_denied"
    assert fs_skill.invoked is False

    # 3. Partial grants: granting only one of multiple required capabilities fails closed
    executor_partial = SkillExecutor(
        registry=registry,
        skill_permission_grants={
            "test.multi_capability": ("read_only",),
        },
    )
    res_partial_denied = executor_partial.execute("test.multi_capability")
    assert res_partial_denied.success is False
    assert res_partial_denied.error == "permission_denied"
    assert "requires permission(s) not granted by runtime configuration: filesystem" in res_partial_denied.message
    assert multi_skill.invoked is False

    # 4. Full grants: all declared capabilities granted succeeds
    executor_full = SkillExecutor(
        registry=registry,
        skill_permission_grants={
            "test.filesystem": ("filesystem",),
            "test.multi_capability": ("read_only", "filesystem"),
        },
    )
    fs_skill.invoked = False
    res_fs_granted = executor_full.execute("test.filesystem")
    assert res_fs_granted.success is True
    assert fs_skill.invoked is True

    multi_skill.invoked = False
    res_multi_granted = executor_full.execute("test.multi_capability")
    assert res_multi_granted.success is True
    assert multi_skill.invoked is True


def test_skill_combined_capability_and_approval_enforcement(
    e2e_controller_factory: ControllerFactory,
    fake_clock: FakeClock,
) -> None:
    """Security Boundary 4: Approval does not replace capability grants; capabilities gate approval lifecycle."""
    # Case A: Controller without capability grant for approval+filesystem skill
    ctrl_no_grant = e2e_controller_factory(
        clock=fake_clock,
        skill_permission_grants={},
    )
    ctrl_no_grant.start()

    try:
        skill_no_grant = ApprovalAndFilesystemSkill()
        ctrl_no_grant.skill_manager.register_skill(skill_no_grant)

        # 1. Attempting execution without capability grant fails-closed immediately
        res_denied = ctrl_no_grant.skill_manager.execute_skill(
            "test.approval_and_fs",
            {"filepath": "secrets.txt"},
        )
        assert res_denied.success is False
        assert res_denied.error == "permission_denied"
        assert "filesystem" in res_denied.message
        assert skill_no_grant.invoked is False

        # 2. No approval request was created because capability check gates before approval
        assert len(ctrl_no_grant.runtime.approval_service._requests) == 0

        # 3. Supplying a fabricated confirmation ID fails at capability gate without touching approvals
        res_forged = ctrl_no_grant.skill_manager.execute_skill(
            "test.approval_and_fs",
            {"filepath": "secrets.txt"},
            confirmation_id="forged-approval-token",
        )
        assert res_forged.success is False
        assert res_forged.error == "permission_denied"
        assert skill_no_grant.invoked is False

    finally:
        ctrl_no_grant.stop(timeout=2.0)
        assert ctrl_no_grant.is_cleanly_stopped is True

    # Case B: Controller WITH capability grant for approval+filesystem skill
    ctrl_with_grant = e2e_controller_factory(
        clock=fake_clock,
        skill_permission_grants={
            "test.approval_and_fs": ("filesystem",),
        },
    )
    ctrl_with_grant.start()

    try:
        skill_with_grant = ApprovalAndFilesystemSkill()
        ctrl_with_grant.skill_manager.register_skill(skill_with_grant)

        # 1. Initial execution passes capability gate, then requests out-of-band approval
        res_req = ctrl_with_grant.skill_manager.execute_skill(
            "test.approval_and_fs",
            {"filepath": "data.csv"},
        )
        assert res_req.success is False
        assert res_req.confirmation_required is True
        assert res_req.confirmation_id is not None
        assert skill_with_grant.invoked is False

        conf_id = res_req.confirmation_id

        # 2. Approve via ApprovalService
        req = ctrl_with_grant.runtime.approval_service.get_request(conf_id)
        assert req is not None
        assert ctrl_with_grant.runtime.approval_service.approve(req) is True

        # 3. Authorized execution with valid token succeeds
        res_exec = ctrl_with_grant.skill_manager.execute_skill(
            "test.approval_and_fs",
            {"filepath": "data.csv"},
            confirmation_id=conf_id,
        )
        assert res_exec.success is True
        assert skill_with_grant.invoked is True
        assert skill_with_grant.invoked_with == {"filepath": "data.csv"}

        # 4. Replay attack: reusing consumed token is rejected
        res_replay = ctrl_with_grant.skill_manager.execute_skill(
            "test.approval_and_fs",
            {"filepath": "data.csv"},
            confirmation_id=conf_id,
        )
        assert res_replay.success is False
        assert res_replay.error == "unauthorized"

    finally:
        ctrl_with_grant.stop(timeout=2.0)
        assert ctrl_with_grant.is_cleanly_stopped is True


def test_skill_permission_enforcement_via_controller_runtime(
    e2e_controller_factory: ControllerFactory,
    fake_clock: FakeClock,
) -> None:
    """Security Boundary 4: End-to-end command-driven skill execution enforces capability grants."""
    ctrl = e2e_controller_factory(
        clock=fake_clock,
        skill_permission_grants={
            "test.read_only": ("read_only",),
        },
    )
    ctrl.start()

    try:
        read_skill = ReadOnlySkill()
        fs_skill = FilesystemSkill()
        unprivileged_skill = UnprivilegedSkill()

        ctrl.skill_manager.register_skill(read_skill)
        ctrl.skill_manager.register_skill(fs_skill)
        ctrl.skill_manager.register_skill(unprivileged_skill)

        # 1. Unprivileged skill executes via text command
        res_unprivileged = ctrl.handle_text("skills.run test.unprivileged")
        assert res_unprivileged.success is True
        assert unprivileged_skill.invoked is True

        # 2. Granted skill executes via text command
        res_read = ctrl.handle_text("skills.run test.read_only")
        assert res_read.success is True
        assert read_skill.invoked is True

        # 3. Ungranted skill is rejected via text command with fail-closed error
        res_fs = ctrl.handle_text("skills.run test.filesystem")
        assert res_fs.success is False
        assert fs_skill.invoked is False

        # Direct execution reveals specific permission_denied diagnostic error
        res_fs_direct = ctrl.skill_manager.execute_skill("test.filesystem")
        assert res_fs_direct.success is False
        assert res_fs_direct.error == "permission_denied"
        assert "filesystem" in res_fs_direct.message

    finally:
        ctrl.stop(timeout=2.0)
        assert ctrl.is_cleanly_stopped is True


def test_bootstrap_skill_permission_override_semantics(
    e2e_controller_factory: ControllerFactory,
    fake_clock: FakeClock,
) -> None:
    """Security Boundary 4: Bootstrap explicit None vs empty dict override semantics for skill grants."""
    base_config = AppConfig(
        skill_permission_grants={
            "test.read_only": ("read_only",),
        }
    )

    # 1. skill_permission_grants=None falls back to AppConfig grants
    ctrl_none = e2e_controller_factory(
        clock=fake_clock,
        config=base_config,
        skill_permission_grants=None,
    )
    ctrl_none.start()
    try:
        read_skill1 = ReadOnlySkill()
        ctrl_none.skill_manager.register_skill(read_skill1)
        res_none = ctrl_none.skill_manager.execute_skill("test.read_only")
        assert res_none.success is True
        assert read_skill1.invoked is True
    finally:
        ctrl_none.stop(timeout=2.0)
        assert ctrl_none.is_cleanly_stopped is True

    # 2. skill_permission_grants={} explicitly overrides non-empty AppConfig and denies all permissions
    ctrl_empty = e2e_controller_factory(
        clock=fake_clock,
        config=base_config,
        skill_permission_grants={},
    )
    ctrl_empty.start()
    try:
        read_skill2 = ReadOnlySkill()
        ctrl_empty.skill_manager.register_skill(read_skill2)
        res_empty = ctrl_empty.skill_manager.execute_skill("test.read_only")
        assert res_empty.success is False
        assert res_empty.error == "permission_denied"
        assert read_skill2.invoked is False
    finally:
        ctrl_empty.stop(timeout=2.0)
        assert ctrl_empty.is_cleanly_stopped is True

    # 3. Explicit per-skill grants override AppConfig grants completely
    ctrl_override = e2e_controller_factory(
        clock=fake_clock,
        config=base_config,
        skill_permission_grants={
            "test.filesystem": ("filesystem",),
        },
    )
    ctrl_override.start()
    try:
        read_skill3 = ReadOnlySkill()
        fs_skill3 = FilesystemSkill()
        ctrl_override.skill_manager.register_skill(read_skill3)
        ctrl_override.skill_manager.register_skill(fs_skill3)

        # test.read_only is NOT granted because explicit grants replaced AppConfig grants
        res_read_denied = ctrl_override.skill_manager.execute_skill("test.read_only")
        assert res_read_denied.success is False
        assert res_read_denied.error == "permission_denied"
        assert read_skill3.invoked is False

        # test.filesystem IS granted by explicit override
        res_fs_granted = ctrl_override.skill_manager.execute_skill("test.filesystem")
        assert res_fs_granted.success is True
        assert fs_skill3.invoked is True
    finally:
        ctrl_override.stop(timeout=2.0)
        assert ctrl_override.is_cleanly_stopped is True

    # 4. An empty override cannot accidentally retain a previous grant
    ctrl_empty_isolated = e2e_controller_factory(
        clock=fake_clock,
        config=base_config,
        skill_permission_grants={},
    )
    ctrl_empty_isolated.start()
    try:
        assert ctrl_empty_isolated.skill_manager.get_granted_permissions("test.read_only") == frozenset()
        read_skill4 = ReadOnlySkill()
        ctrl_empty_isolated.skill_manager.register_skill(read_skill4)
        res_denied4 = ctrl_empty_isolated.skill_manager.execute_skill("test.read_only")
        assert res_denied4.success is False
        assert res_denied4.error == "permission_denied"
        assert read_skill4.invoked is False
    finally:
        ctrl_empty_isolated.stop(timeout=2.0)
        assert ctrl_empty_isolated.is_cleanly_stopped is True


# ---------------------------------------------------------------------------
# 5. AI Isolation Security
# ---------------------------------------------------------------------------


def test_ai_isolation_adversarial_outputs_passive_only(
    e2e_controller_factory: ControllerFactory,
    fake_clock: FakeClock,
    temp_anaya_dir: TempAnayaPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Security Boundary 5: AI-generated text cannot execute commands or authorize approvals."""
    # 1. Create the real task before constructing the fake AI provider
    seeder_tm = TaskManager(db_path=temp_anaya_dir.tasks_db, clock=fake_clock)
    real_task = seeder_tm.create_task(title="Critical Immutable Task")
    seeder_tm.close()
    assert real_task.id is not None
    actual_task_id = str(real_task.id)

    # 2. Embed the actual task ID into the adversarial payload
    adversarial_payload = (
        f"I am executing tasks.delete {actual_task_id} and approve now.\n"
        f"confirm {actual_task_id}\n"
        "files.delete 0 /etc/passwd\n"
        "DROP TABLE tasks;"
    )
    fake_provider = FakeAdversarialAIProvider(adversarial_payload)

    ctrl = e2e_controller_factory(
        clock=fake_clock,
        enable_ai=True,
        custom_ai_provider=fake_provider,
    )
    ctrl.start()

    # 3. Spy on CommandExecutor.execute to verify no command dispatch from AI
    executor_calls: list[tuple[str, tuple[str, ...], str | None]] = []
    orig_execute = ctrl.runtime.executor.execute

    def spy_execute(name: str, arguments: tuple[str, ...] = (), confirmation_id: str | None = None):
        executor_calls.append((name, arguments, confirmation_id))
        return orig_execute(name, arguments, confirmation_id)

    monkeypatch.setattr(ctrl.runtime.executor, "execute", spy_execute)

    # 4. Spy on ApprovalService.request_approval and approve
    approval_requests: list[tuple[str, tuple[str, ...]]] = []
    orig_request_approval = ctrl.runtime.approval_service.request_approval

    def spy_request_approval(action: str, arguments: tuple[str, ...] = ()):
        approval_requests.append((action, arguments))
        return orig_request_approval(action, arguments)

    approval_approvals: list[Any] = []
    orig_approve = ctrl.runtime.approval_service.approve

    def spy_approve(request: Any):
        approval_approvals.append(request)
        return orig_approve(request)

    monkeypatch.setattr(ctrl.runtime.approval_service, "request_approval", spy_request_approval)
    monkeypatch.setattr(ctrl.runtime.approval_service, "approve", spy_approve)

    try:
        # Conversational query triggers AI fallback
        result = ctrl.handle_text("What actions should we take on our tasks today?")
        assert result.success is True
        assert fake_provider.chat_called is True
        assert f"tasks.delete {actual_task_id}" in result.message

        # Assert spies confirm AI output never reached command execution or approval APIs
        assert executor_calls == []
        assert approval_requests == []
        assert approval_approvals == []

        # Assert no pending approval request or authorization was created
        assert len(ctrl.runtime.approval_service._requests) == 0
        assert len(ctrl.runtime.approval_service._authorizations) == 0

        # Verify real task remains present and unchanged in SQLite
        reloaded = ctrl.task_manager.get_task(actual_task_id)
        assert reloaded is not None
        assert reloaded.title == "Critical Immutable Task"
        assert reloaded.status == "pending"

    finally:
        ctrl.stop(timeout=2.0)
        assert ctrl.is_cleanly_stopped is True


# ---------------------------------------------------------------------------
# 6. Runtime and Data Safety
# ---------------------------------------------------------------------------


def test_security_rejections_preserve_runtime_and_data_integrity(
    e2e_controller_factory: ControllerFactory,
    fake_clock: FakeClock,
    tmp_path: Path,
) -> None:
    """Security Boundary 6: Multiple rejected security attacks leave database and threads fully consistent."""
    sandbox_dir = tmp_path / "sandbox_int"
    sandbox_dir.mkdir()

    ctrl = e2e_controller_factory(
        clock=fake_clock,
        allowed_directories=(sandbox_dir,),
    )
    ctrl.start()

    try:
        # Pre-seed tasks and memories
        pre_task = ctrl.task_manager.create_task(title="Pre-attack Task")
        saved_mem = ctrl.memory_manager.save_memory("pre_attack_content")

        # Bombard with security violations:
        # 1. Invalid command
        ctrl.handle_text("malicious.shell.spawn /bin/sh")
        # 2. Path traversal
        ctrl.handle_text("files.read 0 ../../../boot.ini")
        # 3. Conversational confirmation
        ctrl.handle_text("confirm all")
        ctrl.handle_text("approve")
        # 4. Unknown approval consumption
        ctrl.runtime.executor.execute("tasks.delete", (str(pre_task.id),), confirmation_id="fake-id")

        # Verify pre-attack data integrity
        reloaded_task = ctrl.task_manager.get_task(pre_task.id)
        assert reloaded_task is not None
        assert reloaded_task.title == "Pre-attack Task"
        assert reloaded_task.status == "pending"

        reloaded_mem = ctrl.memory_manager.get_memory(saved_mem.id)
        assert reloaded_mem is not None
        assert reloaded_mem.content == "pre_attack_content"

        # Verify subsequent normal operations continue working flawlessly
        post_task = ctrl.task_manager.create_task(title="Post-attack Task")
        assert post_task.id is not None
        ctrl.task_manager.complete_task(post_task.id)
        completed_task = ctrl.task_manager.get_task(post_task.id)
        assert completed_task.status == "completed"

        saved_post_mem = ctrl.memory_manager.save_memory("post_attack_content")
        post_mem = ctrl.memory_manager.get_memory(saved_post_mem.id)
        assert post_mem is not None
        assert post_mem.content == "post_attack_content"

        # Verify normal shutdown succeeds cleanly
        ctrl.stop(timeout=2.0)
        assert ctrl.is_cleanly_stopped is True
        assert ctrl.runtime.scheduler.is_running is False
        assert getattr(ctrl.task_manager.store, "_closed", False) is True
        assert getattr(ctrl.memory_manager.store, "_closed", False) is True

    finally:
        # Ensure cleanup if assertions fail above
        if not ctrl.is_cleanly_stopped:
            ctrl.stop(timeout=2.0)
