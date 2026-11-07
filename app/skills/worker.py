"""Standalone skill worker process entry point for out-of-process execution.

This module is designed to be executed as a one-shot child process:
    python -m app.skills.worker

Responsibilities:
    - Read a single JSON request from stdin (bounded to MAX_MESSAGE_BYTES).
    - Parse and validate the IPC protocol using app.skills.protocol.
    - Dispatch supported skills under strict input contract validation.
    - Write exactly one JSON response to stdout followed by a newline.
    - Send all diagnostics, tracebacks, and operational logs to stderr.
    - Terminate cleanly after execution with an appropriate exit code.

Security Invariants:
    - NEVER accepts arbitrary module names, import paths, or executable paths.
    - NEVER executes arbitrary source code, eval, exec, or shell commands.
    - Does NOT dynamically load external code in Step 10.1 (returns explicit unsupported_skill).
    - Guarantees stdout is 100% clean and reserved exclusively for protocol messages.
"""

from __future__ import annotations

import copy
import json
import logging
import sys
from typing import Any, TextIO

from app.skills.builtin import register_builtin_skills
from app.skills.models import Skill, SkillPermission, SkillResult
from app.skills.protocol import (
    IDENTIFIER_PATTERN,
    MAX_ID_LENGTH,
    MAX_MESSAGE_BYTES,
    ExecuteRequest,
    ExecuteResponse,
    ProtocolError,
    ProtocolSizeError,
    create_error_response,
    decode_request,
    encode_response,
    serialize_skill_result,
)
from app.skills.skill_registry import SkillRegistry

# Configure dedicated stderr logger
logger = logging.getLogger("anaya.skills.worker")
logger.propagate = False
if not logger.handlers:
    _err_handler = logging.StreamHandler(sys.stderr)
    _err_handler.setFormatter(logging.Formatter("[skill-worker] %(levelname)s: %(message)s"))
    logger.addHandler(_err_handler)
logger.setLevel(logging.INFO)

# Exit Codes
EXIT_SUCCESS: int = 0
EXIT_PROTOCOL_ERROR: int = 1
EXIT_FATAL_ERROR: int = 2


def _validate_and_filter_inputs(
    skill: Skill,
    raw_inputs: dict[str, Any],
) -> tuple[bool, dict[str, Any], str | None]:
    """Validate requested inputs against declared InputFieldSpecs and reject unexpected fields."""
    specs_by_name = {spec.name: spec for spec in skill.metadata.inputs}

    # 1. Reject unexpected keys to prevent argument pollution
    unexpected = set(raw_inputs.keys()) - set(specs_by_name.keys())
    if unexpected:
        return False, {}, f"Unexpected input field(s) for skill '{skill.metadata.id}': {sorted(unexpected)}."

    processed: dict[str, Any] = {}
    for name, spec in specs_by_name.items():
        if name in raw_inputs:
            val = raw_inputs[name]
            is_valid, err = spec.validate(val)
            if not is_valid:
                return False, {}, err
            processed[name] = copy.deepcopy(val)
        else:
            if spec.required:
                return False, {}, f"Missing required input field: '{name}' for skill '{skill.metadata.id}'."
            processed[name] = copy.deepcopy(spec.default)

    return True, processed, None


def execute_skill_in_worker(
    request: ExecuteRequest,
    registry: SkillRegistry,
) -> ExecuteResponse:
    """Dispatch and execute a request against the worker's trusted skill registry."""
    skill = registry.get(request.skill_id)

    # In Step 10.1, only explicitly registered/built-in skills are supported.
    # External skill discovery and loading are deferred to Steps 10.2-10.4.
    if skill is None:
        logger.warning(
            "Skill '%s' requested but not found in worker registry. External skill loading is disabled in Step 10.1.",
            request.skill_id,
        )
        return create_error_response(
            request_id=request.request_id,
            error_code="unsupported_skill",
            error_message=(
                f"Skill '{request.skill_id}' is not registered or not supported by worker in Step 10.1. "
                "Out-of-process external skill execution requires Step 10.2 (supervisor) and Step 10.3 (isolation)."
            ),
        )

    if not registry.is_enabled(request.skill_id):
        return ExecuteResponse(
            request_id=request.request_id,
            success=False,
            result=serialize_skill_result(
                SkillResult(
                    success=False,
                    error="skill_disabled",
                    message=f"Skill '{request.skill_id}' is currently disabled.",
                )
            ),
            error={"code": "skill_disabled", "message": f"Skill '{request.skill_id}' is disabled."},
        )

    # Fail-closed permission and approval barrier:
    # Standalone worker cannot authorize privileged skills or grant user approval.
    requires_approval = (
        skill.metadata.requires_approval
        or SkillPermission.APPROVAL_REQUIRED in skill.metadata.required_permissions
    )
    non_none_permissions = {
        p for p in skill.metadata.required_permissions
        if p != SkillPermission.NONE
    }
    if requires_approval or non_none_permissions:
        perms_str = ", ".join(sorted(p.value for p in non_none_permissions)) or "approval_required"
        logger.warning(
            "Execution of privileged skill '%s' rejected by worker (requires: %s).",
            request.skill_id,
            perms_str,
        )
        fail_res = SkillResult(
            success=False,
            error="permission_denied",
            message=(
                f"Skill '{request.skill_id}' requires privileged capability permissions ({perms_str}) "
                "or explicit human approval. Standalone worker cannot authorize privileged execution."
            ),
        )
        return ExecuteResponse(
            request_id=request.request_id,
            success=False,
            result=serialize_skill_result(fail_res),
            error={
                "code": "permission_denied",
                "message": (
                    f"Skill '{request.skill_id}' requires privileged capability permissions ({perms_str}) "
                    "or explicit human approval. Standalone worker cannot authorize privileged execution."
                ),
            },
        )

    # Validate inputs against skill contract
    is_valid, validated_inputs, val_err = _validate_and_filter_inputs(skill, request.inputs)
    if not is_valid:
        return ExecuteResponse(
            request_id=request.request_id,
            success=False,
            result=serialize_skill_result(
                SkillResult(
                    success=False,
                    error="validation_error",
                    message=val_err or "Input validation failed.",
                )
            ),
            error={"code": "validation_error", "message": val_err or "Input validation failed."},
        )

    # Execute skill implementation with complete exception isolation
    try:
        raw_result = skill.execute(validated_inputs)
        if isinstance(raw_result, SkillResult):
            skill_res = raw_result
        else:
            skill_res = SkillResult(
                success=True,
                data=raw_result,
                message=str(raw_result) if raw_result is not None else "",
            )
        serialized = serialize_skill_result(skill_res)
        return ExecuteResponse(
            request_id=request.request_id,
            success=skill_res.success,
            result=serialized,
            error=None if skill_res.success else {
                "code": skill_res.error or "execution_failed",
                "message": skill_res.message,
            },
        )
    except Exception as exc:
        logger.error("Unhandled exception executing skill '%s': %s", request.skill_id, exc, exc_info=True)
        fail_res = SkillResult(
            success=False,
            error=type(exc).__name__,
            message=f"Skill execution raised uncaught exception: {exc}",
        )
        return ExecuteResponse(
            request_id=request.request_id,
            success=False,
            result=serialize_skill_result(fail_res),
            error={"code": type(exc).__name__, "message": str(exc)},
        )


def run_worker(
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    registry: SkillRegistry | None = None,
) -> int:
    """Read one request from stdin, execute, write one response to stdout, and return an exit code."""
    in_stream = stdin or sys.stdin
    out_stream = stdout or sys.stdout
    err_stream = stderr or sys.stderr

    # Initialize worker registry with built-ins if not provided
    if registry is None:
        registry = SkillRegistry()
        register_builtin_skills(registry)

    # 1. Read single request line bounded by MAX_MESSAGE_BYTES + 1
    try:
        raw_line = in_stream.readline(MAX_MESSAGE_BYTES + 1)
    except Exception as exc:
        err_stream.write(f"[skill-worker] Stdin read error: {exc}\n")
        err_stream.flush()
        return EXIT_FATAL_ERROR

    if not raw_line:
        err_stream.write("[skill-worker] Received empty input / EOF on stdin.\n")
        err_stream.flush()
        return EXIT_PROTOCOL_ERROR

    if len(raw_line.encode("utf-8")) > MAX_MESSAGE_BYTES:
        err_stream.write(f"[skill-worker] Request exceeded max allowed size of {MAX_MESSAGE_BYTES} bytes.\n")
        err_stream.flush()
        resp = create_error_response(
            request_id=None,
            error_code="oversized_message",
            error_message=f"Message size exceeds limit of {MAX_MESSAGE_BYTES} bytes.",
        )
        try:
            out_stream.write(encode_response(resp) + "\n")
            out_stream.flush()
        except Exception:
            pass
        return EXIT_PROTOCOL_ERROR

    # 2. Extract request_id defensively before strict decoding for error responses
    provisional_id: str | None = None
    try:
        partial_json = json.loads(raw_line.strip())
        if isinstance(partial_json, dict) and isinstance(partial_json.get("request_id"), str):
            candidate_id = partial_json["request_id"].strip()
            if IDENTIFIER_PATTERN.fullmatch(candidate_id) and len(candidate_id) <= MAX_ID_LENGTH:
                provisional_id = candidate_id
    except Exception:
        pass

    # 3. Decode and validate request against protocol
    try:
        request = decode_request(raw_line)
    except ProtocolSizeError as exc:
        err_stream.write(f"[skill-worker] ProtocolSizeError: {exc}\n")
        err_stream.flush()
        resp = create_error_response(provisional_id, "oversized_message", str(exc))
        out_stream.write(encode_response(resp) + "\n")
        out_stream.flush()
        return EXIT_PROTOCOL_ERROR
    except ProtocolError as exc:
        err_stream.write(f"[skill-worker] ProtocolError: {exc}\n")
        err_stream.flush()
        resp = create_error_response(provisional_id, "protocol_error", str(exc))
        out_stream.write(encode_response(resp) + "\n")
        out_stream.flush()
        return EXIT_PROTOCOL_ERROR
    except Exception as exc:
        err_stream.write(f"[skill-worker] Unexpected parsing error: {exc}\n")
        err_stream.flush()
        resp = create_error_response(provisional_id, "malformed_request", f"Failed to parse request: {exc}")
        out_stream.write(encode_response(resp) + "\n")
        out_stream.flush()
        return EXIT_PROTOCOL_ERROR

    # 4. Dispatch skill execution
    try:
        response = execute_skill_in_worker(request, registry)
    except Exception as exc:
        err_stream.write(f"[skill-worker] Uncaught execution dispatch error: {exc}\n")
        err_stream.flush()
        response = create_error_response(request.request_id, "internal_worker_error", str(exc))

    # 5. Write single framed JSON response to stdout
    try:
        encoded = encode_response(response)
        out_stream.write(encoded + "\n")
        out_stream.flush()
    except Exception as exc:
        err_stream.write(f"[skill-worker] Failed to encode/write response to stdout: {exc}\n")
        err_stream.flush()
        return EXIT_FATAL_ERROR

    return EXIT_SUCCESS


def main() -> int:
    """CLI entry point for the skill worker."""
    return run_worker()


if __name__ == "__main__":
    sys.exit(main())
