# Anaya — Project Status and Roadmap

**Document Version:** 1.2.0\
**Repository State:** Post-Phase 10 Completion\
**Latest Release Commit:** `d7b223a`\
**Branch:** `master` (synchronized with `origin/master`)\
**Latest Verification:** 1,158 passed, 15 skipped, 0 failed (1,173 total items)

---

## 1. Project Vision

Anaya is an open-source, local-first personal AI assistant engineered for desktop operating systems. The project provides an autonomous, privacy-respecting companion capable of voice and text interaction, persistent knowledge retention, reliable task scheduling with desktop reminders, modular extensibility through sandboxed external skills, and private LLM inference via Ollama.

### Core Distinctions
- **Zero Cloud Dependence:** Complete offline operation without telemetry, remote API keys, or data harvesting.
- **Deterministic Security Barrier:** Strict boundary separating generative text from deterministic command execution and approval consumption.
- **Multi-Layered Sandbox Isolation:** External skills execute strictly out-of-process in isolated Windows AppContainers under Job Object resource limits, with handle-relative filesystem traversal, read-only staging, and zero network access.
- **Lightweight & Efficient:** Written in pure, modern Python (3.12+) with SQLite backing, minimal dependencies, and push-to-talk bounded audio.

---

## 2. Completed Architecture Roadmap (Phases 0–10)

The foundational architecture through desktop integration and out-of-process skill sandboxing has been completely built, hardened, verified, and merged into `master`.

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
| **9** | **Desktop Native Services** | `WindowsTrayService`, `WindowsToastNotificationHandler`, `WindowsGlobalHotkeyService`, floating assistant HUD, and desktop coordinator integration. | **Completed & Verified** |
| **10** | **Out-of-Process Skill Sandbox** | Isolated worker IPC (10.1), process supervisor (10.2), Job Objects (10.3.1), AppContainer infrastructure (10.3.2), process launcher (10.3.3), manifest parser (10.4.1), filesystem discovery (10.4.2), secure staging & execution (10.4.3), and dynamic refresh (10.4.4). | **Completed & Verified** |

---

## 3. Detailed Release History (Phases 8–10)

### Phase 8 — Production Integration & E2E Validation (`3026d3d`)
1. **Application Bootstrap & Controller (`2f16fac`)**:
   - `app/bootstrap.py`: Unified dependency injection ensuring single authoritative `ApprovalService`, lazy AI connections, and zero standing audio threads.
   - `app/controller.py`: Unified `AssistantController` routing text, voice, and audio inputs strictly through `TextAssistant`.
2. **Text CLI Entry Point (`23db757`)**:
   - `app/main.py`: Interactive REPL with clean exit (`exit`, `quit`, `:q`), EOF (`Ctrl+D`), and sanitized error outputs.
3. **E2E Infrastructure & Coverage (`47b74f4`, `d997985`, `fede601`, `835bcf6`, `3026d3d`)**:
   - Deterministic test harness with `FakeClock` and `RecordingNotificationHandler`.
   - Scheduler worker hang fix preserving `TaskStore` during delayed termination.
   - Fail-closed capability gates in `SkillExecutor` and configuration immutability via `FrozenDict`.

### Phase 9 — Desktop Native Services (`17e0c5b`)
1. **Windows System Tray & Notifications (`d2787b2`, `a48f991`)**:
   - `WindowsTrayService`: Background tray icon with contextual menus, KB131501 focus handling, Explorer restart recovery, and thread-safe dispatch.
   - `WindowsToastNotificationHandler`: Native Windows Action Center toasts with thread-safe queueing.
2. **Global Hotkey & Floating HUD (`7bf2a5c`, `39895cc`, `be8b87b`)**:
   - `WindowsGlobalHotkeyService`: Low-level keyboard hook registering push-to-talk hotkeys with serialized toggle processing.
   - `FloatingAssistantHUD`: Non-blocking floating status widget displaying transcription, AI thinking, and approval prompts.
3. **Desktop Coordinator & Lifecycle (`e7f5605`, `6696075`, `17e0c5b`)**:
   - Unified desktop service lifecycle with clean startup sequencing, graceful degradation in headless environments, and audio pipeline routing.

### Phase 10 — Out-of-Process Skill Sandbox & Security Isolation (`d7b223a`)
1. **Subphase 10.1 — Isolated Worker IPC Protocol (`5b772d0`)**:
   - `app/skills/protocol.py`: Strict, versioned JSON IPC wire protocol. Custom decoder rejecting duplicate keys (`DuplicateKeyError`), `NaN`, and `Infinity`. Message sizes strictly bounded to `MAX_MESSAGE_BYTES = 1_048_576` (1 MiB).
2. **Subphase 10.2 — Isolated Skill Process Supervisor (`566dd0a`)**:
   - `app/skills/supervisor.py`: Concurrency bounding via `threading.Semaphore`, request deduplication, session lifecycle tracking, and graceful two-stage process termination.
3. **Subphase 10.3.1 — Windows Job Object Resource Controls (`0b0585f`)**:
   - `app/skills/sandbox/win32_job.py`: Job Object resource limits enforcing `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, `JOB_OBJECT_LIMIT_ACTIVE_PROCESS (1)`, `JOB_OBJECT_LIMIT_PROCESS_MEMORY (256 MiB)`, and `JOB_OBJECT_LIMIT_JOB_MEMORY (512 MiB)`.
4. **Subphase 10.3.2 — Windows AppContainer Infrastructure (`ae4655f`)**:
   - `app/skills/sandbox/appcontainer.py` & `runtime_provisioner.py`: AppContainer profile creation, SID derivation, per-execution scratch directory provisioning, and session quarantine on unconfirmed process termination.
5. **Subphase 10.3.3 — Sandboxed Process Launcher (`273f483`)**:
   - `app/skills/sandbox/process_launcher.py`: Launches workers suspended (`CREATE_SUSPENDED | EXTENDED_STARTUPINFO_PRESENT`), attaches Job Objects and security capabilities before resume, and verifies handle-relative process metadata.
6. **Subphase 10.4.1 — External Skill Manifest Parser (`5396f12`)**:
   - `app/skills/manifest.py`: Strict JSON schema validation, semver parsing, entrypoint module syntax validation, and input field specification bounds. Manifest size strictly capped at `MAX_MANIFEST_BYTES = 65_536` (64 KiB).
7. **Subphase 10.4.2 — External Skill Filesystem Discovery (`a484573`)**:
   - `app/skills/discovery.py`: Handle-anchored directory scanning via `NtCreateFile` (`_open_verified_root_windows`), virtual MS-DOS substitution drive (`subst`) rejection, reparse-point rejection, descriptor-relative manifest reading, and `ExternalSkillProxy` construction.
8. **Subphase 10.4.3 — Secure Package Staging & Sandboxed Execution (`aef5741`)**:
   - `app/skills/staging.py`: Immutable snapshot copying into session scratch, pre/post read `fstat` mutation detection, module shadowing rejection (`sys`, `os`, `app`, etc.), and read-only DACL enforcement.
   - Remediated supervisor to completely eliminate unsandboxed fallback execution; non-sandboxed external execution fails closed immediately.
9. **Subphase 10.4.4 — Dynamic Skill Refresh & Application Integration (`d7b223a`)**:
   - `app/skills/skill_manager.py` & `skill_registry.py`: Dynamic `refresh_skills()` with delta tracking (`added`, `updated`, `removed`, `unmodified`).
   - Retains previously registered last-known-good proxies on malformed updates; preserves the entire registry on root-level access failures.
   - Synchronous discovery during bootstrap, `skills.refresh` CLI command, and natural language intent routing.

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
| **Windows System Tray Service** | **Complete** | **Verified (Unit & Mock)** | Background tray icon with focus handling and restart recovery. |
| **Windows Toast Notifications** | **Complete** | **Verified (Unit & Mock)** | Action Center toast delivery with thread-safe queueing. |
| **Windows Global Hotkey Service** | **Complete** | **Verified (Unit & Mock)** | Low-level keyboard hook for push-to-talk capture. |
| **Floating Assistant HUD** | **Complete** | **Verified (Unit & Mock)** | Non-blocking floating status window. |
| **Isolated Worker IPC Protocol** | **Complete** | **Verified (Unit & Integ)** | Versioned JSON protocol, 1 MiB message limit, duplicate key rejection. |
| **Skill Process Supervisor** | **Complete** | **Verified (Unit & Integ)** | Concurrency bounding, request deduplication, session quarantine. |
| **Windows Job Object Limits** | **Complete** | **Verified (Unit & Integ)** | Process/job memory caps (256/512 MiB), kill-on-close. |
| **Windows AppContainer Sandbox** | **Complete** | **Verified (Unit & Integ)** | Zero network capability, isolated per-session scratch ACLs. |
| **External Skill Manifest Parser** | **Complete** | **Verified (Unit)** | Strict schema, semver, input validation, 64 KiB manifest limit. |
| **External Skill Discovery** | **Complete** | **Verified (Unit & Integ)** | NtCreateFile handle-relative traversal, reparse point rejection. |
| **Secure Package Staging** | **Complete** | **Verified (Unit & Integ)** | Snapshot copy, mutation detection, read-only DACL enforcement. |
| **Dynamic Skill Refresh** | **Complete** | **Verified (Unit & E2E)** | Delta tracking, retained last-known-good, synchronous bootstrap scan. |
| **Cross-Platform Sandbox Gate** | **Complete** | **Verified (Unit & Integ)** | Windows: AppContainer sandbox. Linux/macOS: fails closed. |

---

## 5. Test Verification Results

### Test Suite Summary
- **Total Test Items:** **1,173**
- **Passed:** **1,158**
- **Skipped:** **15**
- **Failed:** **0**
- **Execution Time:** ~65.0 seconds

### Skipped Tests Breakdown (15 Total)
1. **Symbolic Link Creation Privileges (12 tests)**:
   - *Tests:* `test_e2e_security.py` (1), `test_command_validator.py` (1), `test_filesystem.py` (4), `test_sandbox_session.py` (1), `test_skill_discovery.py` (2), `test_skill_staging.py` (2), `test_skill_staging.py` (1 POSIX-specific).
   - *Reason:* Windows requires `SeCreateSymbolicLinkPrivilege` or Developer Mode to create symbolic links (`[WinError 1314]`). In unprivileged environments, these tests detect the limitation and skip with an informative alert.
2. **Network Isolation Policy Timeouts (3 tests)**:
   - *Tests:* `test_sandbox_launcher.py` (loopback, private network, DNS resolution).
   - *Reason:* On certain Windows host configurations, firewall policies drop AppContainer network packets resulting in connection timeouts rather than immediate `WSAEACCES` denials. The test suite explicitly distinguishes socket timeouts from policy assertions to avoid inconclusive results.

---

## 6. Known Limitations & Technical Debt

1. **Platform-Specific Sandboxing (Windows AppContainer):**
   - The out-of-process skill sandbox is fully implemented for Windows 10/11 (build >= 19041) leveraging native AppContainer profiles and Win32 Job Objects.
   - *Limitation:* Linux and macOS lack equivalent AppContainer APIs. On Linux and macOS, external skill execution currently fails closed (`platform_unavailable` / `sandbox_unavailable`).
   - *Remediation:* Implement a Linux sandbox backend using `seccomp`, `unshare` (user/mount/network namespaces), and `cgroups` in a future cross-platform expansion.
2. **Single Background Scheduler Worker Thread:**
   - Reminders are polled and dispatched sequentially by a single background thread.
   - *Limitation:* If a notification handler blocks, subsequent reminder evaluations are delayed.
   - *Remediation:* Offload notification dispatching to a concurrent worker pool (`ThreadPoolExecutor`).
3. **Advanced Recurring Tasks:**
   - Recurring tasks currently rely on basic interval or daily triggers.
   - *Limitation:* Complex RRULE patterns (e.g., "every second Tuesday of the month", "weekdays only excluding holidays") are not yet supported.
   - *Remediation:* Integrate RFC 5545 recurrence rules (planned for Phase 11).
4. **Natural Language Parser Phrasing Breadth:**
   - `NaturalLanguageParser` uses regular expressions and heuristic token extraction.
   - *Limitation:* Highly colloquial or compound requests that fall outside established regexes fail to extract structured arguments and drop through to conversational LLM fallback.

---

## 7. Verified Resource & Staging Limits

To ensure system stability and prevent denial-of-service, Phase 10 enforces verified constants across IPC, staging, and execution:

| Scope | Constant Name | Implemented Value | Location | Description |
| :--- | :--- | :---: | :--- | :--- |
| **IPC Message** | `MAX_MESSAGE_BYTES` | `1_048_576` (1 MiB) | `app/skills/protocol.py` | Maximum size for JSON requests and responses over stdin/stdout. |
| **Manifest** | `MAX_MANIFEST_BYTES` | `65_536` (64 KiB) | `app/skills/manifest.py` | Maximum file size for `skill.json` manifest. |
| **Package File** | `MAX_SOURCE_FILE_BYTES` | `262_144` (256 KiB) | `app/skills/staging.py` | Maximum size for an individual source file in an external package. |
| **Total Package** | `MAX_TOTAL_PACKAGE_BYTES`| `2_097_152` (2 MiB) | `app/skills/staging.py` | Maximum cumulative size of all files in an external skill package. |
| **File Count** | `MAX_STAGED_FILES` | `50` | `app/skills/staging.py` | Maximum number of files permitted in a single package. |
| **Dir Depth** | `MAX_STAGING_DEPTH` | `3` | `app/skills/staging.py` | Maximum directory hierarchy depth within an external package. |
| **Package Limit** | `MAX_PACKAGE_DIRECTORIES`| `100` | `app/skills/discovery.py` | Maximum external package directories scanned under skills root. |
| **Stderr Log** | `MAX_STDERR_BYTES` | `65_536` (64 KiB) | `app/skills/supervisor.py` | Maximum diagnostic error output collected from worker stderr. |
| **Worker RAM** | `process_memory_limit_bytes` | `268_435_456` (256 MiB) | `app/skills/sandbox/win32_job.py` | Per-process committed memory ceiling enforced by Job Object. |
| **Job RAM** | `job_memory_limit_bytes` | `536_870_912` (512 MiB) | `app/skills/sandbox/win32_job.py` | Total aggregate memory ceiling enforced by Job Object. |
| **Active Procs** | `active_process_limit` | `1` | `app/skills/sandbox/win32_job.py` | Blocks worker from spawning unauthorized child processes. |
| **Concurrency** | `DEFAULT_MAX_CONCURRENT_WORKERS` | `4` | `app/skills/supervisor.py` | Maximum simultaneous worker processes managed by supervisor. |

---

## 8. Proposed Future Roadmap (Phase 11: Proposed Only, Not Approved or Implemented)

> [!IMPORTANT]
> The following phase represents **proposed future architectural concepts**. It is **not approved, not scheduled, and not implemented**. All authorized development to date concluded with the completion, verification, and closure of Phase 10 (`d7b223a`).

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

## 9. Dependencies & Environmental Risks

1. **Local Ollama Daemon Availability:**
   - While Anaya functions gracefully in text-only command mode without Ollama, conversational fallback and natural language Q&A depend on a locally running Ollama instance (`localhost:11434`).
2. **Audio Hardware & Headless Environments:**
   - In containerized, headless CI, or remote SSH environments, audio capture/playback devices may be absent. The codebase handles this via mock adapters and graceful feature degradation (`PipelineResult(success=False)`), but full voice validation requires physical desktop testing.
3. **Python 3.12+ Compatibility:**
   - The codebase strictly leverages modern Python 3.12 typing (`str | None`, `frozenset`, frozen dataclasses). Older Python versions are not supported.
