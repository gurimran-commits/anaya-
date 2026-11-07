"""IPC protocol definitions, serialization, and validation for skill workers.

This module provides strict, versioned, dependency-free JSON framing and validation
for inter-process communication between Anaya and isolated skill worker subprocesses.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import json
import math
import re
from typing import Any

from app.skills.models import SkillResult

# Protocol Constants
PROTOCOL_VERSION: str = "1.0"
MAX_MESSAGE_BYTES: int = 1_048_576  # 1 MB maximum message payload
MAX_ID_LENGTH: int = 128
MAX_NESTING_DEPTH: int = 32
MIN_TIMEOUT_SECONDS: float = 0.1
MAX_TIMEOUT_SECONDS: float = 300.0
DEFAULT_TIMEOUT_SECONDS: float = 10.0

# Conservative allowlist for safe identifier names (alphanumerics, dots, colons, underscores, hyphens)
IDENTIFIER_PATTERN: re.Pattern[str] = re.compile(r"^[a-zA-Z0-9_.:-]+$")


class MessageType(str, Enum):
    """Supported IPC protocol message types."""

    EXECUTE_REQUEST = "execute_request"
    EXECUTE_RESPONSE = "execute_response"


class ProtocolError(Exception):
    """Base exception for all IPC protocol validation and framing errors."""


class ProtocolVersionError(ProtocolError):
    """Raised when a message contains an unsupported protocol version."""


class ProtocolValidationError(ProtocolError):
    """Raised when a message violates structural, typing, or value constraints."""


class ProtocolSizeError(ProtocolError):
    """Raised when a raw message exceeds the maximum allowed payload length."""


def validate_identifier(val: str, field_name: str) -> None:
    """Validate that an identifier is non-empty, bounded, and contains only safe printable characters."""
    if not isinstance(val, str) or not val.strip():
        raise ProtocolValidationError(f"{field_name} must be a non-empty string.")
    if len(val) > MAX_ID_LENGTH:
        raise ProtocolValidationError(f"{field_name} exceeds maximum length of {MAX_ID_LENGTH} characters.")
    if not IDENTIFIER_PATTERN.fullmatch(val):
        sanitized_sample = repr(val[:32])
        raise ProtocolValidationError(
            f"{field_name} contains invalid characters ({sanitized_sample}). "
            "Only alphanumeric characters, dots, colons, underscores, and hyphens are permitted."
        )


def assert_json_safe(obj: Any, path: str = "payload", depth: int = 0) -> None:
    """Recursively verify that an object contains strictly JSON-safe primitive types and bounded depth.

    Rejects functions, classes, file handles, sockets, custom class instances,
    infinite or NaN floating-point numbers, non-string dictionary keys, and
    nesting structures deeper than MAX_NESTING_DEPTH.
    """
    if depth > MAX_NESTING_DEPTH:
        raise ProtocolValidationError(
            f"Maximum JSON nesting depth of {MAX_NESTING_DEPTH} exceeded at '{path}'."
        )

    if obj is None or isinstance(obj, (str, bool)):
        return

    if isinstance(obj, int):
        return

    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise ProtocolValidationError(
                f"Non-finite float value '{obj}' at '{path}' is not permitted in protocol payloads."
            )
        return

    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise ProtocolValidationError(
                    f"Dictionary key at '{path}' must be a string, got {type(k).__name__}."
                )
            assert_json_safe(v, f"{path}.{k}", depth + 1)
        return

    if isinstance(obj, (list, tuple)):
        for idx, item in enumerate(obj):
            assert_json_safe(item, f"{path}[{idx}]", depth + 1)
        return

    raise ProtocolValidationError(
        f"Unsupported object type '{type(obj).__name__}' at '{path}'. "
        "Only JSON-safe primitives (dict, list, str, int, float, bool, None) are permitted."
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Object pairs hook for json.loads that strictly rejects duplicate keys at any nesting level."""
    d: dict[str, Any] = {}
    for k, v in pairs:
        if k in d:
            raise ProtocolValidationError(f"Duplicate key '{k}' detected in JSON object.")
        d[k] = v
    return d


def _reject_constant(c: str) -> None:
    """Constant parser hook for json.loads that strictly rejects non-standard JSON constants."""
    raise ProtocolValidationError(f"Non-standard JSON constant '{c}' is not permitted.")


def parse_strict_json(clean_str: str) -> Any:
    """Parse JSON string with duplicate key rejection and NaN/Infinity constant rejection."""
    try:
        return json.loads(
            clean_str,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise ProtocolValidationError(f"Malformed JSON payload: {exc.msg}") from exc
    except RecursionError as exc:
        raise ProtocolValidationError("Maximum JSON recursion depth exceeded.") from exc


@dataclass(frozen=True)
class ExecuteRequest:
    """Structured request payload sent to a skill worker subprocess."""

    request_id: str
    skill_id: str
    inputs: dict[str, Any]
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    protocol_version: str = PROTOCOL_VERSION
    message_type: str = MessageType.EXECUTE_REQUEST.value

    def __post_init__(self) -> None:
        if not isinstance(self.protocol_version, str) or self.protocol_version != PROTOCOL_VERSION:
            raise ProtocolVersionError(
                f"Unsupported protocol version '{self.protocol_version}'. Expected '{PROTOCOL_VERSION}'."
            )
        if not isinstance(self.message_type, str) or self.message_type != MessageType.EXECUTE_REQUEST.value:
            raise ProtocolValidationError(
                f"Invalid message_type '{self.message_type}'. Expected '{MessageType.EXECUTE_REQUEST.value}'."
            )

        validate_identifier(self.request_id, "request_id")
        validate_identifier(self.skill_id, "skill_id")

        if not isinstance(self.inputs, dict):
            raise ProtocolValidationError("inputs must be a dictionary.")
        assert_json_safe(self.inputs, "inputs", depth=0)

        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float)):
            raise ProtocolValidationError("timeout_seconds must be a positive number.")
        if not math.isfinite(self.timeout_seconds):
            raise ProtocolValidationError("timeout_seconds must be a finite number.")
        if not (MIN_TIMEOUT_SECONDS <= self.timeout_seconds <= MAX_TIMEOUT_SECONDS):
            raise ProtocolValidationError(
                f"timeout_seconds must be between {MIN_TIMEOUT_SECONDS} and {MAX_TIMEOUT_SECONDS}s, "
                f"got {self.timeout_seconds}."
            )


@dataclass(frozen=True)
class ExecuteResponse:
    """Structured response payload returned by a skill worker subprocess."""

    request_id: str
    success: bool
    result: dict[str, Any] | None = None
    error: dict[str, str] | None = None
    protocol_version: str = PROTOCOL_VERSION
    message_type: str = MessageType.EXECUTE_RESPONSE.value

    def __post_init__(self) -> None:
        if not isinstance(self.protocol_version, str) or self.protocol_version != PROTOCOL_VERSION:
            raise ProtocolVersionError(
                f"Unsupported protocol version '{self.protocol_version}'. Expected '{PROTOCOL_VERSION}'."
            )
        if not isinstance(self.message_type, str) or self.message_type != MessageType.EXECUTE_RESPONSE.value:
            raise ProtocolValidationError(
                f"Invalid message_type '{self.message_type}'. Expected '{MessageType.EXECUTE_RESPONSE.value}'."
            )

        validate_identifier(self.request_id, "request_id")

        if not isinstance(self.success, bool):
            raise ProtocolValidationError("success must be a boolean.")

        if self.result is not None:
            if not isinstance(self.result, dict):
                raise ProtocolValidationError("result must be a dictionary or None.")
            validate_serialized_skill_result(self.result)

        if self.error is not None:
            if not isinstance(self.error, dict):
                raise ProtocolValidationError("error must be a dictionary or None.")
            validate_serialized_error(self.error)


def serialize_skill_result(result: SkillResult) -> dict[str, Any]:
    """Serialize a domain SkillResult into a validated JSON-safe dictionary."""
    if not isinstance(result, SkillResult):
        raise ProtocolValidationError(f"Expected SkillResult instance, got {type(result).__name__}.")

    assert_json_safe(result.data, "result.data", depth=0)

    return {
        "success": bool(result.success),
        "data": result.data,
        "message": str(result.message or ""),
        "error": str(result.error) if result.error is not None else None,
        "confirmation_required": bool(result.confirmation_required),
        "confirmation_id": str(result.confirmation_id) if result.confirmation_id is not None else None,
    }


def deserialize_skill_result(data: dict[str, Any]) -> SkillResult:
    """Deserialize a validated dictionary back into a domain SkillResult."""
    validate_serialized_skill_result(data)
    return SkillResult(
        success=data["success"],
        data=data["data"],
        message=data["message"],
        error=data["error"],
        confirmation_required=data["confirmation_required"],
        confirmation_id=data["confirmation_id"],
    )


def validate_serialized_skill_result(data: dict[str, Any]) -> None:
    """Validate that a dictionary conforms strictly to the serialized SkillResult schema."""
    if not isinstance(data, dict):
        raise ProtocolValidationError("Serialized SkillResult must be a dictionary.")

    allowed_keys = {
        "success",
        "data",
        "message",
        "error",
        "confirmation_required",
        "confirmation_id",
    }
    missing_keys = allowed_keys - set(data.keys())
    if missing_keys:
        raise ProtocolValidationError(f"Missing required SkillResult field(s): {sorted(missing_keys)}.")

    extra_keys = set(data.keys()) - allowed_keys
    if extra_keys:
        raise ProtocolValidationError(f"Unexpected field(s) in SkillResult: {sorted(extra_keys)}.")

    if not isinstance(data["success"], bool):
        raise ProtocolValidationError("SkillResult 'success' must be a boolean.")
    assert_json_safe(data["data"], "result.data", depth=0)
    if not isinstance(data["message"], str):
        raise ProtocolValidationError("SkillResult 'message' must be a string.")
    if data["error"] is not None and not isinstance(data["error"], str):
        raise ProtocolValidationError("SkillResult 'error' must be a string or None.")
    if not isinstance(data["confirmation_required"], bool):
        raise ProtocolValidationError("SkillResult 'confirmation_required' must be a boolean.")
    if data["confirmation_id"] is not None and not isinstance(data["confirmation_id"], str):
        raise ProtocolValidationError("SkillResult 'confirmation_id' must be a string or None.")


def validate_serialized_error(error: dict[str, Any]) -> None:
    """Validate that an error object conforms strictly to the protocol error schema."""
    if not isinstance(error, dict):
        raise ProtocolValidationError("Error object must be a dictionary.")

    allowed_keys = {"code", "message"}
    missing_keys = allowed_keys - set(error.keys())
    if missing_keys:
        raise ProtocolValidationError(f"Missing required error field(s): {sorted(missing_keys)}.")

    extra_keys = set(error.keys()) - allowed_keys
    if extra_keys:
        raise ProtocolValidationError(f"Unexpected field(s) in error object: {sorted(extra_keys)}.")

    if not isinstance(error["code"], str) or not error["code"].strip():
        raise ProtocolValidationError("error.code must be a non-empty string.")
    if not isinstance(error["message"], str):
        raise ProtocolValidationError("error.message must be a string.")


def encode_request(request: ExecuteRequest) -> str:
    """Encode an ExecuteRequest dataclass into a single-line JSON string."""
    if not isinstance(request, ExecuteRequest):
        raise ProtocolValidationError(f"Expected ExecuteRequest, got {type(request).__name__}.")

    payload = {
        "protocol_version": request.protocol_version,
        "message_type": request.message_type,
        "request_id": request.request_id,
        "skill_id": request.skill_id,
        "inputs": request.inputs,
        "timeout_seconds": float(request.timeout_seconds),
    }
    encoded = json.dumps(payload, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ProtocolSizeError(f"Encoded request exceeds maximum allowed size of {MAX_MESSAGE_BYTES} bytes.")
    return encoded


def decode_request(raw_message: str | bytes) -> ExecuteRequest:
    """Decode and validate a JSON string or bytes into an ExecuteRequest dataclass."""
    if isinstance(raw_message, bytes):
        if len(raw_message) > MAX_MESSAGE_BYTES:
            raise ProtocolSizeError(f"Raw message exceeds maximum allowed size of {MAX_MESSAGE_BYTES} bytes.")
        try:
            raw_str = raw_message.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolValidationError(f"Invalid UTF-8 byte sequence in request: {exc}") from exc
    elif isinstance(raw_message, str):
        if len(raw_message.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise ProtocolSizeError(f"Raw message exceeds maximum allowed size of {MAX_MESSAGE_BYTES} bytes.")
        raw_str = raw_message
    else:
        raise ProtocolValidationError(f"Expected str or bytes, got {type(raw_message).__name__}.")

    clean_str = raw_str.strip()
    if not clean_str:
        raise ProtocolValidationError("Empty message payload.")

    data = parse_strict_json(clean_str)

    if not isinstance(data, dict):
        raise ProtocolValidationError("Top-level message payload must be a JSON object.")

    expected_keys = {
        "protocol_version",
        "message_type",
        "request_id",
        "skill_id",
        "inputs",
        "timeout_seconds",
    }
    missing_keys = expected_keys - set(data.keys())
    if missing_keys:
        raise ProtocolValidationError(f"Missing required field(s) in request: {sorted(missing_keys)}.")

    extra_keys = set(data.keys()) - expected_keys
    if extra_keys:
        raise ProtocolValidationError(f"Unexpected field(s) in request: {sorted(extra_keys)}.")

    return ExecuteRequest(
        request_id=data["request_id"],
        skill_id=data["skill_id"],
        inputs=data["inputs"],
        timeout_seconds=data["timeout_seconds"],
        protocol_version=data["protocol_version"],
        message_type=data["message_type"],
    )


def encode_response(response: ExecuteResponse) -> str:
    """Encode an ExecuteResponse dataclass into a single-line JSON string."""
    if not isinstance(response, ExecuteResponse):
        raise ProtocolValidationError(f"Expected ExecuteResponse, got {type(response).__name__}.")

    payload = {
        "protocol_version": response.protocol_version,
        "message_type": response.message_type,
        "request_id": response.request_id,
        "success": response.success,
        "result": response.result,
        "error": response.error,
    }
    encoded = json.dumps(payload, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ProtocolSizeError(f"Encoded response exceeds maximum allowed size of {MAX_MESSAGE_BYTES} bytes.")
    return encoded


def decode_response(raw_message: str | bytes) -> ExecuteResponse:
    """Decode and validate a JSON string or bytes into an ExecuteResponse dataclass."""
    if isinstance(raw_message, bytes):
        if len(raw_message) > MAX_MESSAGE_BYTES:
            raise ProtocolSizeError(f"Raw message exceeds maximum allowed size of {MAX_MESSAGE_BYTES} bytes.")
        try:
            raw_str = raw_message.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolValidationError(f"Invalid UTF-8 byte sequence in response: {exc}") from exc
    elif isinstance(raw_message, str):
        if len(raw_message.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise ProtocolSizeError(f"Raw message exceeds maximum allowed size of {MAX_MESSAGE_BYTES} bytes.")
        raw_str = raw_message
    else:
        raise ProtocolValidationError(f"Expected str or bytes, got {type(raw_message).__name__}.")

    clean_str = raw_str.strip()
    if not clean_str:
        raise ProtocolValidationError("Empty message payload.")

    data = parse_strict_json(clean_str)

    if not isinstance(data, dict):
        raise ProtocolValidationError("Top-level message payload must be a JSON object.")

    expected_keys = {
        "protocol_version",
        "message_type",
        "request_id",
        "success",
        "result",
        "error",
    }
    missing_keys = expected_keys - set(data.keys())
    if missing_keys:
        raise ProtocolValidationError(f"Missing required field(s) in response: {sorted(missing_keys)}.")

    extra_keys = set(data.keys()) - expected_keys
    if extra_keys:
        raise ProtocolValidationError(f"Unexpected field(s) in response: {sorted(extra_keys)}.")

    return ExecuteResponse(
        request_id=data["request_id"],
        success=data["success"],
        result=data["result"],
        error=data["error"],
        protocol_version=data["protocol_version"],
        message_type=data["message_type"],
    )


def create_error_response(
    request_id: str | None,
    error_code: str,
    error_message: str,
) -> ExecuteResponse:
    """Construct an ExecuteResponse representing a protocol or execution error."""
    safe_request_id = "unknown"
    if request_id and isinstance(request_id, str):
        candidate_id = request_id.strip()
        if IDENTIFIER_PATTERN.fullmatch(candidate_id) and len(candidate_id) <= MAX_ID_LENGTH:
            safe_request_id = candidate_id

    return ExecuteResponse(
        request_id=safe_request_id,
        success=False,
        result=None,
        error={
            "code": str(error_code or "error"),
            "message": str(error_message or ""),
        },
    )
