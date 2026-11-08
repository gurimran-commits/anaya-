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
from app.skills.manifest import (
    MAX_MANIFEST_BYTES,
    ManifestValidationError,
    parse_skill_manifest,
)
from app.skills.models import Skill, SkillMetadata, SkillPermission, SkillResult
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

# Protected module names that package code must never shadow
PROTECTED_MODULE_NAMES: frozenset[str] = frozenset({
    "app",
    "sys",
    "os",
    "json",
    "math",
    "re",
    "logging",
    "ctypes",
    "builtins",
    "importlib",
    "subprocess",
    "socket",
    "threading",
    "time",
    "types",
    "typing",
    "collections",
    "io",
    "pathlib",
    "dataclasses",
    "enum",
    "errno",
    "stat",
    "shutil",
    "copy",
})

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


def _execute_external_skill_in_worker(request: ExecuteRequest) -> ExecuteResponse:
    """Load and execute an external user skill from the staged sandbox package directory.

    Guarantees:
        - Staged package must reside strictly at cwd / 'package'.
        - Verifies skill.json manifest and identity against request.
        - Loads ONLY the entrypoint declared in the validated manifest.
        - Blocks shadowing of protected Anaya and Python standard library modules.
        - Disables bytecode writes (sys.dont_write_bytecode = True).
        - Validates class interface (Skill subclass) and SkillMetadata.
        - Validates inputs strictly against declared InputFieldSpecs.
        - Catches exceptions and returns controlled, bounded JSON error responses.
    """
    from pathlib import Path
    sys.dont_write_bytecode = True

    pkg_dir = (Path.cwd() / "package").resolve()
    if not pkg_dir.is_dir():
        logger.error("Staged package directory not found at '%s'.", pkg_dir)
        return create_error_response(
            request_id=request.request_id,
            error_code="package_not_found",
            error_message="Staged package directory not found in worker sandbox.",
        )

    manifest_path = pkg_dir / "skill.json"
    if not manifest_path.is_file():
        logger.error("Staged manifest file not found at '%s'.", manifest_path)
        return create_error_response(
            request_id=request.request_id,
            error_code="missing_manifest",
            error_message="Staged package missing skill.json manifest.",
        )

    try:
        raw_manifest = manifest_path.read_bytes()
        if len(raw_manifest) > MAX_MANIFEST_BYTES:
            return create_error_response(
                request_id=request.request_id,
                error_code="manifest_too_large",
                error_message=f"Manifest exceeds maximum allowed limit of {MAX_MANIFEST_BYTES} bytes.",
            )
        manifest = parse_skill_manifest(raw_manifest)
    except ManifestValidationError as exc:
        logger.error("Staged manifest validation failed: %s", exc)
        return create_error_response(
            request_id=request.request_id,
            error_code="manifest_validation_error",
            error_message=f"Staged manifest validation failed: {exc}",
        )
    except Exception as exc:
        logger.error("Failed to read staged manifest: %s", exc)
        return create_error_response(
            request_id=request.request_id,
            error_code="manifest_read_error",
            error_message=f"Failed to read staged manifest: {exc}",
        )

    if manifest.id != request.skill_id:
        logger.error("Manifest ID '%s' does not match request ID '%s'.", manifest.id, request.skill_id)
        return create_error_response(
            request_id=request.request_id,
            error_code="identity_mismatch",
            error_message=f"Manifest ID '{manifest.id}' does not match requested skill '{request.skill_id}'.",
        )

    if request.entrypoint is not None and request.entrypoint != manifest.entrypoint:
        logger.error(
            "Request entrypoint '%s' does not match manifest entrypoint '%s'.",
            request.entrypoint,
            manifest.entrypoint,
        )
        return create_error_response(
            request_id=request.request_id,
            error_code="entrypoint_mismatch",
            error_message=f"Request entrypoint '{request.entrypoint}' does not match manifest entrypoint '{manifest.entrypoint}'.",
        )

    # Capability permissions check in worker:
    # AppContainer worker runs with zero privileged capabilities.
    non_none_permissions = {
        p for p in manifest.required_permissions
        if p != SkillPermission.NONE
    }
    if non_none_permissions:
        perms_str = ", ".join(sorted(p.value for p in non_none_permissions))
        logger.warning(
            "Execution of privileged external skill '%s' rejected by worker (requires: %s).",
            request.skill_id,
            perms_str,
        )
        return create_error_response(
            request_id=request.request_id,
            error_code="permission_denied",
            error_message=(
                f"External skill '{request.skill_id}' requires privileged capability permissions ({perms_str}). "
                "Standalone worker runs with zero privileged capabilities and cannot grant access."
            ),
        )

    # Parse and validate entrypoint
    entrypoint_parts = manifest.entrypoint.split(":")
    if len(entrypoint_parts) != 2:
        return create_error_response(
            request_id=request.request_id,
            error_code="invalid_entrypoint",
            error_message=f"Malformed entrypoint '{manifest.entrypoint}'.",
        )
    module_name, class_name = entrypoint_parts

    if module_name.lower() in PROTECTED_MODULE_NAMES or module_name.lower().startswith("app"):
        logger.error("External skill entrypoint attempts to shadow protected module: '%s'", module_name)
        return create_error_response(
            request_id=request.request_id,
            error_code="shadowing_disallowed",
            error_message=f"Entrypoint module '{module_name}' shadows protected module.",
        )

    module_file = pkg_dir / f"{module_name}.py"
    pkg_init_file = pkg_dir / module_name / "__init__.py"
    if not module_file.is_file() and not pkg_init_file.is_file():
        logger.error("Entrypoint module file not found in staged package: '%s'", module_name)
        return create_error_response(
            request_id=request.request_id,
            error_code="entrypoint_not_found",
            error_message=f"Entrypoint module file '{module_name}.py' not found in staged package.",
        )

    # Configure sys.path: append package directory to END to preserve trusted module precedence
    str_pkg_dir = str(pkg_dir)
    if str_pkg_dir not in sys.path:
        sys.path.append(str_pkg_dir)

    try:
        import importlib
        sys.modules.pop(module_name, None)
        module = importlib.import_module(module_name)
    except Exception as exc:
        logger.error("Failed to import external skill module '%s': %s", module_name, exc, exc_info=True)
        return create_error_response(
            request_id=request.request_id,
            error_code="import_error",
            error_message=f"Failed to import external skill module '{module_name}': {exc}",
        )

    cls = getattr(module, class_name, None)
    if cls is None:
        return create_error_response(
            request_id=request.request_id,
            error_code="class_not_found",
            error_message=f"Entrypoint class '{class_name}' not found in module '{module_name}'.",
        )
    if not isinstance(cls, type):
        return create_error_response(
            request_id=request.request_id,
            error_code="invalid_skill_class",
            error_message=f"Entrypoint '{class_name}' is not a class.",
        )
    if not issubclass(cls, Skill):
        return create_error_response(
            request_id=request.request_id,
            error_code="invalid_skill_interface",
            error_message=f"Class '{class_name}' does not inherit from Skill.",
        )

    try:
        instance = cls()
    except Exception as exc:
        logger.error("Failed to instantiate external skill class '%s': %s", class_name, exc, exc_info=True)
        return create_error_response(
            request_id=request.request_id,
            error_code="instantiation_error",
            error_message=f"Failed to instantiate skill class '{class_name}': {exc}",
        )

    if not hasattr(instance, "metadata") or not isinstance(instance.metadata, SkillMetadata):
        return create_error_response(
            request_id=request.request_id,
            error_code="invalid_skill_metadata",
            error_message=f"Skill instance '{class_name}' does not provide a valid SkillMetadata attribute.",
        )

    if instance.metadata.id != manifest.id:
        return create_error_response(
            request_id=request.request_id,
            error_code="identity_mismatch",
            error_message=f"Skill metadata ID '{instance.metadata.id}' does not match manifest ID '{manifest.id}'.",
        )

    is_valid, validated_inputs, val_err = _validate_and_filter_inputs(instance, request.inputs)
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

    try:
        raw_result = instance.execute(validated_inputs)
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
        logger.error("Unhandled exception executing external skill '%s': %s", request.skill_id, exc, exc_info=True)
        fail_res = SkillResult(
            success=False,
            error=type(exc).__name__,
            message=f"External skill execution raised uncaught exception: {exc}",
        )
        return ExecuteResponse(
            request_id=request.request_id,
            success=False,
            result=serialize_skill_result(fail_res),
            error={"code": type(exc).__name__, "message": str(exc)},
        )


def execute_skill_in_worker(
    request: ExecuteRequest,
    registry: SkillRegistry,
) -> ExecuteResponse:
    """Dispatch and execute a request against the worker's trusted skill registry."""
    skill = registry.get(request.skill_id)

    if skill is None:
        if request.skill_id.startswith("user."):
            return _execute_external_skill_in_worker(request)

        logger.warning(
            "Skill '%s' requested but not found in worker registry.",
            request.skill_id,
        )
        return create_error_response(
            request_id=request.request_id,
            error_code="unsupported_skill",
            error_message=(
                f"Skill '{request.skill_id}' is not registered or not supported by worker in Step 10.1."
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
