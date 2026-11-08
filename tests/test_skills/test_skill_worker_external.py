"""Comprehensive test suite for external skill execution inside worker (Phase 10.4.3).

Covers:
- Dynamic loading and execution of external skill packages from ./package
- Input validation and contract enforcement inside the worker
- Failure modes: missing package, missing manifest, identity mismatch, syntax errors,
  shadowing, missing class, invalid interface, instantiation errors, execution crashes
- Out-of-process subprocess execution of external skills via worker CLI
- Zero module leakage into host process
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import pytest

from app.skills.models import InputFieldSpec, Skill, SkillMetadata, SkillPermission, SkillResult
from app.skills.protocol import (
    ExecuteRequest,
    ExecuteResponse,
    decode_response,
    encode_request,
)
from app.skills.worker import (
    _execute_external_skill_in_worker,
    execute_skill_in_worker,
)


def _setup_external_package(
    base_dir: Path,
    pkg_name: str,
    manifest_dict: dict,
    modules: dict[str, str],
) -> Path:
    """Create a staged external package at base_dir / 'package'."""
    pkg_dir = base_dir / "package"
    pkg_dir.mkdir(parents=True, exist_ok=True)

    manifest_file = pkg_dir / "skill.json"
    manifest_file.write_text(json.dumps(manifest_dict, indent=2), encoding="utf-8")

    for mod_path, content in modules.items():
        dest = pkg_dir / mod_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")

    return pkg_dir


class TestWorkerExternalExecution:
    """Unit tests for _execute_external_skill_in_worker inside the worker runtime."""

    def test_valid_external_skill_execution(self, tmp_path: Path, monkeypatch):
        """Worker loads and executes a valid external skill, returning success."""
        manifest = {
            "schema_version": 1,
            "id": "user.multiplier",
            "name": "Multiplier",
            "description": "Multiplies two numbers",
            "version": "1.0.0",
            "entrypoint": "multiplier_module:MultiplySkill",
            "inputs": [
                {"name": "a", "type": "int", "description": "First number", "required": True},
                {"name": "b", "type": "int", "description": "Second number", "required": False, "default": 2},
            ],
        }
        code = (
            "from app.skills.models import Skill, SkillMetadata, InputFieldSpec, SkillResult\n"
            "class MultiplySkill(Skill):\n"
            "    @property\n"
            "    def metadata(self) -> SkillMetadata:\n"
            "        return SkillMetadata(\n"
            "            id='user.multiplier',\n"
            "            name='Multiplier',\n"
            "            description='Multiplies two numbers',\n"
            "            inputs=(\n"
            "                InputFieldSpec('a', int, required=True, description='First number'),\n"
            "                InputFieldSpec('b', int, required=False, default=2, description='Second number'),\n"
            "            ),\n"
            "        )\n"
            "    def execute(self, inputs):\n"
            "        res = inputs['a'] * inputs['b']\n"
            "        return SkillResult(success=True, data={'product': res}, message=f'Result is {res}')\n"
        )
        _setup_external_package(tmp_path, "multiplier", manifest, {"multiplier_module.py": code})
        monkeypatch.chdir(tmp_path)

        req = ExecuteRequest(
            request_id="req-worker-ext-1",
            skill_id="user.multiplier",
            inputs={"a": 6, "b": 7},
            entrypoint="multiplier_module:MultiplySkill",
        )
        resp = _execute_external_skill_in_worker(req)

        assert resp.request_id == "req-worker-ext-1"
        assert resp.success is True
        assert resp.error is None
        assert resp.result["success"] is True
        assert resp.result["data"] == {"product": 42}
        assert resp.result["message"] == "Result is 42"

    def test_valid_external_skill_with_helpers(self, tmp_path: Path, monkeypatch):
        """External skill successfully imports and uses helper modules within package."""
        manifest = {
            "schema_version": 1,
            "id": "user.modular_skill",
            "name": "Modular Skill",
            "description": "Uses a helper module",
            "version": "1.0.0",
            "entrypoint": "modular_entry:ModularSkill",
            "inputs": [
                {"name": "name", "type": "str", "description": "Name to greet", "required": False, "default": "Friend"},
            ],
        }
        helper_code = "def get_greeting(name: str) -> str: return f'Welcome, {name}!'"
        main_code = (
            "import helper\n"
            "from app.skills.models import Skill, SkillMetadata, InputFieldSpec, SkillResult\n"
            "class ModularSkill(Skill):\n"
            "    @property\n"
            "    def metadata(self) -> SkillMetadata:\n"
            "        return SkillMetadata(\n"
            "            id='user.modular_skill',\n"
            "            name='Modular Skill',\n"
            "            description='desc',\n"
            "            inputs=(InputFieldSpec('name', str, required=False, default='Friend', description='Name to greet'),),\n"
            "        )\n"
            "    def execute(self, inputs):\n"
            "        msg = helper.get_greeting(inputs.get('name', 'Friend'))\n"
            "        return SkillResult(success=True, data={'greeting': msg}, message=msg)\n"
        )
        _setup_external_package(tmp_path, "modular_skill", manifest, {
            "modular_entry.py": main_code,
            "helper.py": helper_code,
        })
        monkeypatch.chdir(tmp_path)

        req = ExecuteRequest(
            request_id="req-worker-mod-1",
            skill_id="user.modular_skill",
            inputs={"name": "Alice"},
            entrypoint="modular_entry:ModularSkill",
        )
        resp = _execute_external_skill_in_worker(req)

        assert resp.success is True
        assert resp.result["data"] == {"greeting": "Welcome, Alice!"}

    def test_missing_package_directory_fails_closed(self, tmp_path: Path, monkeypatch):
        """Worker fails closed when ./package directory does not exist."""
        monkeypatch.chdir(tmp_path)
        req = ExecuteRequest(
            request_id="req-worker-nopkg-1",
            skill_id="user.missing_pkg",
            inputs={},
        )
        resp = _execute_external_skill_in_worker(req)

        assert resp.success is False
        assert resp.error["code"] == "package_not_found"

    def test_missing_manifest_fails_closed(self, tmp_path: Path, monkeypatch):
        """Worker fails closed when skill.json is missing in ./package."""
        pkg_dir = tmp_path / "package"
        pkg_dir.mkdir()
        (pkg_dir / "pkg_main.py").write_text("pass", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        req = ExecuteRequest(
            request_id="req-worker-nomani-1",
            skill_id="user.no_mani",
            inputs={},
        )
        resp = _execute_external_skill_in_worker(req)

        assert resp.success is False
        assert resp.error["code"] == "missing_manifest"

    def test_identity_mismatch_fails_closed(self, tmp_path: Path, monkeypatch):
        """Worker fails closed when requested skill_id does not match manifest ID."""
        manifest = {
            "schema_version": 1,
            "id": "user.actual_id",
            "name": "Actual",
            "description": "desc",
            "version": "1.0.0",
            "entrypoint": "mismatch_mod:Skill",
        }
        _setup_external_package(tmp_path, "mismatch", manifest, {"mismatch_mod.py": "pass"})
        monkeypatch.chdir(tmp_path)

        req = ExecuteRequest(
            request_id="req-worker-mismatch-1",
            skill_id="user.different_id",
            inputs={},
        )
        resp = _execute_external_skill_in_worker(req)

        assert resp.success is False
        assert resp.error["code"] == "identity_mismatch"

    def test_entrypoint_shadowing_protected_module(self, tmp_path: Path, monkeypatch):
        """Worker rejects entrypoint attempting to shadow standard library or core modules."""
        manifest = {
            "schema_version": 1,
            "id": "user.shadow_test",
            "name": "Shadow",
            "description": "desc",
            "version": "1.0.0",
            "entrypoint": "sys:SysSkill",
        }
        _setup_external_package(tmp_path, "shadow_test", manifest, {"sys.py": "pass"})
        monkeypatch.chdir(tmp_path)

        req = ExecuteRequest(
            request_id="req-worker-shadow-1",
            skill_id="user.shadow_test",
            inputs={},
            entrypoint="sys:SysSkill",
        )
        resp = _execute_external_skill_in_worker(req)

        assert resp.success is False
        assert resp.error["code"] == "shadowing_disallowed"

    def test_entrypoint_module_file_missing(self, tmp_path: Path, monkeypatch):
        """Worker fails closed when declared entrypoint module file is missing."""
        manifest = {
            "schema_version": 1,
            "id": "user.missing_mod",
            "name": "Missing Mod",
            "description": "desc",
            "version": "1.0.0",
            "entrypoint": "missing_module:MySkill",
        }
        _setup_external_package(tmp_path, "missing_mod", manifest, {"other_mod.py": "pass"})
        monkeypatch.chdir(tmp_path)

        req = ExecuteRequest(
            request_id="req-worker-nomod-1",
            skill_id="user.missing_mod",
            inputs={},
            entrypoint="missing_module:MySkill",
        )
        resp = _execute_external_skill_in_worker(req)

        assert resp.success is False
        assert resp.error["code"] == "entrypoint_not_found"

    def test_import_error_in_external_skill(self, tmp_path: Path, monkeypatch):
        """Syntax or import errors in skill module return clean import_error response."""
        manifest = {
            "schema_version": 1,
            "id": "user.broken_import",
            "name": "Broken Import",
            "description": "desc",
            "version": "1.0.0",
            "entrypoint": "broken_mod:BrokenSkill",
        }
        _setup_external_package(tmp_path, "broken_import", manifest, {
            "broken_mod.py": "import non_existent_library_12345\nclass BrokenSkill: pass\n",
        })
        monkeypatch.chdir(tmp_path)

        req = ExecuteRequest(
            request_id="req-worker-badimp-1",
            skill_id="user.broken_import",
            inputs={},
            entrypoint="broken_mod:BrokenSkill",
        )
        resp = _execute_external_skill_in_worker(req)

        assert resp.success is False
        assert resp.error["code"] == "import_error"

    def test_class_not_found_in_module(self, tmp_path: Path, monkeypatch):
        """Worker returns class_not_found when the declared entrypoint class is absent."""
        manifest = {
            "schema_version": 1,
            "id": "user.noclass",
            "name": "No Class",
            "description": "desc",
            "version": "1.0.0",
            "entrypoint": "noclass_mod:NonExistentClass",
        }
        _setup_external_package(tmp_path, "noclass", manifest, {"noclass_mod.py": "x = 10\n"})
        monkeypatch.chdir(tmp_path)

        req = ExecuteRequest(
            request_id="req-worker-noclass-1",
            skill_id="user.noclass",
            inputs={},
            entrypoint="noclass_mod:NonExistentClass",
        )
        resp = _execute_external_skill_in_worker(req)

        assert resp.success is False
        assert resp.error["code"] == "class_not_found"

    def test_entrypoint_is_not_a_class(self, tmp_path: Path, monkeypatch):
        """Worker returns invalid_skill_class when entrypoint symbol is not a class."""
        manifest = {
            "schema_version": 1,
            "id": "user.notclass",
            "name": "Not Class",
            "description": "desc",
            "version": "1.0.0",
            "entrypoint": "notclass_mod:my_function",
        }
        _setup_external_package(tmp_path, "notclass", manifest, {
            "notclass_mod.py": "def my_function(): pass\n",
        })
        monkeypatch.chdir(tmp_path)

        req = ExecuteRequest(
            request_id="req-worker-notcls-1",
            skill_id="user.notclass",
            inputs={},
            entrypoint="notclass_mod:my_function",
        )
        resp = _execute_external_skill_in_worker(req)

        assert resp.success is False
        assert resp.error["code"] == "invalid_skill_class"

    def test_class_does_not_inherit_skill(self, tmp_path: Path, monkeypatch):
        """Worker returns invalid_skill_interface when class does not inherit from Skill."""
        manifest = {
            "schema_version": 1,
            "id": "user.noinherit",
            "name": "No Inherit",
            "description": "desc",
            "version": "1.0.0",
            "entrypoint": "noinherit_mod:PlainObject",
        }
        _setup_external_package(tmp_path, "noinherit", manifest, {
            "noinherit_mod.py": "class PlainObject: pass\n",
        })
        monkeypatch.chdir(tmp_path)

        req = ExecuteRequest(
            request_id="req-worker-noinh-1",
            skill_id="user.noinherit",
            inputs={},
            entrypoint="noinherit_mod:PlainObject",
        )
        resp = _execute_external_skill_in_worker(req)

        assert resp.success is False
        assert resp.error["code"] == "invalid_skill_interface"

    def test_skill_execute_raises_uncaught_exception(self, tmp_path: Path, monkeypatch):
        """Uncaught runtime exception in skill execute() returns clean error response without crashing."""
        manifest = {
            "schema_version": 1,
            "id": "user.crash_skill",
            "name": "Crash",
            "description": "desc",
            "version": "1.0.0",
            "entrypoint": "crash_mod:CrashSkill",
        }
        code = (
            "from app.skills.models import Skill, SkillMetadata\n"
            "class CrashSkill(Skill):\n"
            "    @property\n"
            "    def metadata(self) -> SkillMetadata:\n"
            "        return SkillMetadata(id='user.crash_skill', name='Crash', description='desc')\n"
            "    def execute(self, inputs):\n"
            "        raise ZeroDivisionError('Division by zero occurred in skill!')\n"
        )
        _setup_external_package(tmp_path, "crash_skill", manifest, {"crash_mod.py": code})
        monkeypatch.chdir(tmp_path)

        req = ExecuteRequest(
            request_id="req-worker-crash-1",
            skill_id="user.crash_skill",
            inputs={},
            entrypoint="crash_mod:CrashSkill",
        )
        resp = _execute_external_skill_in_worker(req)

        assert resp.success is False
        assert resp.error["code"] == "ZeroDivisionError"
        assert "Division by zero" in resp.error["message"]


class TestWorkerExternalSubprocess:
    """Out-of-process subprocess verification for worker executing external skills."""

    def test_worker_subprocess_executes_external_skill(self, tmp_path: Path):
        """Worker subprocess receives ExecuteRequest via stdin, executes external skill, and outputs response."""
        manifest = {
            "schema_version": 1,
            "id": "user.echo",
            "name": "Echo Skill",
            "description": "Echoes back the message",
            "version": "1.0.0",
            "entrypoint": "echo_mod:EchoSkill",
            "inputs": [
                {"name": "message", "type": "str", "description": "Message to echo", "required": True},
            ],
        }
        code = (
            "from app.skills.models import Skill, SkillMetadata, InputFieldSpec, SkillResult\n"
            "class EchoSkill(Skill):\n"
            "    @property\n"
            "    def metadata(self) -> SkillMetadata:\n"
            "        return SkillMetadata(\n"
            "            id='user.echo',\n"
            "            name='Echo Skill',\n"
            "            description='Echoes message',\n"
            "            inputs=(InputFieldSpec('message', str, required=True, description='msg'),),\n"
            "        )\n"
            "    def execute(self, inputs):\n"
            "        msg = inputs['message']\n"
            "        return SkillResult(success=True, data={'echo': msg}, message=f'Echo: {msg}')\n"
        )
        _setup_external_package(tmp_path, "echo", manifest, {"echo_mod.py": code})

        req = ExecuteRequest(
            request_id="req-subproc-ext-1",
            skill_id="user.echo",
            inputs={"message": "Anaya Sandboxed Worker Test"},
            entrypoint="echo_mod:EchoSkill",
        )
        encoded_req = encode_request(req)

        proc = subprocess.Popen(
            [sys.executable, "-m", "app.skills.worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(tmp_path),
            text=True,
        )
        stdout_data, stderr_data = proc.communicate(input=encoded_req, timeout=10.0)

        assert proc.returncode == 0
        resp = decode_response(stdout_data.strip())
        assert resp.request_id == "req-subproc-ext-1"
        assert resp.success is True
        assert resp.error is None
        assert resp.result["data"] == {"echo": "Anaya Sandboxed Worker Test"}

    def test_worker_subprocess_isolation_no_host_import(self, tmp_path: Path):
        """External skill executed inside worker subprocess leaves zero module trace in the host Python process."""
        marker_module_name = "unique_isolated_marker_module_xyz"
        manifest = {
            "schema_version": 1,
            "id": "user.marker_skill",
            "name": "Marker",
            "description": "desc",
            "version": "1.0.0",
            "entrypoint": f"{marker_module_name}:MarkerSkill",
        }
        code = (
            "from app.skills.models import Skill, SkillMetadata, SkillResult\n"
            "class MarkerSkill(Skill):\n"
            "    @property\n"
            "    def metadata(self) -> SkillMetadata:\n"
            "        return SkillMetadata(id='user.marker_skill', name='Marker', description='desc')\n"
            "    def execute(self, inputs):\n"
            "        return SkillResult(success=True, data={'status': 'ok'}, message='Marker executed')\n"
        )
        _setup_external_package(tmp_path, "marker_skill", manifest, {f"{marker_module_name}.py": code})

        # Host invariant check before execution
        assert marker_module_name not in sys.modules

        req = ExecuteRequest(
            request_id="req-marker-1",
            skill_id="user.marker_skill",
            inputs={},
            entrypoint=f"{marker_module_name}:MarkerSkill",
        )
        encoded_req = encode_request(req)

        proc = subprocess.Popen(
            [sys.executable, "-m", "app.skills.worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(tmp_path),
            text=True,
        )
        stdout_data, _ = proc.communicate(input=encoded_req, timeout=10.0)

        assert proc.returncode == 0
        resp = decode_response(stdout_data.strip())
        assert resp.success is True

        # Host invariant check after execution: external code was NEVER loaded in host
        assert marker_module_name not in sys.modules
