# Anaya AI — Phase 0: Existing Repository Audit Report

**Date:** November 2026  
**Phase:** 0 (Repository Audit & Architecture Analysis)  
**Status:** Completed  

---

## 1. Current Repository Structure

The repository contains an existing, functional core foundation for the Anaya desktop AI assistant. It includes core configuration, logging, permissions, security controls (approval service, confirmation manager, path validator), command execution architecture, a hardened filesystem service, and an intent/text processing front-end.

```
anaya/
├── .claude/
│   └── settings.json            # Tool hook guards for graphify integration
├── .gitignore                   # Ignores venvs, pycache, .env, models, logs, dbs
├── .pytest_cache/               # Local test cache
├── .vscode/
│   └── settings.json            # VS Code Python environment configuration
├── CLAUDE.md                    # Graphify workflow rules and navigation guide
├── docs/
│   ├── phase-0-audit.md         # This audit report
│   └── security.md              # Filesystem security assumptions and trust boundary documentation
├── install-ponytail.ps1         # Installer script for Ponytail Antigravity IDE skills
├── readme.md                    # Environment activation and execution policy instructions
├── app/
│   ├── __init__.py              # Package marker (empty)
│   ├── brain/
│   │   ├── __init__.py          # Package marker (empty)
│   │   ├── intent_handler.py    # Command intent parser using shlex with registry validation
│   │   └── text_assistant.py    # Text entrypoint with strict separation of text & approval channels
│   ├── commands/
│   │   ├── __init__.py          # Package marker (empty)
│   │   ├── executor.py          # Command execution engine enforcing approvals and argument validation
│   │   ├── filesystem.py        # Hardened filesystem service with sandboxing and dir_fd support
│   │   ├── filesystem_commands.py # Adapter registering file operations as commands
│   │   └── registry.py          # Central registry for command specifications and argument validators
│   ├── core/
│   │   ├── __init__.py          # Package marker (empty)
│   │   ├── action_result.py     # Immutable ActionResult dataclass (success, message, cancelled)
│   │   ├── config.py            # AppConfig dataclass and DEFAULT_CONFIG (speech, permissions, defaults)
│   │   ├── logger.py            # Standardized logger factory
│   │   └── permissions.py       # PermissionDecision dataclass and internet access permission check
│   ├── security/
│   │   ├── __init__.py          # Package marker (empty)
│   │   ├── approval_service.py  # Two-phase approval manager (request, approve, single-use authorize)
│   │   ├── command_validator.py # Strict filename and path safety validator (rejects traversal, ADS, device names)
│   │   └── confirmation.py      # Bounded, expiring confirmation token manager (thread-safe)
│   └── speech/
│       └── __init__.py          # Package marker (empty, implementation pending)
└── tests/
    ├── test_action_result.py       # 3 tests: success, failure, cancellation dataclass behavior
    ├── test_approval_service.py    # 14 tests: approval lifecycle, expiration, binding, bounds
    ├── test_command_validator.py   # 36 tests: path traversal, dangerous characters, Windows device names
    ├── test_config.py              # 4 tests: default configuration values
    ├── test_confirmation.py        # 6 tests: confirmation expiration, reuse rejection, matching
    ├── test_executor.py            # 9 tests: execution checks, approval enforcement, error masking
    ├── test_filesystem.py          # 17 tests: sandboxing, size limits, deletion, close lifecycle
    ├── test_filesystem_commands.py # 7 tests: integration of filesystem service with command registry
    ├── test_logger.py              # 2 tests: logger naming and default level
    ├── test_permissions.py         # 2 tests: internet permission toggle validation
    ├── test_registry.py            # 11 tests: command registration, duplicate rejection, argument validation
    └── test_text_assistant.py      # 8 tests: text commands, help command, rejection of text confirmation
```

---

## 2. Existing Architecture and Request Flow

Anaya uses a secure command-dispatch architecture with deliberate out-of-band human-in-the-loop approvals for sensitive operations.

### High-Level Request Pipeline

```
[ User Input (Text) ]
         │
         ▼
[ TextAssistant.process() ]
         │
         ├─► Matches "help" / "commands" ──► Returns command listing
         ├─► Matches "confirm <id>" ───────► REJECTED (Text confirmation strictly forbidden)
         │
         ▼
[ IntentHandler.parse() ]
         │
         ├─► Checks input length (<= 4096 chars)
         ├─► Splits arguments via shlex.split()
         └─► Validates name & args against CommandRegistry
         │
         ▼ (CommandIntent: name, arguments)
[ CommandExecutor.execute() ]
         │
         ├─► Checks CommandSpec in CommandRegistry
         ├─► Runs CommandSpec.validate_arguments()
         │
         ├─► If requires_confirmation == False:
         │         │
         │         ▼
         │   [ Command Handler ] ──► Returns ActionResult(success=True/False)
         │
         └─► If requires_confirmation == True:
                   │
                   ├── Case A: No confirmation_id provided
                   │         │
                   │         ▼
                   │   [ ApprovalService.request_approval() ]
                   │         │
                   │         ▼
                   │   Returns ActionResult(success=False, "Confirmation required. Confirmation ID: <id>")
                   │
                   └── Case B: confirmation_id provided
                             │
                             ▼
                       [ ApprovalService.consume_authorization(confirmation_id, action, args) ]
                             │
                             ├─► Invalid / Expired / Mismatched / Already consumed ──► Fails execution
                             │
                             └─► Valid ──► Invokes Command Handler ──► Returns ActionResult
```

### Out-of-Band Approval Channel

A critical architectural invariant in Anaya is that **approvals cannot be performed via the text assistant interface**. 

1. When a sensitive command (such as `files.delete`) is invoked, `CommandExecutor` requests an approval token via `ApprovalService.request_approval()`.
2. A unique UUID `confirmation_id` with a 60-second expiration is generated and recorded.
3. The user/UI must interact with `ApprovalService` through a dedicated approval interface:
   - Call `approval_service.get_request(confirmation_id)` to review the exact action and arguments.
   - Call `approval_service.approve(request)` to create a single-use `_Authorization` bound to those exact arguments.
4. The execution is retried passing `confirmation_id`. `ApprovalService.consume_authorization()` consumes the token once. Any replay or argument alteration is rejected.

---

## 3. Module Responsibilities

| Module | Location | Primary Responsibility |
|---|---|---|
| `action_result` | `app/core/action_result.py` | Defines immutable `ActionResult(success, message, cancelled)` outcome container. |
| `config` | `app/core/config.py` | Defines `AppConfig` dataclass and `DEFAULT_CONFIG` with defaults for internet access, TTS (Kokoro), push-to-talk, and confirmations. |
| `logger` | `app/core/logger.py` | Produces standardized `logging.Logger` instances with custom formatting. |
| `permissions` | `app/core/permissions.py` | Checks coarse-grained capabilities (`check_internet_permission`). |
| `confirmation` | `app/security/confirmation.py` | Thread-safe, bounded, expiring one-time confirmation token store (`ConfirmationManager`). |
| `command_validator` | `app/security/command_validator.py` | Validates filenames against path traversal, Windows reserved device names, ADS, control characters, and base directory escape. |
| `approval_service` | `app/security/approval_service.py` | Two-phase approval manager (`request_approval`, `approve`, `reject`, `consume_authorization`). Bound to exact actions and arguments. |
| `registry` | `app/commands/registry.py` | Stores registered `CommandSpec` instances; provides schema validation for names and arguments. |
| `executor` | `app/commands/executor.py` | Validates commands and arguments, enforces approval policies, isolates handler failures. |
| `filesystem` | `app/commands/filesystem.py` | Confined filesystem operations (`list_files`, `read_text`, `create_text`, `delete_file`) constrained to pre-authorized directories. Supports POSIX `dir_fd` descriptor isolation. |
| `filesystem_commands` | `app/commands/filesystem_commands.py` | Registers `files.list`, `files.read`, `files.create`, and `files.delete` into `CommandRegistry`. |
| `intent_handler` | `app/brain/intent_handler.py` | Safely tokenizes command input using `shlex` and verifies syntax against registered command specifications. |
| `text_assistant` | `app/brain/text_assistant.py` | Dispatches user text to `IntentHandler` and `CommandExecutor`; enforces rejection of in-band text approvals. |
| `speech` | `app/speech/__init__.py` | Package exists but currently empty (0 bytes). Speech modules not yet implemented. |

---

## 4. Dependency Relationships

```
                     ┌───────────────────────┐
                     │   TextAssistant       │
                     └──────────┬────────────┘
                                │
               ┌────────────────┴───────────────┐
               ▼                                ▼
     ┌──────────────────┐             ┌──────────────────┐
     │  IntentHandler   │             │ CommandExecutor  │
     └─────────┬────────┘             └─────────┬────────┘
               │                                │
               ▼                                ▼
     ┌──────────────────┐             ┌──────────────────┐
     │ CommandRegistry  │◄────────────┤ ApprovalService  │
     └─────────▲────────┘             └─────────┬────────┘
               │                                │
               │ (registers specs)              ▼
     ┌─────────┴────────────┐         ┌──────────────────┐
     │ filesystem_commands  │         │  Confirmation-   │
     └─────────┬────────────┘         │     Manager      │
               │                      └──────────────────┘
               ▼
     ┌──────────────────┐             ┌──────────────────┐
     │FilesystemService ├────────────►│command_validator │
     └──────────────────┘             └──────────────────┘
```

### Direct Internal Dependencies
- `app.brain.text_assistant` -> `app.brain.intent_handler`, `app.commands.executor`, `app.commands.registry`, `app.core.action_result`
- `app.brain.intent_handler` -> `app.commands.registry`
- `app.commands.executor` -> `app.commands.registry`, `app.core.action_result`, `app.security.approval_service`
- `app.commands.filesystem_commands` -> `app.commands.filesystem`, `app.commands.registry`
- `app.commands.filesystem` -> `app.security.command_validator`
- `app.security.approval_service` -> `app.security.confirmation`
- `app.core.permissions` -> standalone
- `app.core.config` -> standalone
- `app.core.logger` -> standalone
- `app.core.action_result` -> standalone

Notice the clean layered separation: `core` and `security` have zero dependencies on `commands` or `brain`. `commands` depends only on `security` and `core`. `brain` depends on `commands` and `core`.

---

## 5. Existing Functionality

1. **Deterministic Command Dispatch**: Registering commands with strict name syntax, human-readable descriptions, custom argument validators, and optional confirmation flags.
2. **Strict Text Tokenization**: Safely splitting commands up to 4096 characters using `shlex` without shell injection risks.
3. **Hardened Local Filesystem Operations**:
   - `files.list <dir_index>`: Enumerate regular files in an allowed directory.
   - `files.read <dir_index> <filename>`: Read UTF-8 text file up to 1 MB.
   - `files.create <dir_index> <filename> <content>`: Create text file with exclusive creation (`O_EXCL`), preventing accidental overwrites.
   - `files.delete <dir_index> <filename>`: Delete file with required confirmation (`requires_confirmation=True`).
4. **Sandboxed Path Validation**: Protection against directory traversal (`..`), path separators (`/`, `\`), null bytes, Windows alternate data streams (`:`), Windows reserved device names (`CON`, `PRN`, `AUX`, `NUL`, `COM1-9`, `LPT1-9`), and forbidden system directories (`/etc`, `/usr`, `C:\Windows`, `Program Files`, etc.).
5. **Two-Phase Bounded Human Approval Engine**: Generating UUID confirmation requests with 60-second timeouts, bound strictly to exact action names and argument tuples.
6. **In-Band Approval Prevention**: `TextAssistant` actively rejects `confirm <id>` strings in conversation, ensuring approvals are only made via trusted interfaces.
7. **Configurable Permissions**: Base configuration for internet access (`internet_enabled=False` by default) and push-to-talk settings.

---

## 6. Missing or Incomplete Functionality

1. **Dependency Manifest**: No `requirements.txt` or `pyproject.toml` file exists in the repository. (Dependencies are installed inside `.venv312win`, but not formally declared).
2. **Speech Layer (`app/speech/`)**: Completely empty (`__init__.py` is 0 bytes). The packages `kokoro`, `soundfile`, `torch`, and `misaki` are present in `.venv312win`, and `config.py` specifies Kokoro TTS parameters, but no STT, TTS, or audio I/O code exists in `app/`.
3. **Natural Language / LLM Intelligence**: `IntentHandler` is strictly keyword/shlex-based. There is no LLM, semantic parsing, or flexible conversational brain yet.
4. **Memory Engine (`app/memory/`)**: Completely absent. There is currently no persistent memory, session tracking, episodic memory, or vector database.
5. **Skills Framework (`app/skills/`)**: Completely absent. There is no plugin or extensible skill loading mechanism beyond direct Python calls to `registry.register()`.
6. **User Interface / Main Entrypoint**: No `main.py`, desktop GUI, CLI interactive loop, or API server exists. The code currently consists of libraries instantiated directly in tests.
7. **Permission Enforcement Integration**: While `check_internet_permission()` exists in `app/core/permissions.py`, it is not hooked into `CommandExecutor` or any command handler.
8. **Asynchronous / Streaming Execution**: `CommandExecutor` and `ActionResult` only support synchronous, blocking operations.

---

## 7. Test Results

### Test Execution Details
- **Test Runner:** `pytest 9.1.1` (Python 3.12.10 via `.venv312win`)
- **Total Tests Discovered:** 119
- **Passed:** 114
- **Skipped:** 5
- **Failed / Errors:** 0
- **Duration:** 0.30 seconds

```
tests/test_action_result.py ........ PASSED (3/3)
tests/test_approval_service.py ..... PASSED (14/14)
tests/test_command_validator.py .... PASSED (35/36), SKIPPED (1/36)
tests/test_config.py ............... PASSED (4/4)
tests/test_confirmation.py ......... PASSED (6/6)
tests/test_executor.py ............. PASSED (9/9)
tests/test_filesystem.py ........... PASSED (13/17), SKIPPED (4/17)
tests/test_filesystem_commands.py .. PASSED (7/7)
tests/test_logger.py ............... PASSED (2/2)
tests/test_permissions.py .......... PASSED (2/2)
tests/test_registry.py ............. PASSED (11/11)
tests/test_text_assistant.py ....... PASSED (8/8)
```

### Explanation of Skipped Tests
The 5 skipped tests are:
1. `tests/test_command_validator.py::test_rejects_symlink_that_escapes_base`
2. `tests/test_filesystem.py::test_rejects_symlink_when_reading`
3. `tests/test_filesystem.py::test_rejects_symlink_when_deleting`
4. `tests/test_filesystem.py::test_rejects_internal_symlink_when_reading`
5. `tests/test_filesystem.py::test_rejects_internal_symlink_when_deleting`

All 5 tests explicitly skip when `symlink_to` raises `OSError` / `NotImplementedError` because creating symbolic links on Windows requires unprivileged Developer Mode or Administrator privileges (`SeCreateSymbolicLinkPrivilege`). The tests are written defensively to handle this environment limitation cleanly.

### Environment Requirement Note
Running tests requires `PYTHONPATH` to point to the repository root (e.g., `$env:PYTHONPATH="."`). When run without `PYTHONPATH`, Python fails to locate the `app` package during collection.

---

## 8. Technical Risks and Architectural Concerns

1. **In-Band Approval Bypass Risk**:
   - The separation between text commands and approval actions must be strictly preserved as LLM and Speech interfaces are introduced. An LLM agent or voice input must NEVER automatically execute `approval_service.approve()` without an explicit, verifiable human confirmation gesture outside the model's text generation stream.
2. **Missing Package Specification**:
   - The absence of `requirements.txt` or `pyproject.toml` means dependency versions (e.g. Kokoro, PyTorch, Pytest) are implicit to the local virtual environment. This can cause build divergence across development machines.
3. **In-Memory Volatility of Security Approvals**:
   - `ConfirmationManager` and `ApprovalService` hold state entirely in RAM (`dict` protected by `RLock`). If the application service or background worker restarts, pending approval requests are wiped.
4. **Filesystem Concurrency / Race Conditions**:
   - As documented in `docs/security.md`, on non-POSIX platforms (Windows), `dir_fd` descriptor-relative operations (`os.open` with `dir_fd`, `os.unlink` with `dir_fd`) are unavailable. File existence checks and subsequent read/unlink operations are non-atomic, requiring the authorized directory to be trusted against concurrent local modifications.
5. **Synchronous Execution Model**:
   - Long-running commands (e.g., Kokoro voice synthesis, LLM token streaming, web fetching) will block the caller if executed synchronously in `CommandExecutor.execute()`.

---

## 9. Recommended Development Sequence

To proceed in subsequent phases while strictly maintaining stability and security invariants:

```
Phase 1: Project & Test Environment Standardization
  ├── Create requirements.txt / pyproject.toml reflecting pinned virtualenv dependencies
  └── Configure pytest / conftest.py so PYTHONPATH is automatically resolved

Phase 2: Core Speech Interface (app/speech/)
  ├── Implement Kokoro TTS engine wrapper (offline, local voice synthesis)
  ├── Implement audio playback service (non-blocking sound output)
  ├── Implement Speech-to-Text (STT) interface with push-to-talk toggle
  └── Ensure voice input routes through TextAssistant without approval bypasses

Phase 3: Memory Engine Foundation (app/memory/)
  ├── Implement SessionMemory (ephemeral conversation history)
  ├── Implement PersistentMemory / Store (local SQLite / key-value store)
  └── Implement Retrieval / Context Provider for brain enrichment

Phase 4: Skills Framework & Extensibility (app/skills/)
  ├── Define SkillBase and SkillMetadata abstractions
  ├── Implement SkillRegistry adapter that safely registers CommandSpec instances into CommandRegistry
  └── Enforce capability security policies (e.g. internet permission check, confirmation flags)

Phase 5: Brain & Semantic Intent Routing (app/brain/)
  ├── Implement LLM conversational orchestrator
  ├── Implement fallback from natural language queries to registered skills/commands
  └── Maintain strict isolation: model output cannot execute sensitive commands without triggering ApprovalService

Phase 6: Desktop Interface / UI
  ├── Build system tray / floating desktop interface
  └── Implement dedicated, visual Approval Dialog window for ApprovalRequest approvals/rejections
```

---

## 10. Proposed Interfaces for Future Memory and Skills Integration

### 10.1 Future Memory Engine Interface (`app/memory/`)

The Memory Engine must serve as an information retrieval and persistence layer, completely decoupled from command execution. It must not have permissions to execute commands or approve actions.

```python
# Proposed: app/memory/base.py (Conceptual - NOT IMPLEMENTED)

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any

@dataclass(frozen=True)
class MemoryItem:
    key: str
    content: str
    category: str  # e.g., "preference", "fact", "conversation_turn"
    timestamp: datetime
    metadata: dict[str, Any]

class MemoryEngine(ABC):
    """Abstract interface for local memory persistence and retrieval."""

    @abstractmethod
    def store(self, item: MemoryItem) -> None:
        """Store a memory item locally."""
        pass

    @abstractmethod
    def retrieve(self, query: str, limit: int = 5) -> tuple[MemoryItem, ...]:
        """Retrieve relevant memory items for context injection."""
        pass

    @abstractmethod
    def delete(self, key: str) -> bool:
        """Remove a memory item."""
        pass
```

### 10.2 Future Skills Framework Interface (`app/skills/`)

Skills must integrate cleanly with the existing `CommandRegistry` rather than inventing an alternative execution pipeline. Every skill must declare its commands as `CommandSpec` definitions, specifying argument validators and whether human confirmation is mandatory.

```python
# Proposed: app/skills/base.py (Conceptual - NOT IMPLEMENTED)

from abc import ABC, abstractmethod
from app.commands.registry import CommandRegistry, CommandSpec

class Skill(ABC):
    """Abstract base class for modular Anaya skills."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique identifier for the skill."""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description of what this skill provides."""
        pass

    @abstractmethod
    def get_commands(self) -> tuple[CommandSpec, ...]:
        """Return CommandSpec definitions to be registered.
        
        Security constraints:
        - Sensitive actions MUST set requires_confirmation=True.
        - Internet-dependent handlers MUST verify check_internet_permission().
        - Filename/path parameters MUST use app.security.command_validator.
        """
        pass

    def register(self, registry: CommandRegistry) -> None:
        """Register all commands provided by this skill into the central registry."""
        for command in self.get_commands():
            registry.register(command)
```

### Invariant Checklist for Memory and Skills
- [x] Memory is read/write data only; never executes code or triggers approvals.
- [x] Skills expose functionality exclusively via `CommandSpec` through `CommandRegistry`.
- [x] Commands requiring user confirmation cannot be approved programmatically by Skills or Memory.
- [x] Internet usage within skills must respect `check_internet_permission()`.
