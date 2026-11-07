"""Declarative external skill manifest model and strict schema parser (Phase 10.4.1).

Provides pure data structures and deterministic parsing for external skill packages:
    ~/.anaya/skills/<skill-directory>/skill.json

Security Invariants:
    - Never uses eval, exec, importlib, or dynamic Python source imports in the host.
    - Never executes paths, shell commands, or subprocesses based on manifest values.
    - Never interprets manifest permission declarations as authorized grants.
    - Rejects oversized files, duplicate JSON keys, NaN/Infinity, and malformed inputs fail-closed.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import keyword
from pathlib import Path
import re
from typing import Any

from app.skills.models import (
    InputFieldSpec,
    SkillMetadata,
    SkillPermission,
)
from app.skills.protocol import MAX_ID_LENGTH

# Manifest Limits and Constraints
MAX_MANIFEST_BYTES: int = 65_536  # 64 KiB maximum manifest file size
MAX_NAME_LENGTH: int = 128
MAX_DESCRIPTION_LENGTH: int = 1024
MAX_VERSION_LENGTH: int = 32
MAX_ENTRYPOINT_LENGTH: int = 128
MAX_INPUT_FIELDS: int = 32
MAX_FIELD_NAME_LENGTH: int = 64
MAX_FIELD_DESCRIPTION_LENGTH: int = 512
MAX_PERMISSIONS: int = 8

# Regex Patterns
SKILL_ID_PATTERN: re.Pattern[str] = re.compile(r"^user\.[a-zA-Z0-9_.-]+$")
PYTHON_IDENTIFIER_PATTERN: re.Pattern[str] = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
VERSION_PATTERN: re.Pattern[str] = re.compile(r"^[0-9]+(\.[0-9]+)*(?:-[a-zA-Z0-9_.-]+)?$")

# Supported types for InputFieldSpec contract
SUPPORTED_INPUT_TYPES: dict[str, type] = {
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "list": list,
    "dict": dict,
}

# Allowed top-level JSON fields in skill.json
ALLOWED_MANIFEST_KEYS: frozenset[str] = frozenset({
    "schema_version",
    "id",
    "name",
    "description",
    "version",
    "entrypoint",
    "inputs",
    "required_permissions",
    "requires_approval",
})

REQUIRED_MANIFEST_KEYS: frozenset[str] = frozenset({
    "schema_version",
    "id",
    "name",
    "description",
    "version",
    "entrypoint",
})


class ManifestValidationError(ValueError):
    """Raised when a skill.json manifest fails structural, schema, or security validation."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Object pairs hook for json.loads that strictly rejects duplicate keys at any nesting level."""
    d: dict[str, Any] = {}
    for k, v in pairs:
        if k in d:
            raise ManifestValidationError(f"Duplicate key '{k}' detected in JSON object.")
        d[k] = v
    return d


def _reject_constant(c: str) -> None:
    """Constant parser hook for json.loads that strictly rejects non-standard JSON constants (NaN, Infinity)."""
    raise ManifestValidationError(f"Non-standard JSON constant '{c}' is not permitted.")


@dataclass(frozen=True)
class SkillManifest:
    """Immutable parsed representation of an external skill.json manifest."""

    schema_version: int
    id: str
    name: str
    description: str
    version: str
    entrypoint: str
    inputs: tuple[InputFieldSpec, ...] = ()
    required_permissions: tuple[SkillPermission, ...] = (SkillPermission.NONE,)
    requires_approval: bool = False

    @property
    def entrypoint_module(self) -> str:
        """Module name component of the entry point (e.g. 'skill' from 'skill:MySkill')."""
        return self.entrypoint.split(":", 1)[0]

    @property
    def entrypoint_class(self) -> str:
        """Class name component of the entry point (e.g. 'MySkill' from 'skill:MySkill')."""
        return self.entrypoint.split(":", 1)[1]

    def to_metadata(self, enabled: bool = True) -> SkillMetadata:
        """Convert this manifest into an authoritative SkillMetadata instance."""
        return SkillMetadata(
            id=self.id,
            name=self.name,
            description=self.description,
            version=self.version,
            inputs=self.inputs,
            required_permissions=self.required_permissions,
            enabled=enabled,
            requires_approval=self.requires_approval,
        )


def parse_skill_manifest(data: str | bytes) -> SkillManifest:
    """Parse and validate a raw manifest string or byte sequence into a SkillManifest.

    Security & Validation Guarantees:
        - Strict UTF-8 decoding.
        - Rejects inputs exceeding MAX_MANIFEST_BYTES (64 KiB).
        - Rejects malformed JSON, duplicate keys, and NaN/Infinity constants.
        - Enforces schema_version == 1 (as integer, rejecting bool).
        - Enforces 'user.' ID namespace prefix, identifier regex, and traversal sequence rejection.
        - Validates 'module:ClassName' entrypoint syntax without loading or resolving code.
        - Validates InputFieldSpec field types, bounds, and defaults.
        - Validates declared permissions against existing SkillPermission enum.
    """
    if isinstance(data, bytes):
        if len(data) > MAX_MANIFEST_BYTES:
            raise ManifestValidationError(
                f"Manifest size ({len(data)} bytes) exceeds maximum allowed limit of {MAX_MANIFEST_BYTES} bytes."
            )
        try:
            raw_str = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ManifestValidationError(f"Invalid UTF-8 byte sequence in manifest: {exc}") from exc
    elif isinstance(data, str):
        raw_bytes = data.encode("utf-8")
        if len(raw_bytes) > MAX_MANIFEST_BYTES:
            raise ManifestValidationError(
                f"Manifest size ({len(raw_bytes)} bytes) exceeds maximum allowed limit of {MAX_MANIFEST_BYTES} bytes."
            )
        raw_str = data
    else:
        raise ManifestValidationError(f"Expected bytes or str for manifest data, got {type(data).__name__}.")

    clean_str = raw_str.strip()
    if not clean_str:
        raise ManifestValidationError("Manifest payload is empty.")

    try:
        parsed = json.loads(
            clean_str,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise ManifestValidationError(f"Malformed JSON in manifest: {exc.msg}") from exc
    except RecursionError as exc:
        raise ManifestValidationError("Maximum JSON nesting depth exceeded in manifest.") from exc

    if not isinstance(parsed, dict):
        raise ManifestValidationError(f"Top-level manifest must be a JSON object, got {type(parsed).__name__}.")

    # 1. Structural Field Completeness & Unexpected Field Rejection
    missing = REQUIRED_MANIFEST_KEYS - set(parsed.keys())
    if missing:
        raise ManifestValidationError(f"Missing required manifest field(s): {sorted(missing)}.")

    extra = set(parsed.keys()) - ALLOWED_MANIFEST_KEYS
    if extra:
        raise ManifestValidationError(f"Unexpected field(s) in manifest: {sorted(extra)}.")

    # 2. Schema Version Validation
    schema_ver = parsed["schema_version"]
    if isinstance(schema_ver, bool) or not isinstance(schema_ver, int):
        raise ManifestValidationError(f"schema_version must be an integer, got {type(schema_ver).__name__}.")
    if schema_ver != 1:
        raise ManifestValidationError(f"Unsupported schema_version '{schema_ver}'. Expected 1.")

    # 3. Skill Identifier Validation
    skill_id = parsed["id"]
    if not isinstance(skill_id, str):
        raise ManifestValidationError(f"Skill ID must be a string, got {type(skill_id).__name__}.")
    if not skill_id.strip():
        raise ManifestValidationError("Skill ID must be a non-empty string.")
    if skill_id != skill_id.strip() or any(c.isspace() for c in skill_id):
        raise ManifestValidationError("Skill ID must not contain whitespace.")
    if len(skill_id) > MAX_ID_LENGTH:
        raise ManifestValidationError(f"Skill ID exceeds maximum length of {MAX_ID_LENGTH} characters.")
    if not skill_id.startswith("user."):
        raise ManifestValidationError(f"External skill ID '{skill_id}' must start with 'user.' namespace prefix.")
    suffix = skill_id[len("user."):]
    if not suffix:
        raise ManifestValidationError("External skill ID cannot be just 'user.'. A valid skill name suffix is required.")
    if ".." in skill_id or "/" in skill_id or "\\" in skill_id:
        raise ManifestValidationError(f"Skill ID '{skill_id}' contains path separators or directory traversal sequences.")
    if not SKILL_ID_PATTERN.fullmatch(skill_id):
        raise ManifestValidationError(
            f"Skill ID '{skill_id}' contains invalid characters. "
            "Only alphanumeric characters, dots, underscores, and hyphens are permitted under 'user.' prefix."
        )

    # 4. Display Name Validation
    name = parsed["name"]
    if not isinstance(name, str):
        raise ManifestValidationError(f"Skill name must be a string, got {type(name).__name__}.")
    if not name.strip():
        raise ManifestValidationError("Skill name must be a non-empty string.")
    if len(name) > MAX_NAME_LENGTH:
        raise ManifestValidationError(f"Skill name exceeds maximum length of {MAX_NAME_LENGTH} characters.")

    # 5. Description Validation
    description = parsed["description"]
    if not isinstance(description, str):
        raise ManifestValidationError(f"Skill description must be a string, got {type(description).__name__}.")
    if len(description) > MAX_DESCRIPTION_LENGTH:
        raise ManifestValidationError(
            f"Skill description exceeds maximum length of {MAX_DESCRIPTION_LENGTH} characters."
        )

    # 6. Version Validation
    version = parsed["version"]
    if not isinstance(version, str):
        raise ManifestValidationError(f"Skill version must be a string, got {type(version).__name__}.")
    if not version.strip():
        raise ManifestValidationError("Skill version must be a non-empty string.")
    if version != version.strip() or any(c.isspace() for c in version):
        raise ManifestValidationError("Skill version must not contain whitespace.")
    if len(version) > MAX_VERSION_LENGTH:
        raise ManifestValidationError(f"Skill version exceeds maximum length of {MAX_VERSION_LENGTH} characters.")
    if not VERSION_PATTERN.fullmatch(version):
        raise ManifestValidationError(f"Skill version '{version}' does not match standard semantic version format.")

    # 7. Entrypoint Validation
    entrypoint = parsed["entrypoint"]
    if not isinstance(entrypoint, str):
        raise ManifestValidationError(f"entrypoint must be a string, got {type(entrypoint).__name__}.")
    if not entrypoint.strip():
        raise ManifestValidationError("entrypoint must be a non-empty string.")
    if entrypoint != entrypoint.strip() or any(c.isspace() for c in entrypoint):
        raise ManifestValidationError("entrypoint must not contain whitespace.")
    if len(entrypoint) > MAX_ENTRYPOINT_LENGTH:
        raise ManifestValidationError(f"entrypoint exceeds maximum length of {MAX_ENTRYPOINT_LENGTH} characters.")
    if ":" not in entrypoint:
        raise ManifestValidationError(
            f"entrypoint '{entrypoint}' must follow 'module:ClassName' format (e.g. 'skill:MySkill')."
        )
    parts = entrypoint.split(":")
    if len(parts) != 2:
        raise ManifestValidationError(
            f"entrypoint '{entrypoint}' contains multiple colons; expected exactly one 'module:ClassName'."
        )
    module_name, class_name = parts
    if not PYTHON_IDENTIFIER_PATTERN.fullmatch(module_name):
        raise ManifestValidationError(
            f"Invalid module name '{module_name}' in entrypoint. Must be a valid Python identifier without dots or slashes."
        )
    if not PYTHON_IDENTIFIER_PATTERN.fullmatch(class_name):
        raise ManifestValidationError(
            f"Invalid class name '{class_name}' in entrypoint. Must be a valid Python identifier."
        )
    if keyword.iskeyword(module_name) or keyword.iskeyword(class_name):
        raise ManifestValidationError(f"entrypoint component ('{module_name}' or '{class_name}') cannot be a Python keyword.")

    # 8. Input Specifications Validation
    input_specs: list[InputFieldSpec] = []
    if "inputs" in parsed:
        raw_inputs = parsed["inputs"]
        if not isinstance(raw_inputs, list):
            raise ManifestValidationError(f"inputs must be a list, got {type(raw_inputs).__name__}.")
        if len(raw_inputs) > MAX_INPUT_FIELDS:
            raise ManifestValidationError(
                f"Number of input fields ({len(raw_inputs)}) exceeds maximum limit of {MAX_INPUT_FIELDS}."
            )

        seen_input_names: set[str] = set()
        for idx, item in enumerate(raw_inputs):
            if not isinstance(item, dict):
                raise ManifestValidationError(f"Item {idx} in inputs must be a dictionary, got {type(item).__name__}.")

            allowed_input_keys = {"name", "type", "required", "default", "description"}
            input_missing = {"name", "type"} - set(item.keys())
            if input_missing:
                raise ManifestValidationError(f"Item {idx} in inputs missing required field(s): {sorted(input_missing)}.")
            input_extra = set(item.keys()) - allowed_input_keys
            if input_extra:
                raise ManifestValidationError(f"Item {idx} in inputs contains unexpected field(s): {sorted(input_extra)}.")

            inp_name = item["name"]
            if not isinstance(inp_name, str) or not inp_name.strip():
                raise ManifestValidationError(f"Item {idx} in inputs has invalid name; must be a non-empty string.")
            if len(inp_name) > MAX_FIELD_NAME_LENGTH:
                raise ManifestValidationError(
                    f"Field name '{inp_name}' exceeds maximum length of {MAX_FIELD_NAME_LENGTH} characters."
                )
            if not PYTHON_IDENTIFIER_PATTERN.fullmatch(inp_name):
                raise ManifestValidationError(f"Field name '{inp_name}' is not a valid Python identifier.")
            if inp_name in seen_input_names:
                raise ManifestValidationError(f"Duplicate input field name '{inp_name}' detected in inputs.")
            seen_input_names.add(inp_name)

            inp_type_str = item["type"]
            if not isinstance(inp_type_str, str):
                raise ManifestValidationError(f"Field '{inp_name}' type must be a string, got {type(inp_type_str).__name__}.")
            if inp_type_str not in SUPPORTED_INPUT_TYPES:
                raise ManifestValidationError(
                    f"Unsupported input type '{inp_type_str}' for field '{inp_name}'. "
                    f"Supported types: {sorted(SUPPORTED_INPUT_TYPES.keys())}."
                )
            resolved_type = SUPPORTED_INPUT_TYPES[inp_type_str]

            required = item.get("required", False)
            if not isinstance(required, bool):
                raise ManifestValidationError(f"Field '{inp_name}' 'required' must be a boolean.")

            desc = item.get("description", "")
            if not isinstance(desc, str):
                raise ManifestValidationError(f"Field '{inp_name}' 'description' must be a string.")
            if len(desc) > MAX_FIELD_DESCRIPTION_LENGTH:
                raise ManifestValidationError(
                    f"Field '{inp_name}' description exceeds maximum length of {MAX_FIELD_DESCRIPTION_LENGTH} characters."
                )

            default = item.get("default", None)
            if default is not None:
                if required:
                    raise ManifestValidationError(f"Required input field '{inp_name}' cannot declare a non-null default value.")

                spec = InputFieldSpec(
                    name=inp_name,
                    type=resolved_type,
                    required=required,
                    default=default,
                    description=desc,
                )
                is_valid, err_msg = spec.validate(default)
                if not is_valid:
                    raise ManifestValidationError(f"Invalid default value for field '{inp_name}': {err_msg}")
            else:
                spec = InputFieldSpec(
                    name=inp_name,
                    type=resolved_type,
                    required=required,
                    default=None,
                    description=desc,
                )
            input_specs.append(spec)

    # 9. Required Permissions Validation
    perm_specs: list[SkillPermission] = []
    if "required_permissions" in parsed:
        raw_perms = parsed["required_permissions"]
        if not isinstance(raw_perms, list):
            raise ManifestValidationError(f"required_permissions must be a list, got {type(raw_perms).__name__}.")
        if len(raw_perms) > MAX_PERMISSIONS:
            raise ManifestValidationError(
                f"Number of required permissions ({len(raw_perms)}) exceeds maximum limit of {MAX_PERMISSIONS}."
            )

        seen_perms: set[str] = set()
        valid_perm_values = {p.value: p for p in SkillPermission}
        for idx, perm_item in enumerate(raw_perms):
            if not isinstance(perm_item, str):
                raise ManifestValidationError(
                    f"Item {idx} in required_permissions must be a string, got {type(perm_item).__name__}."
                )
            cleaned_perm = perm_item.strip().lower()
            if cleaned_perm not in valid_perm_values:
                raise ManifestValidationError(
                    f"Unknown permission '{perm_item}' in required_permissions. "
                    f"Supported permissions: {sorted(valid_perm_values.keys())}."
                )
            if cleaned_perm in seen_perms:
                raise ManifestValidationError(f"Duplicate permission '{perm_item}' in required_permissions.")
            seen_perms.add(cleaned_perm)
            perm_specs.append(valid_perm_values[cleaned_perm])

    final_permissions = tuple(perm_specs) if perm_specs else (SkillPermission.NONE,)

    # 10. Approval Requirement Validation
    requires_approval = False
    if "requires_approval" in parsed:
        raw_appr = parsed["requires_approval"]
        if not isinstance(raw_appr, bool):
            raise ManifestValidationError(f"requires_approval must be a boolean, got {type(raw_appr).__name__}.")
        requires_approval = raw_appr

    return SkillManifest(
        schema_version=schema_ver,
        id=skill_id,
        name=name,
        description=description,
        version=version,
        entrypoint=entrypoint,
        inputs=tuple(input_specs),
        required_permissions=final_permissions,
        requires_approval=requires_approval,
    )


def load_skill_manifest(path: Path | str) -> SkillManifest:
    """Load and validate a skill manifest from a file path on disk.

    Raises:
        ManifestValidationError: If the file does not exist, is not a regular file,
            exceeds MAX_MANIFEST_BYTES, cannot be read, or fails schema validation.
    """
    if not isinstance(path, (Path, str)):
        raise ManifestValidationError(f"Expected Path or str for manifest path, got {type(path).__name__}.")

    p = Path(path)
    try:
        st = p.stat()
    except FileNotFoundError as exc:
        raise ManifestValidationError(f"Manifest file not found at '{path}'.") from exc
    except OSError as exc:
        raise ManifestValidationError(f"Failed to access manifest file '{path}': {exc}") from exc

    if not p.is_file():
        raise ManifestValidationError(f"Manifest path '{path}' is not a regular file.")

    if st.st_size > MAX_MANIFEST_BYTES:
        raise ManifestValidationError(
            f"Manifest file size ({st.st_size} bytes) exceeds maximum limit of {MAX_MANIFEST_BYTES} bytes."
        )

    try:
        content = p.read_bytes()
    except OSError as exc:
        raise ManifestValidationError(f"Failed to read manifest file '{path}': {exc}") from exc

    return parse_skill_manifest(content)
