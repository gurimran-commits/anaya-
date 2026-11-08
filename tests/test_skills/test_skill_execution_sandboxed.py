"""Comprehensive test suite for secure external skill execution and supervisor integration (Phase 10.4.3).

Covers:
- End-to-end flow: Filesystem discovery -> SkillRegistry -> SkillExecutor -> SkillProcessSupervisor -> Worker
- Dynamic staging of external packages into per-execution session scratch
- Strict host-side security authority: contract validation, permissions, and approval gating
- Host process memory isolation: external skill code is never imported into host Python runtime
- Fail-closed behavior on staging failures, supervisor unavailability, and invalid inputs
- Clean session cleanup and scratch teardown
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest.mock as mock
import uuid
import pytest

from app.security.approval_service import ApprovalService
from app.skills.discovery import discover_external_skills
from app.skills.models import Skill, SkillMetadata, SkillResult
from app.skills.protocol import ExecuteRequest
from app.skills.skill_executor import SkillExecutor
from app.skills.skill_registry import SkillRegistry
from app.skills.supervisor import SkillProcessSupervisor

IS_WINDOWS = sys.platform == "win32"


def _create_external_package(
    root: Path,
    pkg_name: str,
    manifest_dict: dict,
    code: str,
    entrypoint_filename: str = "main.py",
) -> Path:
    """Helper to create a valid external skill package under a skills root."""
    pkg_dir = root / pkg_name
    pkg_dir.mkdir(parents=True, exist_ok=True)

    manifest_file = pkg_dir / "skill.json"
    manifest_file.write_text(json.dumps(manifest_dict, indent=2), encoding="utf-8")

    code_file = pkg_dir / entrypoint_filename
    code_file.write_text(code, encoding="utf-8")

    return pkg_dir


class TestExternalSkillExecutionEndToEnd:
    """End-to-end integration tests for external skill discovery, staging, and isolated execution."""

    def test_full_external_skill_roundtrip(self, tmp_path: Path):
        """Discovered external skill executes out-of-process via supervisor and returns validated result."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()

        pkg_name = "math_adder"
        skill_id = f"user.{pkg_name}"
        manifest = {
            "schema_version": 1,
            "id": skill_id,
            "name": "Math Adder",
            "description": "Adds two integers",
            "version": "1.0.0",
            "entrypoint": "adder_mod:AdderSkill",
            "inputs": [
                {"name": "a", "type": "int", "description": "First operand", "required": True},
                {"name": "b", "type": "int", "description": "Second operand", "required": False, "default": 10},
            ],
        }
        code = (
            "from app.skills.models import Skill, SkillMetadata, InputFieldSpec, SkillResult\n"
            "class AdderSkill(Skill):\n"
            "    @property\n"
            "    def metadata(self) -> SkillMetadata:\n"
            "        return SkillMetadata(\n"
            "            id='user.math_adder',\n"
            "            name='Math Adder',\n"
            "            description='Adds two integers',\n"
            "            inputs=(\n"
            "                InputFieldSpec('a', int, required=True, description='First operand'),\n"
            "                InputFieldSpec('b', int, required=False, default=10, description='Second operand'),\n"
            "            ),\n"
            "        )\n"
            "    def execute(self, inputs):\n"
            "        total = inputs['a'] + inputs['b']\n"
            "        return SkillResult(success=True, data={'total': total}, message=f'Sum: {total}')\n"
        )
        _create_external_package(skills_root, pkg_name, manifest, code, "adder_mod.py")

        # Invariant check: module not in host
        assert "adder_mod" not in sys.modules

        # 1. Discover skills
        disc_result = discover_external_skills(root_path=skills_root)
        discovered = disc_result.valid_skills
        assert len(discovered) == 1
        proxy = discovered[0]
        assert proxy.metadata.id == skill_id
        assert proxy.is_external is True

        # 2. Register into registry
        registry = SkillRegistry()
        registry.register(proxy)

        # 3. Configure supervisor and executor
        supervisor = SkillProcessSupervisor(skills_root=skills_root)
        try:
            executor = SkillExecutor(registry=registry, supervisor=supervisor)

            # 4. Execute external skill through executor
            res = executor.execute(skill_id, {"a": 15, "b": 25})

            assert isinstance(res, SkillResult)
            assert res.success is True
            assert res.error is None
            assert res.data == {"total": 40}
            assert res.message == "Sum: 40"

            # 5. Verify host memory isolation: external code was NEVER loaded into host process
            assert "adder_mod" not in sys.modules
        finally:
            supervisor.shutdown(timeout=2.0)

    def test_external_skill_default_inputs_handling(self, tmp_path: Path):
        """Host populates default inputs before passing to supervisor worker."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()

        pkg_name = "def_input_tool"
        skill_id = f"user.{pkg_name}"
        manifest = {
            "schema_version": 1,
            "id": skill_id,
            "name": "Default Input Tool",
            "description": "Tests default input handling",
            "version": "1.0.0",
            "entrypoint": "def_mod:DefSkill",
            "inputs": [
                {"name": "x", "type": "int", "description": "Required X", "required": True},
                {"name": "y", "type": "int", "description": "Default Y", "required": False, "default": 100},
            ],
        }
        code = (
            "from app.skills.models import Skill, SkillMetadata, InputFieldSpec, SkillResult\n"
            "class DefSkill(Skill):\n"
            "    @property\n"
            "    def metadata(self) -> SkillMetadata:\n"
            "        return SkillMetadata(\n"
            "            id='user.def_input_tool',\n"
            "            name='Default Input Tool',\n"
            "            description='Tests default input handling',\n"
            "            inputs=(\n"
            "                InputFieldSpec('x', int, required=True),\n"
            "                InputFieldSpec('y', int, required=False, default=100),\n"
            "            ),\n"
            "        )\n"
            "    def execute(self, inputs):\n"
            "        return SkillResult(success=True, data={'x': inputs['x'], 'y': inputs['y']})\n"
        )
        _create_external_package(skills_root, pkg_name, manifest, code, "def_mod.py")

        registry = SkillRegistry()
        for s in discover_external_skills(root_path=skills_root).valid_skills:
            registry.register(s)

        supervisor = SkillProcessSupervisor(skills_root=skills_root)
        try:
            executor = SkillExecutor(registry=registry, supervisor=supervisor)
            # Omit y to test default population
            res = executor.execute(skill_id, {"x": 50})

            assert res.success is True
            assert res.data == {"x": 50, "y": 100}
        finally:
            supervisor.shutdown(timeout=2.0)


class TestHostSecurityAuthorityEnforcement:
    """Host acts as security authority and validates contracts before worker dispatch."""

    def test_host_validates_inputs_before_supervisor_dispatch(self, tmp_path: Path):
        """Host-side validation fails closed on invalid input type without invoking supervisor."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()

        pkg_name = "strict_tool"
        skill_id = f"user.{pkg_name}"
        manifest = {
            "schema_version": 1,
            "id": skill_id,
            "name": "Strict Tool",
            "description": "Validates inputs strictly",
            "version": "1.0.0",
            "entrypoint": "strict_mod:StrictSkill",
            "inputs": [
                {"name": "val", "type": "int", "description": "An int value", "required": True},
            ],
        }
        code = "class StrictSkill: pass\n"
        _create_external_package(skills_root, pkg_name, manifest, code, "strict_mod.py")

        registry = SkillRegistry()
        for s in discover_external_skills(root_path=skills_root).valid_skills:
            registry.register(s)

        # Mock supervisor to verify execute() is NOT called when validation fails
        mock_supervisor = SkillProcessSupervisor(skills_root=skills_root)
        executed_requests = []
        orig_execute = mock_supervisor.execute

        def tracking_execute(req, package_path=None):
            executed_requests.append(req)
            return orig_execute(req, package_path=package_path)

        mock_supervisor.execute = tracking_execute
        try:
            executor = SkillExecutor(registry=registry, supervisor=mock_supervisor)

            # Pass string instead of int
            res = executor.execute(skill_id, {"val": "not an int"})

            assert res.success is False
            assert res.error == "validation_error"
            # Invariant: supervisor was NEVER invoked
            assert len(executed_requests) == 0
        finally:
            mock_supervisor.shutdown(timeout=2.0)

    def test_disabled_external_skill_fails_closed(self, tmp_path: Path):
        """Disabled external skill is rejected by host without worker dispatch."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()

        pkg_name = "disabled_tool"
        skill_id = f"user.{pkg_name}"
        manifest = {
            "schema_version": 1,
            "id": skill_id,
            "name": "Disabled Tool",
            "description": "Tests disablement",
            "version": "1.0.0",
            "entrypoint": "dis_mod:DisSkill",
        }
        _create_external_package(skills_root, pkg_name, manifest, "pass", "dis_mod.py")

        registry = SkillRegistry()
        for s in discover_external_skills(root_path=skills_root).valid_skills:
            registry.register(s)

        # Disable skill in registry
        registry.disable(skill_id)

        supervisor = SkillProcessSupervisor(skills_root=skills_root)
        try:
            executor = SkillExecutor(registry=registry, supervisor=supervisor)
            res = executor.execute(skill_id, {})

            assert res.success is False
            assert res.error == "skill_disabled"
        finally:
            supervisor.shutdown(timeout=2.0)

    def test_external_skill_without_supervisor_fails_closed(self, tmp_path: Path):
        """Attempting to execute an external skill when no supervisor is configured returns external_skill_execution_unavailable."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()

        pkg_name = "no_sup_tool"
        skill_id = f"user.{pkg_name}"
        manifest = {
            "schema_version": 1,
            "id": skill_id,
            "name": "No Sup Tool",
            "description": "No supervisor test",
            "version": "1.0.0",
            "entrypoint": "main:Skill",
        }
        _create_external_package(skills_root, pkg_name, manifest, "pass", "main.py")

        registry = SkillRegistry()
        for s in discover_external_skills(root_path=skills_root).valid_skills:
            registry.register(s)

        executor = SkillExecutor(registry=registry, supervisor=None)
        res = executor.execute(skill_id, {})

        assert res.success is False
        assert res.error == "external_skill_execution_unavailable"

    def test_approval_gated_external_skill(self, tmp_path: Path):
        """External skill declaring requires_approval prompts for approval and succeeds once approved."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()

        pkg_name = "privileged_tool"
        skill_id = f"user.{pkg_name}"
        manifest = {
            "schema_version": 1,
            "id": skill_id,
            "name": "Privileged Tool",
            "description": "Requires user approval",
            "version": "1.0.0",
            "entrypoint": "priv_mod:PrivSkill",
            "requires_approval": True,
            "inputs": [
                {"name": "cmd", "type": "str", "description": "Action name", "required": True},
            ],
        }
        code = (
            "from app.skills.models import Skill, SkillMetadata, InputFieldSpec, SkillResult\n"
            "class PrivSkill(Skill):\n"
            "    @property\n"
            "    def metadata(self) -> SkillMetadata:\n"
            "        return SkillMetadata(\n"
            "            id='user.privileged_tool',\n"
            "            name='Privileged Tool',\n"
            "            description='Requires user approval',\n"
            "            requires_approval=True,\n"
            "            inputs=(InputFieldSpec('cmd', str, required=True),),\n"
            "        )\n"
            "    def execute(self, inputs):\n"
            "        return SkillResult(success=True, data={'executed': inputs['cmd']})\n"
        )
        _create_external_package(skills_root, pkg_name, manifest, code, "priv_mod.py")

        registry = SkillRegistry()
        for s in discover_external_skills(root_path=skills_root).valid_skills:
            registry.register(s)

        approval_svc = ApprovalService()
        supervisor = SkillProcessSupervisor(skills_root=skills_root)
        try:
            executor = SkillExecutor(registry=registry, supervisor=supervisor, approval_service=approval_svc)

            # 1. First execution requires approval
            res1 = executor.execute(skill_id, {"cmd": "purge"})
            assert res1.success is False
            assert res1.confirmation_required is True
            assert res1.confirmation_id is not None
            conf_id = res1.confirmation_id

            # 2. Grant approval
            req_obj = approval_svc.get_request(conf_id)
            assert req_obj is not None
            assert approval_svc.approve(req_obj) is True

            # 3. Second execution with confirmation_id succeeds
            res2 = executor.execute(skill_id, {"cmd": "purge"}, confirmation_id=conf_id)
            assert res2.success is True
            assert res2.data == {"executed": "purge"}
        finally:
            supervisor.shutdown(timeout=2.0)


class TestStagingFailurePreventsWorkerLaunch:
    """Fail-closed behavior when package staging encounters security or format errors."""

    def test_staging_failure_prevents_worker_spawn(self, tmp_path: Path):
        """When staging fails (e.g. package modified to include disallowed file), supervisor does not spawn worker."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()

        pkg_name = "tampered_pkg"
        skill_id = f"user.{pkg_name}"
        manifest = {
            "schema_version": 1,
            "id": skill_id,
            "name": "Tampered Pkg",
            "description": "Contains disallowed file",
            "version": "1.0.0",
            "entrypoint": "main:MySkill",
        }
        pkg_dir = _create_external_package(skills_root, pkg_name, manifest, "pass", "main.py")

        registry = SkillRegistry()
        for s in discover_external_skills(root_path=skills_root).valid_skills:
            registry.register(s)

        # Inject disallowed file after discovery
        (pkg_dir / "bad_script.sh").write_text("#!/bin/sh\nexit 1", encoding="utf-8")

        supervisor = SkillProcessSupervisor(skills_root=skills_root)
        try:
            executor = SkillExecutor(registry=registry, supervisor=supervisor)
            res = executor.execute(skill_id, {})

            assert res.success is False
            assert res.error in ("worker_startup_failed", "staging_error", "disallowed_file_extension")
        finally:
            supervisor.shutdown(timeout=2.0)


class TestExternalSkillSandboxEnforcement:
    """Rigorous regression tests verifying elimination of unsandboxed external skill execution."""

    def test_external_skill_sandbox_disabled_fails_closed(self, tmp_path: Path):
        """External skill with sandbox disabled MUST fail closed without temp dir, staging, or subprocess."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        pkg_name = "unsafe_attempt"
        skill_id = f"user.{pkg_name}"
        manifest = {
            "schema_version": 1,
            "id": skill_id,
            "name": "Unsafe",
            "description": "d",
            "version": "1.0.0",
            "entrypoint": "main:MySkill",
        }
        pkg_dir = _create_external_package(skills_root, pkg_name, manifest, "pass", "main.py")

        # Supervisor configured with sandbox disabled
        supervisor = SkillProcessSupervisor(use_sandbox=False, skills_root=skills_root)
        try:
            req = ExecuteRequest(
                request_id="req-sandbox-disabled-1",
                skill_id=skill_id,
                inputs={},
                entrypoint="main:MySkill",
            )

            with mock.patch("subprocess.Popen") as mock_popen, \
                 mock.patch("tempfile.TemporaryDirectory") as mock_tempdir, \
                 mock.patch("app.skills.staging.stage_external_package") as mock_staging:
                resp = supervisor.execute(req, package_path=pkg_dir)

            assert resp.success is False
            assert resp.error is not None
            assert resp.error["code"] == "worker_startup_failed"
            assert "AppContainer" in resp.error["message"]

            # Invariant: NEVER created temporary directory
            mock_tempdir.assert_not_called()
            # Invariant: NEVER staged external package
            mock_staging.assert_not_called()
            # Invariant: NEVER spawned ordinary subprocess
            mock_popen.assert_not_called()

            # Capacity invariant: slot was released
            acquired = supervisor._semaphore.acquire(blocking=False)
            assert acquired is True
            supervisor._semaphore.release()
        finally:
            supervisor.shutdown(timeout=2.0)

    def test_external_skill_missing_launcher_fails_closed(self, tmp_path: Path):
        """External skill with missing launcher fails closed without spawning worker."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        pkg_name = "no_launcher_pkg"
        skill_id = f"user.{pkg_name}"
        manifest = {
            "schema_version": 1,
            "id": skill_id,
            "name": "No Launcher",
            "description": "d",
            "version": "1.0.0",
            "entrypoint": "main:MySkill",
        }
        pkg_dir = _create_external_package(skills_root, pkg_name, manifest, "pass", "main.py")

        supervisor = SkillProcessSupervisor(skills_root=skills_root)
        # Explicitly remove launcher
        supervisor._launcher = None
        try:
            req = ExecuteRequest(
                request_id="req-no-launcher-1",
                skill_id=skill_id,
                inputs={},
                entrypoint="main:MySkill",
            )
            with mock.patch("subprocess.Popen") as mock_popen:
                resp = supervisor.execute(req, package_path=pkg_dir)

            assert resp.success is False
            assert resp.error["code"] == "worker_startup_failed"
            mock_popen.assert_not_called()

            # Capacity released
            acquired = supervisor._semaphore.acquire(blocking=False)
            assert acquired is True
            supervisor._semaphore.release()
        finally:
            supervisor.shutdown(timeout=2.0)

    def test_external_skill_missing_session_registry_fails_closed(self, tmp_path: Path):
        """External skill with missing session registry fails closed without spawning worker."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        pkg_name = "no_registry_pkg"
        skill_id = f"user.{pkg_name}"
        manifest = {
            "schema_version": 1,
            "id": skill_id,
            "name": "No Registry",
            "description": "d",
            "version": "1.0.0",
            "entrypoint": "main:MySkill",
        }
        pkg_dir = _create_external_package(skills_root, pkg_name, manifest, "pass", "main.py")

        supervisor = SkillProcessSupervisor(skills_root=skills_root)
        # Explicitly remove session registry
        supervisor._session_registry = None
        try:
            req = ExecuteRequest(
                request_id="req-no-reg-1",
                skill_id=skill_id,
                inputs={},
                entrypoint="main:MySkill",
            )
            with mock.patch("subprocess.Popen") as mock_popen:
                resp = supervisor.execute(req, package_path=pkg_dir)

            assert resp.success is False
            assert resp.error["code"] == "worker_startup_failed"
            mock_popen.assert_not_called()

            # Capacity released
            acquired = supervisor._semaphore.acquire(blocking=False)
            assert acquired is True
            supervisor._semaphore.release()
        finally:
            supervisor.shutdown(timeout=2.0)

    @pytest.mark.skipif(not IS_WINDOWS, reason="AppContainer launcher requires Windows")
    def test_external_skill_uses_launcher_and_never_subprocess_fallback(self, tmp_path: Path):
        """When full sandbox components are present, launcher is used and subprocess.Popen is NEVER called."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        pkg_name = "sandboxed_pkg"
        skill_id = f"user.{pkg_name}"
        manifest = {
            "schema_version": 1,
            "id": skill_id,
            "name": "Sandboxed",
            "description": "d",
            "version": "1.0.0",
            "entrypoint": "main:MySkill",
        }
        code = (
            "from app.skills.models import Skill, SkillMetadata, SkillResult\n"
            "class MySkill(Skill):\n"
            "    @property\n"
            "    def metadata(self) -> SkillMetadata:\n"
            "        return SkillMetadata(id='user.sandboxed_pkg', name='Sandboxed', description='d')\n"
            "    def execute(self, inputs):\n"
            "        return SkillResult(success=True, data={'status': 'sandboxed'})\n"
        )
        pkg_dir = _create_external_package(skills_root, pkg_name, manifest, code, "main.py")

        supervisor = SkillProcessSupervisor(skills_root=skills_root)
        try:
            req = ExecuteRequest(
                request_id="req-sandboxed-verify-1",
                skill_id=skill_id,
                inputs={},
                entrypoint="main:MySkill",
            )

            # Ordinary subprocess.Popen must NOT be called at all during sandboxed execution
            with mock.patch("subprocess.Popen", side_effect=AssertionError("Ordinary subprocess.Popen called!")):
                resp = supervisor.execute(req, package_path=pkg_dir)

            assert resp.success is True
            assert resp.result["data"] == {"status": "sandboxed"}
        finally:
            supervisor.shutdown(timeout=2.0)

    def test_builtin_skill_without_package_path_preserves_supported_behavior(self):
        """Built-in skills without package_path retain standard supported execution."""
        supervisor = SkillProcessSupervisor(use_sandbox=False)
        try:
            req = ExecuteRequest(
                request_id="req-builtin-1",
                skill_id="example.greeting",
                inputs={"name": "Alice"},
            )
            resp = supervisor.execute(req)

            assert resp.success is True
            assert "Alice" in resp.result["message"]
        finally:
            supervisor.shutdown(timeout=2.0)

    def test_executor_defense_rejects_external_proxy_in_process(self, tmp_path: Path):
        """SkillExecutor._execute_in_process rejects external proxies directly."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        pkg_name = "proxy_test"
        skill_id = f"user.{pkg_name}"
        manifest = {
            "schema_version": 1,
            "id": skill_id,
            "name": "Proxy",
            "description": "d",
            "version": "1.0.0",
            "entrypoint": "main:Skill",
        }
        _create_external_package(skills_root, pkg_name, manifest, "pass", "main.py")

        discovered = discover_external_skills(root_path=skills_root).valid_skills
        proxy = discovered[0]

        registry = SkillRegistry()
        executor = SkillExecutor(registry=registry)
        res = executor._execute_in_process(proxy, skill_id, {})

        assert res.success is False
        assert res.error == "external_skill_in_process_denied"
        assert "cannot be executed directly in-process" in res.message

    def test_executor_defense_rejects_user_id_on_plain_skill_object(self):
        """SkillExecutor._execute_in_process rejects user.* IDs even if Skill object lacks is_external flag."""
        class MaliciousPlainSkill(Skill):
            @property
            def metadata(self) -> SkillMetadata:
                return SkillMetadata(id="user.spoofed", name="Spoofed", description="d")

            def execute(self, inputs):
                return SkillResult(success=True, data="in_process_breach")

        plain_skill = MaliciousPlainSkill()
        registry = SkillRegistry()
        executor = SkillExecutor(registry=registry)
        res = executor._execute_in_process(plain_skill, "user.spoofed", {})

        assert res.success is False
        assert res.error == "external_skill_in_process_denied"
        assert "cannot be executed directly in-process" in res.message

    def test_supervisor_package_validation_rejects_mismatched_skill_id(self, tmp_path: Path):
        """Supervisor rejects external execution where skill_id does not match directory name."""
        pkg_dir = tmp_path / "calc"
        pkg_dir.mkdir()
        supervisor = SkillProcessSupervisor(skills_root=tmp_path)
        try:
            req = ExecuteRequest(
                request_id="req-mismatch-1",
                skill_id="user.weather",
                inputs={},
            )
            resp = supervisor.execute(req, package_path=pkg_dir)

            assert resp.success is False
            assert resp.error["code"] == "identity_mismatch"
        finally:
            supervisor.shutdown(timeout=2.0)

    def test_supervisor_package_validation_rejects_builtin_with_package_path(self, tmp_path: Path):
        """Supervisor rejects package_path passed for a built-in skill."""
        supervisor = SkillProcessSupervisor(skills_root=tmp_path)
        try:
            req = ExecuteRequest(
                request_id="req-builtin-pkg-1",
                skill_id="math.add",
                inputs={"a": 1, "b": 2},
            )
            resp = supervisor.execute(req, package_path=tmp_path)

            assert resp.success is False
            assert resp.error["code"] == "invalid_package_path"
        finally:
            supervisor.shutdown(timeout=2.0)

    def test_supervisor_package_validation_rejects_outside_skills_root(self, tmp_path: Path):
        """Supervisor rejects package_path outside configured skills_root."""
        skills_root = tmp_path / "valid_root"
        skills_root.mkdir()
        outside_dir = tmp_path / "outside_root" / "tool"
        outside_dir.mkdir(parents=True)

        supervisor = SkillProcessSupervisor(skills_root=skills_root)
        try:
            req = ExecuteRequest(
                request_id="req-outside-root-1",
                skill_id="user.tool",
                inputs={},
            )
            resp = supervisor.execute(req, package_path=outside_dir)

            assert resp.success is False
            assert resp.error["code"] == "invalid_package_path"
            assert "outside configured trusted skills root" in resp.error["message"]
        finally:
            supervisor.shutdown(timeout=2.0)

    def test_supervisor_rejects_external_skill_without_package_path(self, tmp_path: Path):
        """Supervisor rejects user.* skill request when package_path is None."""
        supervisor = SkillProcessSupervisor(skills_root=tmp_path)
        try:
            req = ExecuteRequest(
                request_id="req-missing-pkg-1",
                skill_id="user.orphan_skill",
                inputs={},
            )
            resp = supervisor.execute(req, package_path=None)

            assert resp.success is False
            assert resp.error["code"] == "missing_package_path"
        finally:
            supervisor.shutdown(timeout=2.0)

    def test_supervisor_rejects_external_skill_when_no_skills_root_configured(self, tmp_path: Path):
        """Supervisor rejects external skill execution when no skills_root is configured."""
        pkg_dir = tmp_path / "tool"
        pkg_dir.mkdir()
        supervisor = SkillProcessSupervisor(skills_root=None)
        try:
            req = ExecuteRequest(
                request_id="req-no-root-1",
                skill_id="user.tool",
                inputs={},
            )
            resp = supervisor.execute(req, package_path=pkg_dir)

            assert resp.success is False
            assert resp.error["code"] == "unconfigured_skills_root"
            assert "no trusted external skills root is configured" in resp.error["message"]
        finally:
            supervisor.shutdown(timeout=2.0)
