# Anaya Modular Skills Framework

## 1. Overview

The Anaya Modular Skills Framework (`app/skills/`) provides a secure, controlled, and extensible architecture allowing Anaya to register, discover, validate, and execute self-contained modular skills.

In accordance with Anaya's core architectural tenets:
- **Local-First & Zero Dependencies:** Operates exclusively using the Python 3.12 standard library.
- **Strict Input Contracts:** All skill inputs are validated against explicit, declared typed specifications (`InputFieldSpec`) prior to invoking skill implementations. Input pollution and unexpected parameters are rejected.
- **Fail-Safe Exception Isolation:** Skill execution is isolated so that unexpected implementation runtime crashes never compromise or crash Anaya's Brain.
- **Out-of-Band Security:** Sensitive skills requiring administrative approval integrate with Anaya's existing `ApprovalService`. In-band conversational confirmations ("yes", "confirm", "approve") can never independently authorize execution.
- **No Arbitrary Code Execution:** The framework strictly forbids `eval()`, `exec()`, arbitrary shell execution, dynamic unvetted module imports, or remote marketplace downloads. Only trusted, explicitly registered Python skills can execute.

---

## 2. Directory Structure

```text
app/
└── skills/
    ├── __init__.py               # Public API exports
    ├── models.py                 # SkillMetadata, InputFieldSpec, SkillResult, SkillPermission, Skill base class
    ├── skill_registry.py         # Thread-safe skill storage, lookups, and lifecycle state
    ├── skill_executor.py         # Input validation, permission gating, isolated execution
    ├── skill_manager.py          # High-level facade for discovery, validation, and execution
    └── builtin/
        ├── __init__.py           # Built-in skills package
        └── example_skill.py      # Example greeting skill (example.greeting)
```

---

## 3. Core Architecture and Lifecycle

```text
                     [ User Natural Language / CLI ]
                                   │
                                   ▼
                           [ TextAssistant ]
                                   │
                                   ▼
                          [ CommandExecutor ]
                                   │
                    ┌──────────────┴──────────────┐
                    │ (skills.list / skills.run)  │
                    ▼                             ▼
             [ SkillManager ]              [ ApprovalService ]
                    │                       (if confirmation
                    ▼                        required)
            [ SkillExecutor ] ◄───────────────────┘
                    │
            ┌───────┴───────┐
            ▼               ▼
    [ SkillRegistry ]  [ Input Validation ]
            │               │
            └───────┬───────┘
                    ▼
            [ Skill.execute() ]
```

### Components

| Module | Location | Primary Responsibility |
|---|---|---|
| `models` | `app/skills/models.py` | `Skill` base class, `SkillMetadata`, `InputFieldSpec`, `SkillResult`, and `SkillPermission` enums. |
| `skill_registry` | `app/skills/skill_registry.py` | Thread-safe in-memory store for registered skills, preventing duplicate registrations, and managing enable/disable states. |
| `skill_executor` | `app/skills/skill_executor.py` | Validates input contracts, rejects unknown fields, verifies approval tokens via `ApprovalService`, normalizes results, and isolates runtime errors. |
| `skill_manager` | `app/skills/skill_manager.py` | High-level facade exposing skill discovery, description lookups, input pre-validation, and execution. |
| `skill_commands` | `app/commands/skill_commands.py` | Exposes skill operations through Anaya's core command registry (`skills.list` and `skills.run`). |
| `example_skill` | `app/skills/builtin/example_skill.py` | Demonstrates registration, metadata, contract validation, and execution with a harmless greeting skill. |

---

## 4. The Skill Contract

Every skill must subclass `Skill` and declare an immutable `SkillMetadata`:

```python
from typing import Any
from app.skills import Skill, SkillMetadata, InputFieldSpec, SkillPermission, SkillResult

class MyCustomSkill(Skill):
    metadata = SkillMetadata(
        id="custom.reverse_text",
        name="Reverse Text",
        description="Reverses the provided input string.",
        version="1.0.0",
        inputs=(
            InputFieldSpec(
                name="text",
                type=str,
                required=True,
                description="The string to be reversed.",
            ),
        ),
        required_permissions=(SkillPermission.NONE,),
        enabled=True,
        requires_approval=False,
    )

    def execute(self, inputs: dict[str, Any]) -> SkillResult:
        text = inputs["text"]
        reversed_text = text[::-1]
        return SkillResult(
            success=True,
            data={"original": text, "reversed": reversed_text},
            message=reversed_text,
        )
```

### Input Field Specifications (`InputFieldSpec`)
- `name`: Non-empty string identifying the field.
- `type`: Expected Python type (`str`, `int`, `float`, `bool`, `dict`, `list`).
- `required`: If `True`, missing values trigger an immediate validation failure.
- `default`: Default value applied if an optional field is omitted.
- `description`: Explains field purpose for discovery and documentation.

---

## 5. Security & Permission Model

### Permission Types (`SkillPermission`)

- `SkillPermission.NONE`: No additional permission requirement. The skill runs under standard in-process execution.
- `SkillPermission.APPROVAL_REQUIRED`: Actively enforced through the existing `ApprovalService` workflow. Execution halts until an explicit out-of-band authorization is granted.
- `SkillPermission.READ_ONLY`: Currently a declarative metadata contract, **not** an enforced filesystem sandbox.
- `SkillPermission.FILESYSTEM`: Currently a declarative metadata contract, **not** an OS-level filesystem restriction.

Skills execute as trusted in-process Python code. Arbitrary standard-library filesystem operations (such as `open()`, `pathlib.Path`, or `os`) are not actively sandboxed by `READ_ONLY` or `FILESYSTEM`. Declaring these permissions indicates intended operational scope for auditing and discovery, but does not prevent unauthorized standard-library operations at runtime.

### Security Guarantees

1. **No Code Generation or Dynamic Eval:**
   Skills must be written as trusted Python classes. The framework does not execute raw string code, evaluate arbitrary scripts, or run dynamic untrusted bytecode.
2. **Explicit Registration:**
   Skills must be explicitly added to a `SkillRegistry` by trusted application initialization code. Arbitrary files placed in a folder are never automatically imported or executed.
3. **Out-of-Band Human Approval for Sensitive Actions:**
   When a skill declares `requires_approval = True` or requires `SkillPermission.APPROVAL_REQUIRED`:
   - Execution without an approval ID halts and returns `confirmation_required = True` alongside a generated confirmation token.
   - The user must explicitly approve the action out-of-band using `ApprovalService.approve()`.
   - In-band chat confirmations ("yes", "confirm", "approve") are caught and blocked by `TextAssistant`'s security barrier.
   - Authorizations are single-use and bound to the exact skill ID and canonical JSON-serialized input parameters (`json.dumps(validated_inputs, sort_keys=True)`). Replays and parameter modifications are rejected.
4. **Input Contract Enforcement & Pollution Rejection:**
   Supplying unexpected fields not defined in `SkillMetadata.inputs` causes `SkillExecutor` to fail validation immediately, preventing unexpected argument injection into handlers.

---

## 6. How to Register and Use a Trusted Skill

### Programmatic Usage

```python
from app.skills import SkillManager, register_builtin_skills
from app.skills.builtin.example_skill import GreetingSkill

# 1. Initialize manager
manager = SkillManager()

# 2. Register built-in or custom skills
register_builtin_skills(manager.registry)

# 3. Discover available skills
available = manager.discover_skills()
for skill in available:
    print(f"- {skill['id']}: {skill['name']}")

# 4. Execute skill with inputs
result = manager.execute_skill("example.greeting", {"name": "Alice"})
if result.success:
    print(result.message)  # "Hello, Alice!"
```

### CLI Command Usage

Registered skills are exposed via standard commands:

- **List available skills:**
  ```text
  skills.list
  ```
- **Execute with default arguments:**
  ```text
  skills.run example.greeting
  ```
- **Execute with JSON arguments:**
  ```text
  skills.run example.greeting {"name": "Alice"}
  ```

### Conversational / Natural Language Usage

When `SkillManager` is provided to `TextAssistant`:

- **Discovery:**
  - *"What skills are available?"*
  - *"List available skills."*
  - *"Show available skills."*
- **Execution:**
  - *"Run the greeting skill."* -> Greets with default ("Hello, friend!")
  - *"Run the greeting skill with name Alice"* -> "Hello, Alice!"
  - *"Greet Alice using the greeting skill"* -> "Hello, Alice!"
  - *"Run skill example.greeting with {"name": "Charlie"}"* -> "Hello, Charlie!"

---

## 7. Testing Instructions

Run all skills framework unit, integration, and security tests:

```powershell
.\.venv312win\Scripts\pytest.exe -v tests/test_skills
```

Run the complete repository regression test suite:

```powershell
.\.venv312win\Scripts\pytest.exe -v
```

---

## 8. Current Limitations

1. **In-Memory Registry:** Skills are registered in memory during application initialization. Dynamic persistence of registered external skill manifests across restarts is not implemented in this phase.
2. **Synchronous Execution:** Skills execute synchronously within the process. Long-running asynchronous tasks or background worker threading are deferred to future phases.
3. **Local Scope:** All skills execute within the local Python runtime environment.
