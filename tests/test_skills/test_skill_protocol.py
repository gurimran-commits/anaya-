"""Comprehensive unit tests for the skill IPC protocol definitions and serialization."""

import json
import math
from typing import Any

import pytest

from app.skills.models import SkillResult
from app.skills.protocol import (
    MAX_ID_LENGTH,
    MAX_MESSAGE_BYTES,
    MAX_NESTING_DEPTH,
    MAX_TIMEOUT_SECONDS,
    MIN_TIMEOUT_SECONDS,
    PROTOCOL_VERSION,
    ExecuteRequest,
    ExecuteResponse,
    MessageType,
    ProtocolError,
    ProtocolSizeError,
    ProtocolValidationError,
    ProtocolVersionError,
    assert_json_safe,
    create_error_response,
    decode_request,
    decode_response,
    deserialize_skill_result,
    encode_request,
    encode_response,
    serialize_skill_result,
)


class TestSkillProtocolSerialization:
    """Test suite covering serialization, deserialization, and strict validation of IPC messages."""

    def test_valid_execute_request_roundtrip(self):
        """Valid ExecuteRequest serializes and deserializes accurately."""
        req = ExecuteRequest(
            request_id="req-12345",
            skill_id="example.greeting",
            inputs={"name": "Alice", "count": 2, "flags": [True, False]},
            timeout_seconds=15.0,
        )
        encoded = encode_request(req)
        assert isinstance(encoded, str)
        assert "\n" not in encoded

        decoded = decode_request(encoded)
        assert decoded.request_id == "req-12345"
        assert decoded.skill_id == "example.greeting"
        assert decoded.inputs == {"name": "Alice", "count": 2, "flags": [True, False]}
        assert decoded.timeout_seconds == 15.0
        assert decoded.protocol_version == PROTOCOL_VERSION
        assert decoded.message_type == MessageType.EXECUTE_REQUEST.value

    def test_valid_execute_response_roundtrip(self):
        """Valid ExecuteResponse with SkillResult serializes and deserializes accurately."""
        skill_res = SkillResult(
            success=True,
            data={"greeting": "Hello, Alice!"},
            message="Greeting generated.",
            confirmation_required=False,
            confirmation_id=None,
        )
        resp = ExecuteResponse(
            request_id="req-12345",
            success=True,
            result=serialize_skill_result(skill_res),
            error=None,
        )
        encoded = encode_response(resp)
        assert isinstance(encoded, str)

        decoded = decode_response(encoded)
        assert decoded.request_id == "req-12345"
        assert decoded.success is True
        assert decoded.error is None
        assert decoded.result is not None
        assert decoded.result["data"] == {"greeting": "Hello, Alice!"}
        assert decoded.result["message"] == "Greeting generated."

        deserialized_res = deserialize_skill_result(decoded.result)
        assert deserialized_res.success is True
        assert deserialized_res.data == {"greeting": "Hello, Alice!"}
        assert deserialized_res.message == "Greeting generated."

    def test_unsupported_protocol_version(self):
        """Messages with unsupported protocol versions are rejected with ProtocolVersionError."""
        with pytest.raises(ProtocolVersionError, match="Unsupported protocol version '2.0'"):
            ExecuteRequest(
                request_id="req-1",
                skill_id="test.skill",
                inputs={},
                protocol_version="2.0",
            )

        raw_req = {
            "protocol_version": "99.0",
            "message_type": "execute_request",
            "request_id": "req-1",
            "skill_id": "test.skill",
            "inputs": {},
            "timeout_seconds": 5.0,
        }
        with pytest.raises(ProtocolVersionError, match="Unsupported protocol version '99.0'"):
            decode_request(json.dumps(raw_req))

        raw_resp = {
            "protocol_version": "0.1",
            "message_type": "execute_response",
            "request_id": "req-1",
            "success": True,
            "result": None,
            "error": None,
        }
        with pytest.raises(ProtocolVersionError, match="Unsupported protocol version '0.1'"):
            decode_response(json.dumps(raw_resp))

    def test_missing_required_fields(self):
        """Omission of any mandatory fields triggers ProtocolValidationError."""
        base_request = {
            "protocol_version": PROTOCOL_VERSION,
            "message_type": MessageType.EXECUTE_REQUEST.value,
            "request_id": "req-1",
            "skill_id": "test.skill",
            "inputs": {},
            "timeout_seconds": 5.0,
        }
        for field in ("request_id", "skill_id", "inputs", "timeout_seconds", "protocol_version", "message_type"):
            incomplete = dict(base_request)
            del incomplete[field]
            with pytest.raises(ProtocolValidationError, match=f"Missing required field.*{field}"):
                decode_request(json.dumps(incomplete))

        base_response = {
            "protocol_version": PROTOCOL_VERSION,
            "message_type": MessageType.EXECUTE_RESPONSE.value,
            "request_id": "req-1",
            "success": True,
            "result": None,
            "error": None,
        }
        for field in ("request_id", "success", "result", "error", "protocol_version", "message_type"):
            incomplete = dict(base_response)
            del incomplete[field]
            with pytest.raises(ProtocolValidationError, match=f"Missing required field.*{field}"):
                decode_response(json.dumps(incomplete))

    def test_unexpected_fields_rejection(self):
        """Unexpected keys in request or response payloads are rejected."""
        raw_req = {
            "protocol_version": PROTOCOL_VERSION,
            "message_type": MessageType.EXECUTE_REQUEST.value,
            "request_id": "req-1",
            "skill_id": "test.skill",
            "inputs": {},
            "timeout_seconds": 5.0,
            "malicious_module_path": "/tmp/evil.py",
        }
        with pytest.raises(ProtocolValidationError, match="Unexpected field.*malicious_module_path"):
            decode_request(json.dumps(raw_req))

        raw_resp = {
            "protocol_version": PROTOCOL_VERSION,
            "message_type": MessageType.EXECUTE_RESPONSE.value,
            "request_id": "req-1",
            "success": True,
            "result": None,
            "error": None,
            "extra_key": "unexpected",
        }
        with pytest.raises(ProtocolValidationError, match="Unexpected field.*extra_key"):
            decode_response(json.dumps(raw_resp))

    def test_invalid_message_type(self):
        """Mismatched or unrecognized message_type values are rejected."""
        raw_req = {
            "protocol_version": PROTOCOL_VERSION,
            "message_type": "invalid_request_type",
            "request_id": "req-1",
            "skill_id": "test.skill",
            "inputs": {},
            "timeout_seconds": 5.0,
        }
        with pytest.raises(ProtocolValidationError, match="Invalid message_type"):
            decode_request(json.dumps(raw_req))

        with pytest.raises(ProtocolValidationError, match="Invalid message_type"):
            ExecuteRequest(
                request_id="req-1",
                skill_id="test.skill",
                inputs={},
                message_type="execute_response",
            )

    def test_malformed_json_handling(self):
        """Malformed or non-JSON input raises ProtocolValidationError without crash."""
        with pytest.raises(ProtocolValidationError, match="Empty message payload"):
            decode_request("")

        with pytest.raises(ProtocolValidationError, match="Empty message payload"):
            decode_request("   \n\t  ")

        with pytest.raises(ProtocolValidationError, match="Malformed JSON payload"):
            decode_request("{not-valid-json: 123")

        with pytest.raises(ProtocolValidationError, match="Top-level message payload must be a JSON object"):
            decode_request('["not", "a", "dict"]')

        with pytest.raises(ProtocolValidationError, match="Top-level message payload must be a JSON object"):
            decode_request('"just-a-string"')

    def test_oversized_message_rejection(self):
        """Messages exceeding MAX_MESSAGE_BYTES are rejected before memory parsing."""
        oversized_str = "x" * (MAX_MESSAGE_BYTES + 10)
        with pytest.raises(ProtocolSizeError, match="exceeds maximum allowed size"):
            decode_request(oversized_str)

        with pytest.raises(ProtocolSizeError, match="exceeds maximum allowed size"):
            decode_request(oversized_str.encode("utf-8"))

        huge_inputs = {"data": "a" * (MAX_MESSAGE_BYTES + 10)}
        huge_req = ExecuteRequest(
            request_id="req-huge",
            skill_id="test.skill",
            inputs=huge_inputs,
        )
        with pytest.raises(ProtocolSizeError, match="exceeds maximum allowed size"):
            encode_request(huge_req)

    def test_invalid_request_id_and_skill_id_constraints(self):
        """Request IDs and skill IDs must conform strictly to identifier length and character allowlist."""
        for invalid_id in ("", "   ", None, 123, False, "a" * (MAX_ID_LENGTH + 1)):
            with pytest.raises(ProtocolValidationError):
                ExecuteRequest(
                    request_id=invalid_id,  # type: ignore[arg-type]
                    skill_id="test.skill",
                    inputs={},
                )

        # Disallowed characters: newlines, carriage returns, null bytes, control chars, spaces, non-ASCII
        unsafe_ids = [
            "req\nid",
            "req\rid",
            "req\x00id",
            "req\x1b[31mid",
            "req id with spaces",
            "skill/with/slashes",
            "../traversal",
            "skill@name",
            "skill#hash",
            "skill👍emoji",
            "идентификатор",
        ]
        for bad_id in unsafe_ids:
            with pytest.raises(ProtocolValidationError, match="contains invalid characters"):
                ExecuteRequest(
                    request_id=bad_id,
                    skill_id="test.skill",
                    inputs={},
                )
            with pytest.raises(ProtocolValidationError, match="contains invalid characters"):
                ExecuteRequest(
                    request_id="valid-id",
                    skill_id=bad_id,
                    inputs={},
                )

        # Valid identifiers: alphanumerics, dots, colons, underscores, hyphens
        valid_ids = [
            "example.greeting",
            "test_calculator-v2",
            "req:12345",
            "A.B.C-99_x",
            "UUID-123e4567-e89b-12d3-a456-426614174000",
        ]
        for ok_id in valid_ids:
            req = ExecuteRequest(request_id=ok_id, skill_id=ok_id, inputs={})
            assert req.request_id == ok_id
            assert req.skill_id == ok_id

    def test_invalid_timeout_values(self):
        """Timeouts must be finite numbers bounded between MIN_TIMEOUT_SECONDS and MAX_TIMEOUT_SECONDS."""
        for bad_timeout in (-1.0, 0.0, 0.05, 301.0, math.nan, math.inf, -math.inf, "5.0", True, False):
            with pytest.raises(ProtocolValidationError):
                ExecuteRequest(
                    request_id="req-1",
                    skill_id="test.skill",
                    inputs={},
                    timeout_seconds=bad_timeout,  # type: ignore[arg-type]
                )

    def test_unsupported_object_types_in_inputs_and_results(self):
        """Arbitrary Python classes, functions, or non-serializable objects fail JSON-safety checks."""
        class ArbitraryObject:
            pass

        with pytest.raises(ProtocolValidationError, match="Unsupported object type 'ArbitraryObject'"):
            assert_json_safe({"evil": ArbitraryObject()})

        with pytest.raises(ProtocolValidationError, match="Unsupported object type 'function'"):
            assert_json_safe({"fn": lambda x: x})

        with pytest.raises(ProtocolValidationError, match="Dictionary key.*must be a string"):
            assert_json_safe({123: "non-string key"})  # type: ignore[dict-item]

        with pytest.raises(ProtocolValidationError, match="Non-finite float value"):
            assert_json_safe({"nan": float("nan")})

        bad_result = SkillResult(
            success=True,
            data={"instance": ArbitraryObject()},
        )
        with pytest.raises(ProtocolValidationError, match="Unsupported object type"):
            serialize_skill_result(bad_result)

    def test_skill_result_serialization_and_deserialization(self):
        """SkillResult converts faithfully to and from serialized form."""
        res = SkillResult(
            success=False,
            data={"step": 3},
            message="Execution paused",
            error="need_approval",
            confirmation_required=True,
            confirmation_id="conf-999",
        )
        serialized = serialize_skill_result(res)
        assert serialized["success"] is False
        assert serialized["data"] == {"step": 3}
        assert serialized["message"] == "Execution paused"
        assert serialized["error"] == "need_approval"
        assert serialized["confirmation_required"] is True
        assert serialized["confirmation_id"] == "conf-999"

        rehydrated = deserialize_skill_result(serialized)
        assert rehydrated == res

    def test_create_error_response_helper(self):
        """create_error_response constructs a standard protocol-compliant error response."""
        err_resp = create_error_response("req-xyz", "worker_timeout", "Skill timed out after 10.0s")
        assert err_resp.request_id == "req-xyz"
        assert err_resp.success is False
        assert err_resp.result is None
        assert err_resp.error == {"code": "worker_timeout", "message": "Skill timed out after 10.0s"}

        # Empty, None, or unsafe request_id falls back safely to 'unknown' without injecting bad characters
        err_resp_unknown = create_error_response(None, "crash", "Fatal crash")
        assert err_resp_unknown.request_id == "unknown"

        err_resp_unsafe = create_error_response("unsafe\nrequest\x00id", "crash", "Fatal crash")
        assert err_resp_unsafe.request_id == "unknown"

    def test_bounded_nesting_depth_exact_and_exceeded(self):
        """Nesting depth is bounded at MAX_NESTING_DEPTH (32); exceeding it raises ProtocolValidationError."""
        # 1. Exactly 32 levels of list nesting passes
        curr_list: Any = "leaf"
        for _ in range(MAX_NESTING_DEPTH):
            curr_list = [curr_list]
        assert_json_safe(curr_list)

        # 33 levels of list nesting fails
        over_list: Any = "leaf"
        for _ in range(MAX_NESTING_DEPTH + 1):
            over_list = [over_list]
        with pytest.raises(ProtocolValidationError, match=f"Maximum JSON nesting depth of {MAX_NESTING_DEPTH} exceeded"):
            assert_json_safe(over_list)

        # 2. Deeply nested dictionaries
        curr_dict: Any = "value"
        for i in range(MAX_NESTING_DEPTH):
            curr_dict = {f"k_{i}": curr_dict}
        assert_json_safe(curr_dict)

        over_dict: Any = "value"
        for i in range(MAX_NESTING_DEPTH + 1):
            over_dict = {f"k_{i}": over_dict}
        with pytest.raises(ProtocolValidationError, match=f"Maximum JSON nesting depth of {MAX_NESTING_DEPTH} exceeded"):
            assert_json_safe(over_dict)

        # 3. Deeply nested mixed dict/list structure exceeding limit
        over_mixed: Any = 42
        for i in range(MAX_NESTING_DEPTH + 5):
            over_mixed = {"items": [over_mixed]}
        with pytest.raises(ProtocolValidationError, match=f"Maximum JSON nesting depth of {MAX_NESTING_DEPTH} exceeded"):
            assert_json_safe(over_mixed)

        # 4. Request decoding rejects excessive input nesting
        raw_over = {
            "protocol_version": PROTOCOL_VERSION,
            "message_type": MessageType.EXECUTE_REQUEST.value,
            "request_id": "req-deep",
            "skill_id": "test.skill",
            "inputs": {"nested": over_dict},
            "timeout_seconds": 10.0,
        }
        with pytest.raises(ProtocolValidationError, match=f"Maximum JSON nesting depth of {MAX_NESTING_DEPTH} exceeded"):
            decode_request(json.dumps(raw_over))

    def test_duplicate_json_keys_rejected(self):
        """Duplicate keys at top-level, nested dicts, and arrays are strictly rejected."""
        # Duplicate top-level key
        dup_top = (
            '{"protocol_version":"1.0","message_type":"execute_request",'
            '"request_id":"req-1","request_id":"req-2","skill_id":"test.skill",'
            '"inputs":{},"timeout_seconds":10.0}'
        )
        with pytest.raises(ProtocolValidationError, match="Duplicate key 'request_id' detected"):
            decode_request(dup_top)

        # Duplicate nested key in inputs
        dup_nested = (
            '{"protocol_version":"1.0","message_type":"execute_request",'
            '"request_id":"req-1","skill_id":"test.skill",'
            '"inputs":{"param":1,"param":2},"timeout_seconds":10.0}'
        )
        with pytest.raises(ProtocolValidationError, match="Duplicate key 'param' detected"):
            decode_request(dup_nested)

        # Duplicate key in array of objects
        dup_in_array = (
            '{"protocol_version":"1.0","message_type":"execute_request",'
            '"request_id":"req-1","skill_id":"test.skill",'
            '"inputs":{"items":[{"id":1,"id":2}]},"timeout_seconds":10.0}'
        )
        with pytest.raises(ProtocolValidationError, match="Duplicate key 'id' detected"):
            decode_request(dup_in_array)

        # Duplicate key in execute_response
        dup_resp = (
            '{"protocol_version":"1.0","message_type":"execute_response",'
            '"request_id":"req-1","success":true,"success":false,"result":null,"error":null}'
        )
        with pytest.raises(ProtocolValidationError, match="Duplicate key 'success' detected"):
            decode_response(dup_resp)

        # Normal non-duplicate JSON parses cleanly
        clean_json = (
            '{"protocol_version":"1.0","message_type":"execute_request",'
            '"request_id":"req-1","skill_id":"test.skill",'
            '"inputs":{"a":1,"b":2},"timeout_seconds":10.0}'
        )
        assert decode_request(clean_json).request_id == "req-1"

    def test_non_standard_json_constants_rejected(self):
        """Non-standard JSON constants (NaN, Infinity, -Infinity) are rejected during parsing."""
        # Top-level timeout_seconds = NaN
        nan_timeout = (
            '{"protocol_version":"1.0","message_type":"execute_request",'
            '"request_id":"req-1","skill_id":"test.skill",'
            '"inputs":{},"timeout_seconds":NaN}'
        )
        with pytest.raises(ProtocolValidationError, match="Non-standard JSON constant 'NaN'"):
            decode_request(nan_timeout)

        # Infinity in timeout_seconds
        inf_timeout = (
            '{"protocol_version":"1.0","message_type":"execute_request",'
            '"request_id":"req-1","skill_id":"test.skill",'
            '"inputs":{},"timeout_seconds":Infinity}'
        )
        with pytest.raises(ProtocolValidationError, match="Non-standard JSON constant 'Infinity'"):
            decode_request(inf_timeout)

        # -Infinity in timeout_seconds
        ninf_timeout = (
            '{"protocol_version":"1.0","message_type":"execute_request",'
            '"request_id":"req-1","skill_id":"test.skill",'
            '"inputs":{},"timeout_seconds":-Infinity}'
        )
        with pytest.raises(ProtocolValidationError, match="Non-standard JSON constant '-Infinity'"):
            decode_request(ninf_timeout)

        # Nested NaN inside inputs
        nested_nan = (
            '{"protocol_version":"1.0","message_type":"execute_request",'
            '"request_id":"req-1","skill_id":"test.skill",'
            '"inputs":{"measurement":NaN},"timeout_seconds":10.0}'
        )
        with pytest.raises(ProtocolValidationError, match="Non-standard JSON constant 'NaN'"):
            decode_request(nested_nan)

        # Nested Infinity inside list in inputs
        nested_inf = (
            '{"protocol_version":"1.0","message_type":"execute_request",'
            '"request_id":"req-1","skill_id":"test.skill",'
            '"inputs":{"values":[1.0, 2.0, Infinity]},"timeout_seconds":10.0}'
        )
        with pytest.raises(ProtocolValidationError, match="Non-standard JSON constant 'Infinity'"):
            decode_request(nested_inf)

        # Valid finite floats are preserved accurately
        finite_req = (
            '{"protocol_version":"1.0","message_type":"execute_request",'
            '"request_id":"req-1","skill_id":"test.skill",'
            '"inputs":{"temp":-12.34,"rate":0.001,"max":1e4},"timeout_seconds":10.5}'
        )
        decoded = decode_request(finite_req)
        assert decoded.inputs["temp"] == -12.34
        assert decoded.inputs["rate"] == 0.001
        assert decoded.inputs["max"] == 10000.0
        assert decoded.timeout_seconds == 10.5
