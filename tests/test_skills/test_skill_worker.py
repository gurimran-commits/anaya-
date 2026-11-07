"""Comprehensive unit and subprocess tests for the standalone skill worker (app.skills.worker)."""

import io
import json
import subprocess
import sys
from typing import Any

import pytest

from app.skills.builtin.example_skill import GreetingSkill
from app.skills.models import (
    InputFieldSpec,
    Skill,
    SkillMetadata,
    SkillPermission,
    SkillResult,
)
from app.skills.protocol import (
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    ExecuteRequest,
    decode_response,
    encode_request,
)
from app.skills.skill_registry import SkillRegistry
from app.skills.worker import (
    EXIT_FATAL_ERROR,
    EXIT_PROTOCOL_ERROR,
    EXIT_SUCCESS,
    run_worker,
)


class ApprovalRequiringSkill(Skill):
    """Test skill declaring explicit requires_approval=True."""

    metadata = SkillMetadata(
        id="test.requires_approval",
        name="Approval Skill",
        description="Requires explicit human approval.",
        inputs=(),
        requires_approval=True,
    )

    def execute(self, inputs: dict[str, Any]) -> SkillResult:
        return SkillResult(success=True, message="Privileged operation executed.")


class FilesystemPermissionSkill(Skill):
    """Test skill declaring required_permissions=(SkillPermission.FILESYSTEM,)."""

    metadata = SkillMetadata(
        id="test.requires_filesystem",
        name="Filesystem Skill",
        description="Requires filesystem access.",
        inputs=(),
        required_permissions=(SkillPermission.FILESYSTEM,),
    )

    def execute(self, inputs: dict[str, Any]) -> SkillResult:
        return SkillResult(success=True, message="Filesystem accessed.")


class ReadOnlyPermissionSkill(Skill):
    """Test skill declaring required_permissions=(SkillPermission.READ_ONLY,)."""

    metadata = SkillMetadata(
        id="test.requires_readonly",
        name="Read-Only Skill",
        description="Requires read-only capability permission.",
        inputs=(),
        required_permissions=(SkillPermission.READ_ONLY,),
    )

    def execute(self, inputs: dict[str, Any]) -> SkillResult:
        return SkillResult(success=True, message="Read-only operation.")


class TestSkillWorkerUnit:
    """In-memory unit tests using mock I/O streams for fast, deterministic worker evaluation."""

    def test_valid_request_handling(self):
        """Worker executes a valid registered harmless skill and returns an accurate success response."""
        req = ExecuteRequest(
            request_id="req-unit-1",
            skill_id="example.greeting",
            inputs={"name": "Bob"},
        )
        stdin = io.StringIO(encode_request(req) + "\n")
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = run_worker(stdin=stdin, stdout=stdout, stderr=stderr)
        assert code == EXIT_SUCCESS

        out_lines = [line for line in stdout.getvalue().splitlines() if line.strip()]
        assert len(out_lines) == 1

        resp = decode_response(out_lines[0])
        assert resp.request_id == "req-unit-1"
        assert resp.success is True
        assert resp.error is None
        assert resp.result is not None
        assert resp.result["message"] == "Hello, Bob!"

    def test_malformed_request_handling(self):
        """Malformed input produces a protocol error response without crashing."""
        stdin = io.StringIO("{this is not json\n")
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = run_worker(stdin=stdin, stdout=stdout, stderr=stderr)
        assert code == EXIT_PROTOCOL_ERROR

        out_lines = [line for line in stdout.getvalue().splitlines() if line.strip()]
        assert len(out_lines) == 1

        resp = decode_response(out_lines[0])
        assert resp.success is False
        assert resp.result is None
        assert resp.error is not None
        assert resp.error["code"] in ("protocol_error", "malformed_request")

        # Diagnostics must be emitted to stderr
        assert "ProtocolError" in stderr.getvalue() or "Malformed" in stderr.getvalue()

    def test_unknown_operation_handling(self):
        """Requesting an unregistered/unsupported skill produces an explicit unsupported_skill error."""
        req = ExecuteRequest(
            request_id="req-unknown-op",
            skill_id="external.custom_math",
            inputs={"x": 10},
        )
        stdin = io.StringIO(encode_request(req) + "\n")
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = run_worker(stdin=stdin, stdout=stdout, stderr=stderr)
        assert code == EXIT_SUCCESS  # Protocol handled gracefully

        out_lines = [line for line in stdout.getvalue().splitlines() if line.strip()]
        assert len(out_lines) == 1

        resp = decode_response(out_lines[0])
        assert resp.request_id == "req-unknown-op"
        assert resp.success is False
        assert resp.error is not None
        assert resp.error["code"] == "unsupported_skill"
        assert "not supported by worker in Step 10.1" in resp.error["message"]

    def test_fail_closed_permission_and_approval_enforcement(self):
        """Worker strictly rejects skills requiring approval or capability permissions."""
        registry = SkillRegistry()
        registry.register(GreetingSkill())
        registry.register(ApprovalRequiringSkill())
        registry.register(FilesystemPermissionSkill())
        registry.register(ReadOnlyPermissionSkill())

        # 1. Skill requiring approval is rejected fail-closed
        req_approval = ExecuteRequest(
            request_id="req-appr-1",
            skill_id="test.requires_approval",
            inputs={},
        )
        stdin = io.StringIO(encode_request(req_approval) + "\n")
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = run_worker(stdin=stdin, stdout=stdout, stderr=stderr, registry=registry)
        assert code == EXIT_SUCCESS
        resp = decode_response(stdout.getvalue().strip())
        assert resp.success is False
        assert resp.error is not None
        assert resp.error["code"] == "permission_denied"
        assert "explicit human approval" in resp.error["message"]

        # 2. Skill requiring filesystem capability is rejected fail-closed
        req_fs = ExecuteRequest(
            request_id="req-fs-1",
            skill_id="test.requires_filesystem",
            inputs={},
        )
        stdin = io.StringIO(encode_request(req_fs) + "\n")
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = run_worker(stdin=stdin, stdout=stdout, stderr=stderr, registry=registry)
        assert code == EXIT_SUCCESS
        resp = decode_response(stdout.getvalue().strip())
        assert resp.success is False
        assert resp.error is not None
        assert resp.error["code"] == "permission_denied"
        assert "filesystem" in resp.error["message"]

        # 3. Skill requiring read-only capability is rejected fail-closed
        req_ro = ExecuteRequest(
            request_id="req-ro-1",
            skill_id="test.requires_readonly",
            inputs={},
        )
        stdin = io.StringIO(encode_request(req_ro) + "\n")
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = run_worker(stdin=stdin, stdout=stdout, stderr=stderr, registry=registry)
        assert code == EXIT_SUCCESS
        resp = decode_response(stdout.getvalue().strip())
        assert resp.success is False
        assert resp.error is not None
        assert resp.error["code"] == "permission_denied"
        assert "read_only" in resp.error["message"]

        # 4. Harmless greeting skill with no permissions executes successfully
        req_safe = ExecuteRequest(
            request_id="req-safe-1",
            skill_id="example.greeting",
            inputs={"name": "Alice"},
        )
        stdin = io.StringIO(encode_request(req_safe) + "\n")
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = run_worker(stdin=stdin, stdout=stdout, stderr=stderr, registry=registry)
        assert code == EXIT_SUCCESS
        resp = decode_response(stdout.getvalue().strip())
        assert resp.success is True
        assert resp.result["message"] == "Hello, Alice!"

    def test_exactly_one_protocol_response(self):
        """Worker writes exactly one JSON line to stdout."""
        req = ExecuteRequest(
            request_id="req-single-line",
            skill_id="example.greeting",
            inputs={},
        )
        stdin = io.StringIO(encode_request(req) + "\n")
        stdout = io.StringIO()
        stderr = io.StringIO()

        run_worker(stdin=stdin, stdout=stdout, stderr=stderr)
        raw_out = stdout.getvalue()
        lines = [line for line in raw_out.splitlines() if line.strip()]
        assert len(lines) == 1
        decode_response(lines[0])

    def test_no_diagnostic_output_on_stdout(self):
        """Stdout contains strictly valid JSON protocol messages; no log records or print noise."""
        req = ExecuteRequest(
            request_id="req-no-stdout-noise",
            skill_id="example.greeting",
            inputs={"name": "Alice"},
        )
        stdin = io.StringIO(encode_request(req) + "\n")
        stdout = io.StringIO()
        stderr = io.StringIO()

        run_worker(stdin=stdin, stdout=stdout, stderr=stderr)

        stdout_val = stdout.getvalue().strip()
        parsed = json.loads(stdout_val)
        assert isinstance(parsed, dict)
        assert "protocol_version" in parsed

    def test_diagnostics_directed_to_stderr(self):
        """Worker warnings, errors, and traces are routed strictly to stderr."""
        stdin = io.StringIO("not-valid-protocol\n")
        stdout = io.StringIO()
        stderr = io.StringIO()

        run_worker(stdin=stdin, stdout=stdout, stderr=stderr)
        assert len(stderr.getvalue()) > 0
        assert "[skill-worker]" in stderr.getvalue()

    def test_request_id_preserved_in_response(self):
        """The client's request_id is preserved identically in the worker's response."""
        unique_id = "req-custom-guid-98765-abcdef"
        req = ExecuteRequest(
            request_id=unique_id,
            skill_id="example.greeting",
            inputs={},
        )
        stdin = io.StringIO(encode_request(req) + "\n")
        stdout = io.StringIO()
        stderr = io.StringIO()

        run_worker(stdin=stdin, stdout=stdout, stderr=stderr)
        resp = decode_response(stdout.getvalue().strip())
        assert resp.request_id == unique_id

    def test_bounded_input_handling_and_rejection(self):
        """Requests exceeding MAX_MESSAGE_BYTES trigger an immediate oversized_message error."""
        huge_payload = "A" * (MAX_MESSAGE_BYTES + 100) + "\n"
        stdin = io.StringIO(huge_payload)
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = run_worker(stdin=stdin, stdout=stdout, stderr=stderr)
        assert code == EXIT_PROTOCOL_ERROR

        lines = [line for line in stdout.getvalue().splitlines() if line.strip()]
        assert len(lines) == 1
        resp = decode_response(lines[0])
        assert resp.success is False
        assert resp.error is not None
        assert resp.error["code"] == "oversized_message"

    def test_no_arbitrary_import_or_command_execution_from_request(self):
        """Request cannot specify code, import targets, module paths, or shell commands."""
        malicious_raw = json.dumps({
            "protocol_version": PROTOCOL_VERSION,
            "message_type": "execute_request",
            "request_id": "req-exploit-1",
            "skill_id": "example.greeting",
            "inputs": {},
            "timeout_seconds": 10.0,
            "module_path": "os.system",
            "command": "calc.exe",
        }) + "\n"

        stdin = io.StringIO(malicious_raw)
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = run_worker(stdin=stdin, stdout=stdout, stderr=stderr)
        assert code == EXIT_PROTOCOL_ERROR

        resp = decode_response(stdout.getvalue().strip())
        assert resp.success is False
        assert resp.error is not None
        assert "Unexpected field" in resp.error["message"]

    def test_skill_input_validation_failure(self):
        """Providing invalid argument types to a skill returns a validation_error response."""
        req = ExecuteRequest(
            request_id="req-val-fail",
            skill_id="example.greeting",
            inputs={"name": 12345},  # name must be str
        )
        stdin = io.StringIO(encode_request(req) + "\n")
        stdout = io.StringIO()
        stderr = io.StringIO()

        run_worker(stdin=stdin, stdout=stdout, stderr=stderr)
        resp = decode_response(stdout.getvalue().strip())
        assert resp.success is False
        assert resp.error is not None
        assert resp.error["code"] == "validation_error"


class TestSkillWorkerSubprocess:
    """Subprocess integration tests executing the worker as python -m app.skills.worker."""

    def test_subprocess_clean_exit_on_valid_request(self):
        """Worker exits with returncode 0 when executing a valid request via subprocess."""
        req = ExecuteRequest(
            request_id="subproc-1",
            skill_id="example.greeting",
            inputs={"name": "Alice"},
        )
        raw_in = (encode_request(req) + "\n").encode("utf-8")

        proc = subprocess.run(
            [sys.executable, "-m", "app.skills.worker"],
            input=raw_in,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )

        assert proc.returncode == 0
        stdout_str = proc.stdout.decode("utf-8").strip()
        resp = decode_response(stdout_str)
        assert resp.request_id == "subproc-1"
        assert resp.success is True
        assert resp.result["message"] == "Hello, Alice!"

    def test_subprocess_clean_handling_of_malformed_json(self):
        """Worker handles malformed input cleanly via subprocess without unhandled traceback."""
        raw_in = b"{malformed json string\n"

        proc = subprocess.run(
            [sys.executable, "-m", "app.skills.worker"],
            input=raw_in,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )

        assert proc.returncode == EXIT_PROTOCOL_ERROR
        stdout_str = proc.stdout.decode("utf-8").strip()
        resp = decode_response(stdout_str)
        assert resp.success is False
        assert resp.error is not None

        stderr_str = proc.stderr.decode("utf-8")
        assert "[skill-worker]" in stderr_str
        assert "Traceback" not in stdout_str

    def test_subprocess_empty_stdin_eof(self):
        """Worker cleanly exits on EOF without hang."""
        proc = subprocess.run(
            [sys.executable, "-m", "app.skills.worker"],
            input=b"",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
        assert proc.returncode == EXIT_PROTOCOL_ERROR
        stderr_str = proc.stderr.decode("utf-8")
        assert "Received empty input" in stderr_str

    def test_subprocess_duplicate_keys_rejection(self):
        """Subprocess strictly rejects duplicate JSON keys."""
        raw_in = (
            b'{"protocol_version":"1.0","message_type":"execute_request",'
            b'"request_id":"sub-dup","request_id":"sub-dup2","skill_id":"example.greeting",'
            b'"inputs":{},"timeout_seconds":10.0}\n'
        )
        proc = subprocess.run(
            [sys.executable, "-m", "app.skills.worker"],
            input=raw_in,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
        assert proc.returncode == EXIT_PROTOCOL_ERROR
        stdout_str = proc.stdout.decode("utf-8").strip()
        resp = decode_response(stdout_str)
        assert resp.success is False
        assert resp.error is not None
        assert "Duplicate key" in resp.error["message"]

    def test_subprocess_excessive_nesting_depth_rejection(self):
        """Subprocess rejects requests with excessive nesting depth (>32) without crash."""
        deep_dict: Any = "leaf"
        for i in range(40):
            deep_dict = {f"k_{i}": deep_dict}

        req = {
            "protocol_version": "1.0",
            "message_type": "execute_request",
            "request_id": "sub-deep",
            "skill_id": "example.greeting",
            "inputs": {"nested": deep_dict},
            "timeout_seconds": 10.0,
        }
        raw_in = (json.dumps(req) + "\n").encode("utf-8")

        proc = subprocess.run(
            [sys.executable, "-m", "app.skills.worker"],
            input=raw_in,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
        assert proc.returncode == EXIT_PROTOCOL_ERROR
        stdout_str = proc.stdout.decode("utf-8").strip()
        resp = decode_response(stdout_str)
        assert resp.success is False
        assert resp.error is not None
        assert "nesting depth" in resp.error["message"]

    def test_subprocess_unsupported_skill_fails_closed(self):
        """Subprocess returns explicit unsupported_skill error when an unregistered skill is requested."""
        req = {
            "protocol_version": "1.0",
            "message_type": "execute_request",
            "request_id": "sub-unsupported",
            "skill_id": "malicious.rmdir",
            "inputs": {},
            "timeout_seconds": 10.0,
        }
        raw_in = (json.dumps(req) + "\n").encode("utf-8")

        proc = subprocess.run(
            [sys.executable, "-m", "app.skills.worker"],
            input=raw_in,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
        assert proc.returncode == 0
        stdout_str = proc.stdout.decode("utf-8").strip()
        resp = decode_response(stdout_str)
        assert resp.success is False
        assert resp.error is not None
        assert resp.error["code"] == "unsupported_skill"

    def test_subprocess_oversized_payload_rejection(self):
        """Subprocess cleanly rejects oversized payload (>1 MB) without memory hang."""
        huge_raw = b"A" * (MAX_MESSAGE_BYTES + 100) + b"\n"
        proc = subprocess.run(
            [sys.executable, "-m", "app.skills.worker"],
            input=huge_raw,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
        assert proc.returncode == EXIT_PROTOCOL_ERROR
        stdout_str = proc.stdout.decode("utf-8").strip()
        resp = decode_response(stdout_str)
        assert resp.success is False
        assert resp.error is not None
        assert resp.error["code"] == "oversized_message"

    def test_subprocess_privileged_skill_execution_rejected(self):
        """Direct subprocess attempt to run a privileged skill with approval/permissions fails closed."""
        # Use python inline script to run worker with custom registry containing an approval skill
        script = (
            "from app.skills.skill_registry import SkillRegistry\n"
            "from app.skills.models import Skill, SkillMetadata, SkillPermission, SkillResult\n"
            "from app.skills.worker import run_worker\n"
            "class Sensitive(Skill):\n"
            "    metadata = SkillMetadata(id='priv.action', name='Priv', description='d', inputs=(), requires_approval=True)\n"
            "    def execute(self, inputs):\n"
            "        return SkillResult(True, message='compromised')\n"
            "reg = SkillRegistry()\n"
            "reg.register(Sensitive())\n"
            "run_worker(registry=reg)\n"
        )
        raw_req = json.dumps({
            "protocol_version": "1.0",
            "message_type": "execute_request",
            "request_id": "sub-priv-test",
            "skill_id": "priv.action",
            "inputs": {},
            "timeout_seconds": 10.0,
        }).encode("utf-8") + b"\n"

        proc = subprocess.run(
            [sys.executable, "-c", script],
            input=raw_req,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
        assert proc.returncode == 0
        stdout_str = proc.stdout.decode("utf-8").strip()
        resp = decode_response(stdout_str)
        assert resp.success is False
        assert resp.error is not None
        assert resp.error["code"] == "permission_denied"
        assert "explicit human approval" in resp.error["message"]
