# Anaya Task Manager & Scheduler — Phase 6 Documentation

## 1. Architecture Overview

The Anaya Task Manager is a lightweight, local-first, privacy-conscious task and reminder system designed for offline personal assistance. Built with zero external dependencies (Python stdlib only), it follows the established architectural principles of the Anaya ecosystem: deterministic parsing, strict validation, defense-in-depth security, and persistent SQLite storage.

```
+-------------------------------------------------------------+
|                      User Interface                         |
|   (CLI, VoicePipeline / Audio Pipeline, Natural Text)       |
+-------------------------------------------------------------+
                              |
                              v
+-------------------------------------------------------------+
|                    TextAssistant & Brain                    |
|   - Intent Parsing (NaturalLanguageParser)                  |
|   - Multi-turn Clarification (ConversationContext)          |
|   - Explicit Approval Intercept (Disables Spoken 'Yes')     |
+-------------------------------------------------------------+
                              |
                              v
+-------------------------------------------------------------+
|               Command Executor & Approval Service            |
|   - tasks.create, tasks.list, tasks.get, tasks.update       |
|   - tasks.complete, tasks.cancel                            |
|   - tasks.delete (Requires Explicit Out-of-Band Approval)   |
+-------------------------------------------------------------+
                              |
                              v
+-------------------------------------------------------------+
|                        TaskManager                          |
|   - Domain validation & short ID generation (8-char hex)    |
|   - Task lifecycle state machine                            |
|   - DateParser integration (due date vs reminder separation)|
+-------------------------------------------------------------+
              |                                 |
              v                                 v
+---------------------------+     +---------------------------+
|       TaskStore           |     |       TaskScheduler       |
| - SQLite (~/.anaya/tasks) |     | - In-process daemon loop  |
| - WAL mode + RLock        |     | - Atomic reminder claim   |
| - Dedicated index sets    |     | - Isolated callback dispatch
+---------------------------+     +---------------------------+
```

---

## 2. Task Lifecycle and State Transition Rules

Tasks transition strictly according to well-defined lifecycle rules. All transitions are verified before persisting to the database.

```
                    +-----------------------+
                    |        CREATED        |
                    +-----------------------+
                                |
                                v
                    +-----------------------+
          +-------->|        PENDING        |<--------+
          |         +-----------------------+         |
          |               /           \               |
          |              /             \              |
          |             v               v             |
          |   +-------------+       +-------------+   |
Reopen    |   |  COMPLETED  |       |  CANCELLED  |   | Reopen
          +---| (Terminal)  |       | (Terminal)  |---+
              +-------------+       +-------------+
                     \                     /
                      \                   /
                       v                 v
                    +-----------------------+
                    |        DELETED        |
                    | (Explicit Approval Req|
                    +-----------------------+
```

### State Definitions
- **PENDING**: The default active state for newly created tasks. Reminders are active if configured.
- **COMPLETED**: The task has been accomplished. Reminders are disarmed.
- **CANCELLED**: The task was abandoned or is no longer needed. Reminders are disarmed.
- **DELETED**: The task record is purged from persistent storage. Requires explicit out-of-band authorization.

### Transition Rules
1. `PENDING -> COMPLETED`: Permitted via `tasks.complete` or `TaskManager.complete_task()`. Sets `completed_at` timestamp.
2. `PENDING -> CANCELLED`: Permitted via `tasks.cancel` or `TaskManager.cancel_task()`.
3. `COMPLETED -> PENDING` / `CANCELLED -> PENDING`: Permitted via `tasks.update(status="pending")`. Clears `completed_at`.
4. Updating `reminder_at` on an active task resets `reminder_triggered = 0`.

---

## 3. Due Dates vs Reminders (Semantic Separation)

Anaya enforces an explicit conceptual separation between **Due Dates** and **Reminders**:

| Property | Field | Semantic Meaning | Scheduling Behavior |
| :--- | :--- | :--- | :--- |
| **Due Date** | `due_date` | The deadline by which the task should be accomplished. | Informational only. Does not trigger intrusive notifications. |
| **Reminder** | `reminder_at` | The exact wall-clock instant at which Anaya must alert the user. | Actively polled and claimed by `TaskScheduler`. Dispatches notifications. |

A task may have:
- Only a deadline (`due_date` set, `reminder_at = None`).
- Only a reminder (`reminder_at` set, `due_date = None`).
- Both a deadline and a reminder (`reminder_at` typically before `due_date`).
- Neither (a general pending to-do item).

---

## 4. Data Model & Schema Reference

### Python Dataclass (`Task`)
Located in `app/tasks/models.py`, `Task` is an immutable, frozen dataclass:

```python
@dataclass(frozen=True)
class Task:
    id: str                     # Hexadecimal identifier (4 to 16 chars)
    title: str                  # Non-empty string, <= 500 characters
    description: str | None     # Optional string, <= 5000 characters
    status: str                 # "pending", "completed", "cancelled"
    priority: str               # "low", "medium", "high", "urgent"
    due_date: datetime | None   # Timezone-aware UTC datetime
    reminder_at: datetime | None# Timezone-aware UTC datetime
    reminder_triggered: bool    # 0 or 1 in SQLite
    created_at: datetime        # Timezone-aware UTC datetime
    updated_at: datetime        # Timezone-aware UTC datetime
    completed_at: datetime | None # Timezone-aware UTC datetime
```

### SQLite Schema (`TaskStore`)
Located in `~/.anaya/tasks.db`:

```sql
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL CHECK(status IN ('pending', 'completed', 'cancelled')),
    priority TEXT NOT NULL CHECK(priority IN ('low', 'medium', 'high', 'urgent')),
    due_date TEXT,
    reminder_at TEXT,
    reminder_triggered INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_due_date ON tasks(due_date);
CREATE INDEX IF NOT EXISTS idx_tasks_reminder ON tasks(reminder_at, reminder_triggered, status);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);
```

---

## 5. ID Format, Collision Resistance, and Validation Rules

### ID Generation
- Task IDs are generated as **8-character lowercase hexadecimal strings** (32 bits of entropy derived from `secrets.token_hex(4)`).
- Provides $4.29 \times 10^9$ possibilities.
- Collision resistance: On collision against an existing task, `TaskManager` generates up to 5 alternative candidate IDs before raising `TaskStoreError`.
- Validation regex: `^[a-f0-9]{4,16}$`.

### Attribute Validation Rules
- **Title**: Required, non-empty, max 500 characters, no leading/trailing whitespace.
- **Description**: Optional, max 5000 characters if provided.
- **Priority**: Must be one of `low`, `medium`, `high`, `urgent` (case-insensitive string fallback).
- **Status**: Must be one of `pending`, `completed`, `cancelled`.
- **Datetimes**: Must be timezone-aware or ISO 8601 strings. Naive datetimes are strictly rejected to prevent UTC ambiguity.

---

## 6. Natural Language Patterns & Clarification Flows

The natural language parser (`app/brain/natural_parser.py`) maps spoken or typed instructions directly to task intents without external LLM dependencies:

### Supported Natural Language Patterns
1. **Creation with Deadline**:
   - *"Add a task to finish my assignment tomorrow"*
   - *"Create task prepare quarterly report by Friday"*
2. **Creation with Reminder**:
   - *"Remind me to call Divya at 5 PM"*
   - *"Remind me to take medication in 30 minutes"*
3. **Task Listing**:
   - *"Show my pending tasks"*
   - *"What do I need to do today?"*
   - *"Display completed tasks"*
4. **Completion**:
   - *"Complete task 1a2b3c4d"*
   - *"Mark my assignment as completed"* (Searches active tasks by title)
5. **Cancellation**:
   - *"Cancel task 1a2b3c4d"*
   - *"Cancel the Team sync task"*
6. **Deletion**:
   - *"Delete task 1a2b3c4d"*

### Multi-turn Clarification Flows
When required arguments are absent or ambiguous, the parser prompts the user and preserves context:
- **Missing Title**:
  - User: *"Add a task"*
  - Anaya: *"What task would you like to create?"*
  - Context captures `tasks.create` pending intent.
  - User: *"Buy milk and eggs"* $\rightarrow$ Creates task.
- **Ambiguous Match**:
  - User: *"Mark my assignment as completed"* (when both *Math assignment* and *Physics assignment* exist)
  - Anaya: *"Multiple matching tasks found: 'Math assignment' (ID: 01a2b3c4), 'Physics assignment' (ID: 5e6f7a8b). Which task ID would you like to mark as completed?"*

---

## 7. Command Registry Specifications

The following commands are registered in `app/commands/task_commands.py`:

| Command Name | Arguments | Validation Rules | Requires Confirmation |
| :--- | :--- | :--- | :--- |
| `tasks.create` | `(title, [due_date], [reminder_at], [priority], [description])` | Title non-empty; valid dates/priorities if present | No |
| `tasks.list` | `([status], [limit], [offset])` | Status in `{pending, completed, cancelled, all}` | No |
| `tasks.get` | `(task_id,)` | `^[a-f0-9]{4,16}$` | No |
| `tasks.update` | `(task_id, [title], [due_date], [reminder_at], [status], [priority], [description])` | Valid ID, valid fields | No |
| `tasks.complete` | `(task_id,)` | `^[a-f0-9]{4,16}$` | No |
| `tasks.cancel` | `(task_id,)` | `^[a-f0-9]{4,16}$` | No |
| `tasks.delete` | `(task_id,)` | `^[a-f0-9]{4,16}$` | **YES** |

---

## 8. Scheduler Architecture

`TaskScheduler` (`app/tasks/scheduler.py`) provides background reminder checking without polling loops in caller threads.

```
                      +-------------------+
                      |   TaskScheduler   |
                      +-------------------+
                                |
               start() creates in-process daemon
                                |
                                v
                     +---------------------+
                     | _run_loop (daemon)  |
                     +---------------------+
                                |
                   Sleeps poll_interval (1.0s)
                                |
                                v
                     +---------------------+
                     |    run_pending()    |
                     +---------------------+
                                |
                     Queries due reminders
                                |
                                v
                     +---------------------+
                     | claim_reminder(id)  | (Atomic UPDATE in SQLite)
                     +---------------------+
                                |
                      If claim succeeds:
                                |
                                v
                     +---------------------+
                     |  on_reminder(task)  | (Isolated try/except block)
                     +---------------------+
```

### Safety Features
- **No Background Thread on Init**: No thread is spawned upon instantiation. Threads are only created when `start()` is explicitly called.
- **Thread Safety**: Uses an internal `threading.RLock()` to protect lifecycle state (`_running`, `_thread`).
- **Atomic Claiming**: Avoids race conditions by updating `reminder_triggered = 1` atomically in SQLite before invoking the notification callback.
- **Callback Isolation**: Exceptions raised by the user-provided callback are logged and caught; they never terminate the background loop.

---

## 9. Reminder Dispatch & Missed Reminder Recovery

### Immediate Dispatch
Reminders whose `reminder_at <= now` and `reminder_triggered = 0` and `status = 'pending'` are claimed and dispatched to the registered callback.

### Missed Reminder Recovery
When Anaya is started or restarted after a period of offline downtime:
1. `TaskManager.get_missed_reminders()` can be queried to detect reminders that expired during downtime.
2. In-flight missed reminders for pending tasks are immediately picked up on the scheduler's next evaluation tick.
3. Completed or cancelled tasks are excluded from reminder dispatch even if their reminder timestamp was reached.

---

## 10. Security Model

Anaya enforces strict security invariants for all task operations:

1. **Destructive Operation Barrier (`tasks.delete`)**:
   - Deleting a task requires explicit two-phase approval via `ApprovalService`.
   - The command executor halts and returns an approval request ID.
   - Spoken confirmations (*"yes"*, *"confirm"*, *"approve"*) are categorically rejected by `TextAssistant` to eliminate audio injection vulnerabilities.
   - Deletions must be approved out-of-band through the application's explicit interface.
2. **Input Sanitization & Length Limits**:
   - Task titles are strictly capped at 500 characters.
   - Descriptions are capped at 5000 characters.
   - SQL queries use parameterized binding exclusively; no dynamic string concatenation.
3. **Local-First & Offline**:
   - Zero telemetry, network calls, or cloud dependencies.

---

## 11. Persistence and Concurrency Model

### Storage Engine
- Stored in a standalone SQLite database at `~/.anaya/tasks.db` (or custom path in tests).
- Completely isolated from file indices and memory tables.

### Concurrency Guarantees
- **Write-Ahead Logging (WAL)**: Enabled via `PRAGMA journal_mode = WAL;` and `PRAGMA synchronous = NORMAL;`.
- **Thread Synchronization**: `TaskStore` uses a reentrant lock (`threading.RLock()`) around all database transactions.
- **Timeout Protection**: `sqlite3.connect(..., timeout=30.0)` prevents lock-wait failures during concurrent reads and writes.

---

## 12. Date/Time Parsing Capabilities & Boundaries

The pure stdlib `DateParser` (`app/tasks/date_parser.py`) converts natural language temporal references into timezone-aware UTC `datetime` objects:

### Capabilities
- **Relative Keywords**: `"today"`, `"tomorrow"`, `"tonight"`, `"yesterday"`.
- **Relative Deltas**: `"in 5 minutes"`, `"in 2 hours"`, `"in 3 days"`, `"in 1 week"`.
- **Named Weekdays**: `"monday"`, `"next tuesday"`, `"this friday"`.
- **Clock Times**: `"at 5pm"`, `"at 17:00"`, `"5:30 am"`.
- **ISO 8601 Strings**: Full ISO 8601 timestamp compatibility.

### Boundaries
- Strict UTC normalization: all datetimes stored in ISO 8601 format with explicit `+00:00` or `Z`.
- Complex multi-recurrence rules (e.g. *"every third Wednesday of the month"*) are outside Version 1 scope and reserved for future recurrence engines.

---

## 13. TextAssistant and Brain Integration

`TextAssistant` accepts `task_manager` as an optional dependency:
```python
assistant = TextAssistant(
    registry=registry,
    executor=executor,
    task_manager=task_manager,
)
```
- Automatically registers task commands when `task_manager` is provided.
- Passes `task_manager` reference to `registry._task_manager` for parser queries.
- Exposes `assistant.task_manager` property for external inspection.

---

## 14. Testing Strategy and Verification Results

The test suite provides comprehensive coverage across all task components:

| Test Module | Coverage Area | Tests Run | Result |
| :--- | :--- | :--- | :--- |
| `tests/test_tasks/test_models.py` | Immutability, validation, length limits, serialization | 17 | **PASSED** |
| `tests/test_tasks/test_task_store.py` | SQLite schema, CRUD, WAL persistence, atomic claim, concurrency | 9 | **PASSED** |
| `tests/test_tasks/test_task_manager.py` | ID generation, collisions, lifecycle, search, missed reminders | 10 | **PASSED** |
| `tests/test_tasks/test_scheduler.py` | Daemon lifecycle, atomic dispatch, exception isolation | 5 | **PASSED** |
| `tests/test_tasks/test_task_commands.py` | Command registry, argument validation, approval barrier | 5 | **PASSED** |
| `tests/test_tasks/test_task_brain.py` | Natural language queries, clarifications, verbal rejection | 9 | **PASSED** |
| **Total Task Tests** | Phase 6 Test Suite | **55** | **100% PASSED** |
| **Full Repository Tests** | All Phases (0 through 6) | **340 passed, 5 skipped** | **100% PASSED** |
