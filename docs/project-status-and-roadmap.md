# Anaya — Project Status and Roadmap

**Document Version:** 1.0.0  
**Repository State:** Post-Phase 8B Completion  
**Latest Release Commit:** `3026d3d`  
**Branch:** `master` (synchronized with `origin/master`)  
**Latest Verification:** 535 passed, 6 skipped, 0 failed (541 total items)  

---

## 1. Project Vision

Anaya is an open-source, local-first personal AI assistant engineered for desktop operating systems. The project provides an autonomous, privacy-respecting companion capable of voice and text interaction, persistent knowledge retention, reliable task scheduling with desktop reminders, modular extensibility through skills, and private LLM inference via Ollama.

### Core Distinctions
- **Zero Cloud Dependence:** Complete offline operation without telemetry, remote API keys, or data harvesting.
- **Deterministic Security Barrier:** Strict boundary separating generative text from deterministic command execution and approval consumption.
- **Lightweight & Efficient:** Written in pure, modern Python (3.12+) with SQLite backing, minimal dependencies, and push-to-talk bounded audio.

---

## 2. Completed Original Roadmap (Phases 0–8)

The initial architectural roadmap of Anaya has been completely built, hardened, verified, and merged into `master`.

| Phase | Milestone | Primary Capabilities Delivered | Status |
| :---: | :--- | :--- | :---: |
| **0** | **Security & Architecture Audit** | Allowlisted `CommandRegistry`, `CommandExecutor`, `ApprovalService`, `FilesystemService` path sandboxing, and confirmation token lifecycle. | **Completed & Verified** |
| **1** | **Natural Language Brain** | `NaturalLanguageParser`, `IntentHandler`, `ConversationContext` for multi-turn disambiguation, and `TextAssistant` security barrier. | **Completed & Verified** |
| **2** | **Persistent Memory Engine** | SQLite `MemoryStore` with WAL mode, FTS5 full-text search with automatic `LIKE` fallback, and high-level `MemoryManager` facade. | **Completed & Verified** |
| **3** | **Modular Skills Framework** | `SkillRegistry`, `SkillExecutor`, `SkillManager`, `InputFieldSpec` contract validation, and `example.greeting` built-in skill. | **Completed & Verified** |
| **4** | **Speech & Audio Engine** | Push-to-talk `VoicePipeline`, SoundDevice I/O adapters, Whisper.cpp STT transcription, Kokoro-82M TTS synthesis, and mock adapters. | **Completed & Verified** |
| **5** | **Task Management & Scheduling** | SQLite `TaskStore` with WAL mode, `TaskManager` domain service, `TaskDateParser` natural language dates, and `TaskScheduler` worker loop. | **Completed & Verified** |
| **6** | **Local AI Model Switcher** | `ModelManager`, `HardwareProfile` capability detection, `ModelRegistry`, and `OllamaProvider` with lazy connection and context tracking. | **Completed & Verified** |
| **7B** | **Runtime & Reminder Integration** | `AnayaRuntime` coordinator, notification interfaces, transactional at-most-once claims, startup missed-reminder recovery, and worker safety. | **Completed & Verified** |
| **8A** | **Controller & Application Entry** | `AssistantController` unified facade, dependency-injected `app/bootstrap.py`, and interactive text REPL in `app/main.py`. | **Completed & Verified** |
| **8B** | **End-to-End System Validation** | Deterministic E2E test harness (`tests/test_e2e/`), CLI tests, persistence tests, scheduler tests, security tests, and lifecycle fixes. | **Completed & Verified** |

---

## 3. Phase 8A and 8B Detailed Release History

Phase 8 established production-grade application integration and rigorous end-to-end verification.

```text
3026d3d (HEAD -> master, origin/master) Add scheduler and security E2E coverage
d997985 Add CLI and persistence E2E coverage
835bcf6 Enforce fail-closed skill capability permissions
fede601 Fix controller and scheduler shutdown lifecycle
47b74f4 Add E2E test infrastructure and fixtures
23db757 Implement text-based CLI application entry point
2f16fac Implement assistant controller and application bootstrap
```

### Key Milestones Delivered in Phase 8

1. **Step 8A.1 — Application Bootstrap & AssistantController (`2f16fac`)**:
   - `app/bootstrap.py`: Constructs exactly one shared instance of each stateful subsystem. Ensures a single authoritative `ApprovalService` across commands and skills. Guarantees zero standing audio threads or eager model pulls.
   - `app/controller.py`: Exposes a unified interface (`handle_text`, `handle_voice`, `handle_audio`, `speak`, `start`, `stop`) routing all inputs strictly through `TextAssistant`.
2. **Step 8A.2 — Text CLI Entry Point (`23db757`)**:
   - `app/main.py`: Interactive REPL supporting clean exit commands (`exit`, `quit`, `:q`), EOF (`Ctrl+D`), interrupt (`Ctrl+C`), and sanitized error reporting without secret leakage.
3. **Step 8B.2A — E2E Test Infrastructure (`47b74f4`)**:
   - `tests/test_e2e/conftest.py`: Introduced `FakeClock` for deterministic time travel, `RecordingNotificationHandler` for event-synchronized reminder assertions, and `e2e_controller_factory` for automated reverse-order cleanup.
4. **Step 8B.2B & 2C — CLI & Persistence E2E Coverage (`d997985`)**:
   - `tests/test_e2e/test_e2e_cli.py`: 14 tests validating REPL lifecycle, input pipelines, error sanitization, text-only mode invariants, and AI boundary containment.
   - `tests/test_e2e/test_e2e_persistence.py`: 6 tests verifying tasks and memories across controller restarts, state transitions, binary serialization survival, and database path isolation.
5. **Step 8B.2D — Scheduler E2E Coverage & Worker Hang Fix (`fede601`, `3026d3d`)**:
   - Fixed a shutdown defect where hung scheduler worker threads left `TaskStore` open but marked the controller cleanly stopped.
   - `AssistantController.stop()` now defers setting `_cleanly_stopped = True` until the worker thread actually terminates, preserving `TaskStore` open safely and allowing repeated shutdown calls to finish cleanup.
   - `tests/test_e2e/test_e2e_scheduler.py`: 9 tests verifying due reminder delivery, timing, at-most-once deduplication, missed reminder recovery on startup, and worker hang handling.
6. **Step 8B.2E — Security E2E Coverage, Capability Gates & Immutability (`835bcf6`, `3026d3d`)**:
   - Resolved an audit defect where declared skill permissions (`READ_ONLY`, `FILESYSTEM`) were not checked by `SkillExecutor`.
   - Implemented fail-closed capability gates in `SkillExecutor` evaluated prior to approval requests or code invocation.
   - Added `FrozenDict` to `app/core/config.py` to recursively freeze `skill_permission_grants`, blocking in-place `|=` mutation while preserving `dataclasses.asdict()`, `json.dumps()`, and `copy.deepcopy()` compatibility.
   - `tests/test_e2e/test_e2e_security.py`: 13 tests verifying invalid command injection, single-use approval tokens, path traversal sandboxing, AI output isolation, fail-closed capability gating, and configuration immutability.

---

## 4. Current Implementation Status Matrix

| Subsystem / Feature | Implementation Status | Verification Status | Notes |
| :--- | :---: | :---: | :--- |
| **Command Allowlisting & Dispatch** | **Complete** | **Verified (Unit & E2E)** | Strict arity and type checking via `CommandRegistry`. |
| **Out-of-Band Action Approvals** | **Complete** | **Verified (Unit & E2E)** | Single-use expiring tokens bound to action and arguments. |
| **Filesystem Sandboxing** | **Complete** | **Verified (Unit & E2E)** | Path traversal (`..`) blocked; operations confined to root. |
| **Filesystem Symlink Containment** | **Complete** | **Verified (CI/Linux)** | Verified logic; skipped on Windows without Developer Mode. |
| **Natural Language Parser** | **Complete** | **Verified (Unit & E2E)** | Regex and heuristic intent parsing. |
| **Multi-Turn Disambiguation** | **Complete** | **Verified (Unit & E2E)** | Managed via `ConversationContext`. |
| **Brain Security Barrier** | **Complete** | **Verified (Unit & E2E)** | Blocks conversational confirmations (`yes`, `confirm`). |
| **AI Output Isolation** | **Complete** | **Verified (Unit & E2E)** | Model outputs cannot invoke commands or authorize actions. |
| **SQLite Memory Engine** | **Complete** | **Verified (Unit & E2E)** | WAL mode, FTS5 search with automatic `LIKE` fallback. |
| **Skills Capability Gating** | **Complete** | **Verified (Unit & E2E)** | Evaluated fail-closed prior to execution or approval. |
| **Configuration Immutability** | **Complete** | **Verified (Unit & E2E)** | `FrozenDict` blocks mutation methods including `|=`. |
| **Push-to-Talk Voice Pipeline** | **Complete** | **Verified (Unit & Mock)** | Zero standing background audio threads. |
| **Whisper.cpp STT Adapter** | **Complete** | **Environment-Dependent** | Uses CLI/C-bindings; verified via unit mocks. |
| **Kokoro-82M TTS Adapter** | **Complete** | **Environment-Dependent** | Uses local ONNX model; verified via unit mocks. |
| **SQLite Task Management** | **Complete** | **Verified (Unit & E2E)** | Full CRUD, status transitions, priorities. |
| **Task Date Parser** | **Complete** | **Verified (Unit)** | Natural language date parsing (relative and absolute). |
| **Background Reminder Scheduler** | **Complete** | **Verified (Unit & E2E)** | 15s polling worker loop with clock abstraction. |
| **At-Most-Once Reminder Claim** | **Complete** | **Verified (Unit & E2E)** | Transactional claim via `reminder_triggered = 1`. |
| **Missed Reminder Startup Recovery** | **Complete** | **Verified (Unit & E2E)** | Automatically delivers past-due reminders on startup. |
| **Local AI (Ollama)** | **Complete** | **Verified (Unit & Mock)** | Hardware profiling, dynamic model switching, lazy connection. |
| **Text REPL CLI (`main.py`)** | **Complete** | **Verified (Unit & E2E)** | Clean lifecycle, EOF handling, sanitized error reporting. |
| **AssistantController Facade** | **Complete** | **Verified (Unit & E2E)** | Unified lifecycle and subsystem routing. |

---

## 5. Test Verification Results

### Test Suite Summary
- **Total Test Items:** **541**
- **Passed:** **535**
- **Skipped:** **6**
- **Failed:** **0**
- **Execution Time:** ~10.0 seconds

### Skipped Tests Breakdown
1. **`tests/test_e2e/test_e2e_security.py::test_filesystem_symlink_containment_and_rejection`**:
   - *Reason:* Windows requires `SeCreateSymbolicLinkPrivilege` or Developer Mode to create symlinks (`[WinError 1314]`). The test detects this host limitation and skips with an informative alert.
2. **`tests/test_command_validator.py` (1 test)** & **`tests/test_filesystem.py` (4 tests)**:
   - *Reason:* Platform-specific Windows path formatting checks (e.g., POSIX-specific root checks or device path validations) that skip safely on non-POSIX hosts.

---

## 6. Known Limitations & Technical Debt

1. **In-Process Skill Execution Sandbox:**
   - Skills execute in-process within the main Python runtime.
   - *Limitation:* Permissions (`READ_ONLY`, `FILESYSTEM`) are application execution gates, not OS-level sandboxes. A malicious native extension or untrusted Python script could circumvent runtime checks.
   - *Remediation:* Migrate to out-of-process worker processes (e.g., subprocesses with restricted OS privileges or containerized microVMs).
2. **Single Background Scheduler Worker Thread:**
   - Reminders are polled and dispatched sequentially by a single background thread.
   - *Limitation:* If a notification handler (e.g., a synchronous webhook or long audio playback) blocks, subsequent reminder evaluations are delayed.
   - *Remediation:* Offload notification dispatching to a concurrent worker pool (`ThreadPoolExecutor`).
3. **Advanced Recurring Tasks:**
   - Recurring tasks currently rely on basic interval or daily triggers.
   - *Limitation:* Complex RRULE patterns (e.g., "every second Tuesday of the month", "weekdays only excluding holidays") are not yet supported.
4. **Natural Language Parser Phrasing Breadth:**
   - `NaturalLanguageParser` uses regular expressions and heuristic token extraction.
   - *Limitation:* Highly colloquial or compound requests that fall outside established regexes fail to extract structured arguments and drop through to conversational LLM fallback.

---

## 7. Proposed Future Roadmap (Phases 9–11: Proposed Only, Not Approved or Implemented)

> [!IMPORTANT]
> The following phases represent **proposed future architectural concepts**. They are **not approved, not scheduled, and not implemented**. All authorized development to date concluded with the completion and release of Phase 8B (`3026d3d`).

With the core foundational architecture (Phases 0–8B) completely verified, the following phases outline potential future enhancements for advanced desktop capabilities and extended autonomy.

### Proposed Phase 9: Desktop Integration & Native User Interface (Proposed)

- **Objective:** Provide a native, unobtrusive desktop presence beyond the terminal REPL.
- **Milestones:**
  1. **System Tray Application (`app/ui/tray.py`):**
     - Background daemon with system tray icon (Windows, macOS, Linux).
     - Global hotkey listener (e.g., `Ctrl+Space` or `Win+Space`) to trigger push-to-talk voice capture.
     - Visual state indicator (idle, listening, thinking, speaking).
  2. **Native OS Desktop Notifications (`app/notifications/desktop.py`):**
     - Native toast notification adapters replacing or complementing terminal logging (Windows Action Center, macOS Notification Center, Linux libnotify).
     - Actionable notification buttons ("Mark Complete", "Snooze 10m").
  3. **Minimalist Desktop Floating HUD / Window:**
     - Non-intrusive floating HUD widget displaying transcribed text, AI responses, and approval confirmation dialogs.

### Proposed Phase 10: Out-of-Process Skill Sandbox & Security Isolation (Proposed)

- **Objective:** Elevate skill security from application-level execution gates to true process-isolated sandboxes.
- **Milestones:**
  1. **Subprocess Skill Worker (`app/skills/worker.py`):**
     - Execute skills in isolated child processes communicating via JSON-RPC / IPC pipes.
     - Enforce process-level memory, CPU, and execution timeout limits.
  2. **Restricted OS Capabilities:**
     - Restrict child process filesystem and network access at the operating system level (Windows AppContainer or Linux seccomp/namespaces).
  3. **Dynamic Skill Loading & Package Discovery:**
     - Safe discovery and loading of user skills from `~/.anaya/skills/` without requiring restart.

### Proposed Phase 11: Advanced Calendar, Recurrence & Multi-Modal Context (Proposed)

- **Objective:** Deepen productivity capabilities with calendar standards and multi-modal awareness.
- **Milestones:**
  1. **Full RRULE & Calendar Standard (`RFC 5545`):**
     - Integrate standard recurrence rules (e.g., via `dateutil.rrule`) for enterprise-grade recurring task scheduling.
  2. **Multi-Modal Local Vision Models:**
     - Leverage Ollama vision models (e.g., `llava`, `llama3.2-vision`) for screen context queries ("What is currently on my screen?").
  3. **Local Vector Embeddings & Hybrid Search:**
     - Upgrade memory engine with local vector embeddings (via Ollama embeddings or `fastembed`) to complement FTS5 keyword search.

---

## 8. Dependencies & Environmental Risks

1. **Local Ollama Daemon Availability:**
   - While Anaya functions gracefully in text-only command mode without Ollama, conversational fallback and natural language Q&A depend on a locally running Ollama instance (`localhost:11434`).
2. **Audio Hardware & Headless Environments:**
   - In containerized, headless CI, or remote SSH environments, audio capture/playback devices may be absent. The codebase handles this via mock adapters and graceful feature degradation (`PipelineResult(success=False)`), but full voice validation requires physical desktop testing.
3. **Python 3.12+ Compatibility:**
   - The codebase strictly leverages modern Python 3.12 typing (`str | None`, `frozenset`, frozen dataclasses). Older Python versions are not supported.
