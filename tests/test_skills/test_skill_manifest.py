"""Comprehensive unit tests for external skill manifest specification & schema parser (Phase 10.4.1).

Covers:
1. Valid manifest parsing (minimal and full specification)
2. Missing required fields
3. Unknown fields (top-level and nested in inputs)
4. Invalid JSON syntax
5. Invalid UTF-8 byte sequences
6. Duplicate JSON keys (top-level and nested)
7. Non-standard JSON constants (NaN, Infinity, -Infinity)
8. Oversized manifests (> 64 KiB)
9. Invalid schema versions (including boolean values)
10. Invalid, malformed, traversal, or reserved skill IDs
11. Invalid entry points, non-identifiers, Python keywords, and path traversal attempts
12. Invalid input field definitions, unsupported types, and invalid defaults
13. Unknown and duplicate permission values
14. Excessive collection sizes and string lengths
15. Path loader behavior, stat checks, and read error handling
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from app.skills.manifest import (
    MAX_DESCRIPTION_LENGTH,
    MAX_ENTRYPOINT_LENGTH,
    MAX_FIELD_DESCRIPTION_LENGTH,
    MAX_FIELD_NAME_LENGTH,
    MAX_ID_LENGTH,
    MAX_INPUT_FIELDS,
    MAX_MANIFEST_BYTES,
    MAX_NAME_LENGTH,
    MAX_PERMISSIONS,
    MAX_VERSION_LENGTH,
    ManifestValidationError,
    SkillManifest,
    load_skill_manifest,
    parse_skill_manifest,
)
from app.skills.models import SkillPermission


VALID_MINIMAL_MANIFEST = {
    "schema_version": 1,
    "id": "user.simple_skill",
    "name": "Simple Skill",
    "description": "A simple external user skill.",
    "version": "1.0.0",
    "entrypoint": "simple:SimpleSkill",
}

VALID_FULL_MANIFEST = {
    "schema_version": 1,
    "id": "user.text_tools.analyzer",
    "name": "Text Analyzer",
    "description": "Analyzes text sentiment and word counts.",
    "version": "2.1.0-beta.1",
    "entrypoint": "analyzer:TextAnalyzerSkill",
    "inputs": [
        {
            "name": "text",
            "type": "str",
            "required": True,
            "description": "Text to analyze",
        },
        {
            "name": "max_words",
            "type": "int",
            "required": False,
            "default": 100,
            "description": "Maximum words to process",
        },
        {
            "name": "options",
            "type": "dict",
            "required": False,
            "default": {"case_sensitive": False},
            "description": "Analysis options dictionary",
        },
    ],
    "required_permissions": ["read_only"],
    "requires_approval": True,
}


class TestValidManifestParsing:
    """Verify that valid manifests parse correctly into immutable SkillManifest dataclasses."""

    def test_parse_valid_minimal_manifest(self):
        raw = json.dumps(VALID_MINIMAL_MANIFEST)
        manifest = parse_skill_manifest(raw)

        assert isinstance(manifest, SkillManifest)
        assert manifest.schema_version == 1
        assert manifest.id == "user.simple_skill"
        assert manifest.name == "Simple Skill"
        assert manifest.description == "A simple external user skill."
        assert manifest.version == "1.0.0"
        assert manifest.entrypoint == "simple:SimpleSkill"
        assert manifest.entrypoint_module == "simple"
        assert manifest.entrypoint_class == "SimpleSkill"
        assert manifest.inputs == ()
        assert manifest.required_permissions == (SkillPermission.NONE,)
        assert manifest.requires_approval is False

        # Conversion to SkillMetadata
        metadata = manifest.to_metadata()
        assert metadata.id == "user.simple_skill"
        assert metadata.name == "Simple Skill"
        assert metadata.enabled is True
        assert metadata.requires_approval is False

    def test_parse_valid_full_manifest_from_bytes(self):
        raw_bytes = json.dumps(VALID_FULL_MANIFEST).encode("utf-8")
        manifest = parse_skill_manifest(raw_bytes)

        assert manifest.id == "user.text_tools.analyzer"
        assert manifest.version == "2.1.0-beta.1"
        assert len(manifest.inputs) == 3

        # Check input specs
        text_spec = manifest.inputs[0]
        assert text_spec.name == "text"
        assert text_spec.type is str
        assert text_spec.required is True
        assert text_spec.default is None

        count_spec = manifest.inputs[1]
        assert count_spec.name == "max_words"
        assert count_spec.type is int
        assert count_spec.required is False
        assert count_spec.default == 100

        dict_spec = manifest.inputs[2]
        assert dict_spec.name == "options"
        assert dict_spec.type is dict
        assert dict_spec.default == {"case_sensitive": False}

        assert manifest.required_permissions == (SkillPermission.READ_ONLY,)
        assert manifest.requires_approval is True


class TestStructuralAndJsonValidation:
    """Verify JSON structure, UTF-8, constants, duplicate keys, and bounds."""

    def test_invalid_input_data_type(self):
        with pytest.raises(ManifestValidationError, match="Expected bytes or str"):
            parse_skill_manifest(12345)  # type: ignore

    def test_empty_manifest_payload(self):
        with pytest.raises(ManifestValidationError, match="Manifest payload is empty"):
            parse_skill_manifest("")
        with pytest.raises(ManifestValidationError, match="Manifest payload is empty"):
            parse_skill_manifest("   \n\t  ")

    def test_malformed_json_syntax(self):
        with pytest.raises(ManifestValidationError, match="Malformed JSON"):
            parse_skill_manifest("{schema_version: 1, broken")

    def test_invalid_utf8_sequence(self):
        invalid_bytes = b'{"schema_version": 1, "id": "\xff\xfe"}'
        with pytest.raises(ManifestValidationError, match="Invalid UTF-8 byte sequence"):
            parse_skill_manifest(invalid_bytes)

    def test_duplicate_json_keys_at_top_level(self):
        raw = '{"schema_version": 1, "id": "user.a", "id": "user.b", "name": "N", "description": "D", "version": "1.0", "entrypoint": "m:C"}'
        with pytest.raises(ManifestValidationError, match="Duplicate key 'id'"):
            parse_skill_manifest(raw)

    def test_duplicate_json_keys_in_inputs(self):
        raw = """{
            "schema_version": 1,
            "id": "user.a",
            "name": "N",
            "description": "D",
            "version": "1.0",
            "entrypoint": "m:C",
            "inputs": [{"name": "x", "type": "str", "name": "x"}]
        }"""
        with pytest.raises(ManifestValidationError, match="Duplicate key 'name'"):
            parse_skill_manifest(raw)

    @pytest.mark.parametrize("constant_val", ["NaN", "Infinity", "-Infinity"])
    def test_reject_non_standard_json_constants(self, constant_val):
        raw = f"""{{
            "schema_version": 1,
            "id": "user.a",
            "name": "N",
            "description": "D",
            "version": "1.0",
            "entrypoint": "m:C",
            "inputs": [{{"name": "x", "type": "float", "default": {constant_val}}}]
        }}"""
        with pytest.raises(ManifestValidationError, match="Non-standard JSON constant"):
            parse_skill_manifest(raw)

    def test_reject_oversized_manifest(self):
        oversized = " " * (MAX_MANIFEST_BYTES + 1)
        with pytest.raises(ManifestValidationError, match="exceeds maximum allowed limit"):
            parse_skill_manifest(oversized)

        oversized_bytes = b" " * (MAX_MANIFEST_BYTES + 1)
        with pytest.raises(ManifestValidationError, match="exceeds maximum allowed limit"):
            parse_skill_manifest(oversized_bytes)

    def test_non_object_top_level_json(self):
        with pytest.raises(ManifestValidationError, match="Top-level manifest must be a JSON object"):
            parse_skill_manifest('["schema_version", 1]')
        with pytest.raises(ManifestValidationError, match="Top-level manifest must be a JSON object"):
            parse_skill_manifest('"user.skill"')


class TestRequiredAndUnexpectedFields:
    """Verify strict rejection of missing required fields and unknown extra fields."""

    @pytest.mark.parametrize(
        "field_to_remove",
        ["schema_version", "id", "name", "description", "version", "entrypoint"],
    )
    def test_missing_required_fields(self, field_to_remove):
        manifest_data = dict(VALID_MINIMAL_MANIFEST)
        del manifest_data[field_to_remove]
        with pytest.raises(ManifestValidationError, match="Missing required manifest field"):
            parse_skill_manifest(json.dumps(manifest_data))

    @pytest.mark.parametrize(
        "extra_field",
        ["author", "license", "command", "shell", "exec", "dependencies", "repo"],
    )
    def test_reject_unexpected_top_level_fields(self, extra_field):
        manifest_data = dict(VALID_MINIMAL_MANIFEST)
        manifest_data[extra_field] = "dangerous_or_unsupported_value"
        with pytest.raises(ManifestValidationError, match="Unexpected field"):
            parse_skill_manifest(json.dumps(manifest_data))


class TestSchemaVersionValidation:
    """Verify strict schema_version typing and value constraints."""

    @pytest.mark.parametrize("invalid_ver", [True, False, 2, 0, -1, 10, "1", 1.0])
    def test_invalid_schema_versions(self, invalid_ver):
        manifest_data = dict(VALID_MINIMAL_MANIFEST)
        manifest_data["schema_version"] = invalid_ver
        with pytest.raises(ManifestValidationError, match=r"(must be an integer|Unsupported schema_version)"):
            parse_skill_manifest(json.dumps(manifest_data))


class TestSkillIdentifierValidation:
    """Verify namespace enforcement, character constraints, length, and traversal prevention."""

    @pytest.mark.parametrize(
        "invalid_id,match_err",
        [
            ("my_skill", "must start with 'user.'"),
            ("system.weather", "must start with 'user.'"),
            ("user.", "cannot be just 'user.'"),
            ("user. weather", "must not contain whitespace"),
            ("user.weather ", "must not contain whitespace"),
            ("user.weather\n", "must not contain whitespace"),
            ("user.weather/sub", "contains path separators or directory traversal"),
            ("user.weather\\sub", "contains path separators or directory traversal"),
            ("user.weather..traverse", "contains path separators or directory traversal"),
            ("user.weather:tool", "contains invalid characters"),
            ("user.weather@2", "contains invalid characters"),
            ("user." + ("a" * (MAX_ID_LENGTH - 4)), "exceeds maximum length"),
            (12345, "must be a string"),
            ("", "must be a non-empty string"),
        ],
    )
    def test_invalid_skill_ids(self, invalid_id, match_err):
        manifest_data = dict(VALID_MINIMAL_MANIFEST)
        manifest_data["id"] = invalid_id
        with pytest.raises(ManifestValidationError, match=match_err):
            parse_skill_manifest(json.dumps(manifest_data))

    def test_skill_id_exact_maximum_length_accepted(self):
        """Skill ID at exactly MAX_ID_LENGTH characters is accepted."""
        # 'user.' is 5 characters, append (MAX_ID_LENGTH - 5) characters
        exact_id = "user." + ("a" * (MAX_ID_LENGTH - 5))
        assert len(exact_id) == MAX_ID_LENGTH
        manifest_data = dict(VALID_MINIMAL_MANIFEST)
        manifest_data["id"] = exact_id
        manifest = parse_skill_manifest(json.dumps(manifest_data))
        assert manifest.id == exact_id

    def test_skill_id_one_beyond_maximum_length_rejected(self):
        """Skill ID at MAX_ID_LENGTH + 1 characters is rejected."""
        too_long_id = "user." + ("a" * (MAX_ID_LENGTH - 4))
        assert len(too_long_id) == MAX_ID_LENGTH + 1
        manifest_data = dict(VALID_MINIMAL_MANIFEST)
        manifest_data["id"] = too_long_id
        with pytest.raises(ManifestValidationError, match=f"exceeds maximum length of {MAX_ID_LENGTH}"):
            parse_skill_manifest(json.dumps(manifest_data))

    @pytest.mark.parametrize(
        "valid_id",
        [
            "user.greeting",
            "user.example_tool",
            "user.custom-weather",
            "user.data.analyzer",
            "user.v2_process",
        ],
    )
    def test_valid_user_namespaced_skill_ids_accepted(self, valid_id):
        """Valid skill IDs under the user. namespace parse cleanly without hardcoded exclusions."""
        manifest_data = dict(VALID_MINIMAL_MANIFEST)
        manifest_data["id"] = valid_id
        manifest = parse_skill_manifest(json.dumps(manifest_data))
        assert manifest.id == valid_id


class TestEntrypointValidation:
    """Verify strict entrypoint syntax without host code resolution."""

    @pytest.mark.parametrize(
        "invalid_entrypoint,match_err",
        [
            ("WeatherSkill", "must follow 'module:ClassName'"),
            ("pkg.module:WeatherSkill", "Invalid module name"),
            ("pkg/module:WeatherSkill", "Invalid module name"),
            ("pkg\\module:WeatherSkill", "Invalid module name"),
            ("module:Class:Extra", "contains multiple colons"),
            ("123module:WeatherSkill", "Invalid module name"),
            ("module:123Skill", "Invalid class name"),
            ("import:WeatherSkill", "cannot be a Python keyword"),
            ("module:class", "cannot be a Python keyword"),
            ("module:Weather Skill", "must not contain whitespace"),
            (":WeatherSkill", "Invalid module name"),
            ("module:", "Invalid class name"),
            ("module:My-Skill", "Invalid class name"),
            ("m:" + ("A" * (MAX_ENTRYPOINT_LENGTH + 1)), "exceeds maximum length"),
            (123, "must be a string"),
            ("", "must be a non-empty string"),
        ],
    )
    def test_invalid_entrypoints(self, invalid_entrypoint, match_err):
        manifest_data = dict(VALID_MINIMAL_MANIFEST)
        manifest_data["entrypoint"] = invalid_entrypoint
        with pytest.raises(ManifestValidationError, match=match_err):
            parse_skill_manifest(json.dumps(manifest_data))


class TestInputSpecificationsValidation:
    """Verify input contract types, bounds, unexpected fields, and default values."""

    def test_inputs_not_a_list(self):
        manifest_data = dict(VALID_MINIMAL_MANIFEST)
        manifest_data["inputs"] = {"name": "x", "type": "str"}
        with pytest.raises(ManifestValidationError, match="inputs must be a list"):
            parse_skill_manifest(json.dumps(manifest_data))

    def test_excessive_input_fields_count(self):
        manifest_data = dict(VALID_MINIMAL_MANIFEST)
        manifest_data["inputs"] = [
            {"name": f"f_{i}", "type": "int"}
            for i in range(MAX_INPUT_FIELDS + 1)
        ]
        with pytest.raises(ManifestValidationError, match="exceeds maximum limit of 32"):
            parse_skill_manifest(json.dumps(manifest_data))

    def test_duplicate_input_field_names(self):
        manifest_data = dict(VALID_MINIMAL_MANIFEST)
        manifest_data["inputs"] = [
            {"name": "query", "type": "str"},
            {"name": "query", "type": "int"},
        ]
        with pytest.raises(ManifestValidationError, match="Duplicate input field name 'query'"):
            parse_skill_manifest(json.dumps(manifest_data))

    @pytest.mark.parametrize("unsupported_type", ["object", "custom", "set", "tuple", "bytes", "any"])
    def test_unsupported_input_types(self, unsupported_type):
        manifest_data = dict(VALID_MINIMAL_MANIFEST)
        manifest_data["inputs"] = [{"name": "target", "type": unsupported_type}]
        with pytest.raises(ManifestValidationError, match="Unsupported input type"):
            parse_skill_manifest(json.dumps(manifest_data))

    def test_unexpected_input_field_attribute(self):
        manifest_data = dict(VALID_MINIMAL_MANIFEST)
        manifest_data["inputs"] = [{"name": "x", "type": "str", "validator_regex": ".*"}]
        with pytest.raises(ManifestValidationError, match="contains unexpected field"):
            parse_skill_manifest(json.dumps(manifest_data))

    def test_required_field_cannot_declare_default(self):
        manifest_data = dict(VALID_MINIMAL_MANIFEST)
        manifest_data["inputs"] = [
            {"name": "target", "type": "str", "required": True, "default": "hello"}
        ]
        with pytest.raises(ManifestValidationError, match="cannot declare a non-null default value"):
            parse_skill_manifest(json.dumps(manifest_data))

    @pytest.mark.parametrize(
        "field_type,invalid_default",
        [
            ("int", "not_an_int"),
            ("int", True),  # In Python, isinstance(True, int) is True, but InputFieldSpec rejects it
            ("str", 123),
            ("bool", "true"),
            ("float", "12.34"),
            ("list", {"key": "val"}),
            ("dict", ["item"]),
        ],
    )
    def test_invalid_default_types(self, field_type, invalid_default):
        manifest_data = dict(VALID_MINIMAL_MANIFEST)
        manifest_data["inputs"] = [
            {"name": "param", "type": field_type, "required": False, "default": invalid_default}
        ]
        with pytest.raises(ManifestValidationError, match="Invalid default value"):
            parse_skill_manifest(json.dumps(manifest_data))


class TestPermissionsAndApprovalValidation:
    """Verify permission enum parsing, bounds, and approval flags."""

    def test_required_permissions_not_a_list(self):
        manifest_data = dict(VALID_MINIMAL_MANIFEST)
        manifest_data["required_permissions"] = "read_only"
        with pytest.raises(ManifestValidationError, match="required_permissions must be a list"):
            parse_skill_manifest(json.dumps(manifest_data))

    def test_unknown_permission_value(self):
        manifest_data = dict(VALID_MINIMAL_MANIFEST)
        manifest_data["required_permissions"] = ["read_only", "root_admin", "filesystem"]
        with pytest.raises(ManifestValidationError, match="Unknown permission 'root_admin'"):
            parse_skill_manifest(json.dumps(manifest_data))

    def test_duplicate_permission_values(self):
        manifest_data = dict(VALID_MINIMAL_MANIFEST)
        manifest_data["required_permissions"] = ["read_only", "read_only"]
        with pytest.raises(ManifestValidationError, match="Duplicate permission"):
            parse_skill_manifest(json.dumps(manifest_data))

    def test_excessive_permissions_count(self):
        manifest_data = dict(VALID_MINIMAL_MANIFEST)
        # More than MAX_PERMISSIONS items
        manifest_data["required_permissions"] = ["none"] * (MAX_PERMISSIONS + 1)
        with pytest.raises(ManifestValidationError, match="exceeds maximum limit of 8"):
            parse_skill_manifest(json.dumps(manifest_data))

    def test_invalid_requires_approval_type(self):
        manifest_data = dict(VALID_MINIMAL_MANIFEST)
        manifest_data["requires_approval"] = "yes"
        with pytest.raises(ManifestValidationError, match="requires_approval must be a boolean"):
            parse_skill_manifest(json.dumps(manifest_data))


class TestStringLengthLimits:
    """Verify maximum string length constraints across all manifest fields."""

    def test_excessive_name_length(self):
        manifest_data = dict(VALID_MINIMAL_MANIFEST)
        manifest_data["name"] = "N" * (MAX_NAME_LENGTH + 1)
        with pytest.raises(ManifestValidationError, match="exceeds maximum length"):
            parse_skill_manifest(json.dumps(manifest_data))

    def test_excessive_description_length(self):
        manifest_data = dict(VALID_MINIMAL_MANIFEST)
        manifest_data["description"] = "D" * (MAX_DESCRIPTION_LENGTH + 1)
        with pytest.raises(ManifestValidationError, match="exceeds maximum length"):
            parse_skill_manifest(json.dumps(manifest_data))

    def test_excessive_version_length(self):
        manifest_data = dict(VALID_MINIMAL_MANIFEST)
        manifest_data["version"] = "1." + ("0" * (MAX_VERSION_LENGTH + 1))
        with pytest.raises(ManifestValidationError, match="exceeds maximum length"):
            parse_skill_manifest(json.dumps(manifest_data))

    def test_excessive_input_name_length(self):
        manifest_data = dict(VALID_MINIMAL_MANIFEST)
        manifest_data["inputs"] = [{"name": "f" * (MAX_FIELD_NAME_LENGTH + 1), "type": "str"}]
        with pytest.raises(ManifestValidationError, match="exceeds maximum length"):
            parse_skill_manifest(json.dumps(manifest_data))

    def test_excessive_input_description_length(self):
        manifest_data = dict(VALID_MINIMAL_MANIFEST)
        manifest_data["inputs"] = [
            {"name": "f1", "type": "str", "description": "D" * (MAX_FIELD_DESCRIPTION_LENGTH + 1)}
        ]
        with pytest.raises(ManifestValidationError, match="exceeds maximum length"):
            parse_skill_manifest(json.dumps(manifest_data))


class TestPathLoaderBehavior:
    """Verify load_skill_manifest file reading, path checks, and IO error handling."""

    def test_load_valid_manifest_from_path(self, tmp_path: Path):
        manifest_path = tmp_path / "skill.json"
        manifest_path.write_text(json.dumps(VALID_MINIMAL_MANIFEST), encoding="utf-8")

        manifest = load_skill_manifest(manifest_path)
        assert manifest.id == "user.simple_skill"

    def test_load_manifest_non_existent_file(self, tmp_path: Path):
        missing_path = tmp_path / "missing.json"
        with pytest.raises(ManifestValidationError, match="Manifest file not found"):
            load_skill_manifest(missing_path)

    def test_load_manifest_directory_path_rejected(self, tmp_path: Path):
        dir_path = tmp_path / "somedir"
        dir_path.mkdir()
        with pytest.raises(ManifestValidationError, match="is not a regular file"):
            load_skill_manifest(dir_path)

    def test_load_manifest_oversized_on_disk(self, tmp_path: Path):
        big_path = tmp_path / "big_skill.json"
        big_path.write_bytes(b" " * (MAX_MANIFEST_BYTES + 10))
        with pytest.raises(ManifestValidationError, match="exceeds maximum limit"):
            load_skill_manifest(big_path)

    def test_load_manifest_invalid_path_argument_type(self):
        with pytest.raises(ManifestValidationError, match="Expected Path or str"):
            load_skill_manifest(12345)  # type: ignore


class TestModelContractSemantics:
    """Verify manifest parsing strictly preserves existing Anaya model contracts."""

    def test_to_metadata_field_preservation(self):
        """to_metadata() maps all attributes to SkillMetadata with exact fidelity."""
        raw = json.dumps(VALID_FULL_MANIFEST)
        manifest = parse_skill_manifest(raw)
        metadata = manifest.to_metadata(enabled=True)

        assert metadata.id == manifest.id
        assert metadata.name == manifest.name
        assert metadata.description == manifest.description
        assert metadata.version == manifest.version
        assert metadata.inputs == manifest.inputs
        assert metadata.required_permissions == manifest.required_permissions
        assert metadata.enabled is True
        assert metadata.requires_approval is True

    def test_optional_input_with_null_default(self):
        """Optional field with explicit null default is valid."""
        data = dict(VALID_MINIMAL_MANIFEST)
        data["inputs"] = [{"name": "opt_field", "type": "str", "required": False, "default": None}]
        manifest = parse_skill_manifest(json.dumps(data))
        spec = manifest.inputs[0]
        assert spec.required is False
        assert spec.default is None
        is_valid, err = spec.validate(None)
        assert is_valid is True
        assert err is None

    def test_strict_int_vs_bool_default_contract(self):
        """InputFieldSpec strict distinction between int and bool is preserved."""
        data = dict(VALID_MINIMAL_MANIFEST)
        data["inputs"] = [{"name": "count", "type": "int", "required": False, "default": True}]
        with pytest.raises(ManifestValidationError, match="Invalid default value"):
            parse_skill_manifest(json.dumps(data))

    @pytest.mark.parametrize(
        "perm_name,expected_enum",
        [
            ("none", SkillPermission.NONE),
            ("read_only", SkillPermission.READ_ONLY),
            ("filesystem", SkillPermission.FILESYSTEM),
            ("approval_required", SkillPermission.APPROVAL_REQUIRED),
        ],
    )
    def test_all_permission_enum_values_mapped(self, perm_name, expected_enum):
        """All existing SkillPermission enum values are recognized and mapped."""
        data = dict(VALID_MINIMAL_MANIFEST)
        data["required_permissions"] = [perm_name]
        manifest = parse_skill_manifest(json.dumps(data))
        assert manifest.required_permissions == (expected_enum,)
