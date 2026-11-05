# Anaya — Runtime and Reminder Integration Architecture

## 1. Overview & Purpose

Phase 7B establishes Anaya's application runtime integration layer (`app/runtime.py`) and notification framework (`app/notifications/`).

This layer coordinates:
- The command execution pipeline (`CommandRegistry`, `CommandExecutor`, `ApprovalService`).
- Natural language and conversational processing (`TextAssistant`, `IntentHandler`).
- Persistent SQLite task management (`TaskManager`, `TaskStore`).
- Background reminder scheduling (`TaskScheduler`).
- Replaceable notification delivery (`NotificationHandler`, `ReminderNotification`).
- Missed reminder recovery across application restarts.
- Optional conversational AI (`ModelManager`).

This architecture is designed around Anaya's core engineering principles: local-first execution, strict process and thread safety, deterministic command boundaries, zero third-party runtime dependencies, and privacy.

---

## 2. Runtime Architecture & Dependency Composition

The `AnayaRuntime` class coordinates all subsystem lifecycles without hardcoding concrete dependencies or creating duplicate resources.

```
                      +-------------------+
                      |   AnayaRuntime    |
                      +---------+---------+
                                |
       +------------------------+------------------------+
       |                        |                        |
+------v------+          +------v------+          +------v------+
| TextAssistant|         | TaskScheduler|         | TaskManager |
+------+------+          +------+------+          +------+------+
       |                        |                        |
       |                        v                        v
       |               +-----------------+        +-------------+
       |               |NotificationHandl|        |  TaskStore  |
       |               +-----------------+        |  (SQLite)   |
       v                                          +-------------+
+--------------+
|CommandExecutr|
+--------------+
```

### Dependency Injection
All services are injected via constructor arguments with sensible defaults:
- If `task_manager` is omitted, an instance backed by `TaskManager(db_path=...)` is initialized.
- If `text_assistant` is omitted, it is constructed using the shared `CommandRegistry`, `CommandExecutor`, `TaskManager`, and optional `ModelManager`.
- If `notification_handler` is omitted, `LoggingNotificationHandler` is used by default.
- If an `on_reminder` callback is provided, it is automatically wrapped in a `CallbackNotificationHandler`.

Zero background threads are spawned upon module import or `AnayaRuntime` instantiation.

---

## 3. Scheduler Lifecycle & Concurrency Guarantees

The reminder scheduler (`TaskScheduler`) operates as a single background daemon thread per runtime instance.

### Lifecycle Methods
- **`runtime.start()`**:
  1. Idempotent: safe to invoke multiple times without spawning duplicate threads.
  2. Runs missed reminder recovery if `config.task_recover_missed_reminders` is enabled.
  3. Launches the single `AnayaTaskSchedulerWorker` background daemon thread.
  4. Sets `runtime.is_running = True`.
- **`runtime.stop(timeout=5.0)`**:
  1. Idempotent: safe to invoke multiple times.
  2. Sets the scheduler stop event, signaling the loop to terminate immediately.
  3. Joins the worker thread within the allotted timeout.
  4. Closes SQLite database connections cleanly.
  5. Sets `runtime.is_running = False`.
- **Context Manager (`with runtime:`)**:
  Supports standard Python context manager syntax, ensuring clean shutdown upon exiting the block even if unhandled exceptions occur.

---

## 4. Notification Interface & Delivery Adapters

Notifications are decoupled from the scheduler engine using a clean interface:

### Data Model: `ReminderNotification`
An immutable frozen payload containing:
- `task_id`: Short hex identifier of the task.
- `title`: Task title.
- `reminder_at`: Scheduled reminder instant (timezone-aware UTC).
- `description`: Optional task details.
- `due_date`: Optional task deadline.
- `priority`: Task priority level (`low`, `medium`, `high`).
- `is_missed`: Boolean flag indicating whether the reminder was recovered after downtime.
- `message`: Human-readable summary suitable for desktop popups, CLI output, or TTS playback.

### Handlers (`app/notifications/handlers.py`)
1. **`LoggingNotificationHandler`**: Minimal default writing reminder alerts to standard application logs.
2. **`CallbackNotificationHandler`**: Wraps any user-provided callback `Callable[[ReminderNotification], None]`.
3. **`CompositeNotificationHandler`**: Dispatches notifications to multiple registered listeners in sequence.
4. **`SpeechNotificationHandler`**: Optional voice adapter passing `notification.message` to an injected `TextToSpeechEngine` without modifying protected speech modules or activating the microphone.

---

## 5. Missed-Reminder Recovery & Delivery Semantics

When Anaya is not running, scheduled reminders that elapse become **missed reminders**.

### Startup Recovery Procedure
1. When `runtime.start()` executes, `runtime.recover_missed_reminders()` is called before starting the periodic background thread.
2. The runtime queries `TaskStore.get_missed_reminders(now)`:
   ```sql
   SELECT * FROM tasks
   WHERE reminder_at IS NOT NULL
     AND reminder_at <= ?
     AND reminder_triggered = 0
     AND status = 'pending'
   ORDER BY reminder_at ASC;
   ```
3. For each candidate task, an atomic compare-and-swap claim is executed:
   ```sql
   UPDATE tasks
   SET reminder_triggered = 1, updated_at = ?
   WHERE id = ? AND reminder_triggered = 0 AND status = 'pending';
   ```
4. Only if `cur.rowcount > 0` (claim succeeded) is the reminder constructed with `is_missed = True` and delivered to the configured `NotificationHandler`.

### Status Exclusion
- Tasks marked `status = 'completed'` are strictly excluded from reminder triggering and recovery.
- Tasks marked `status = 'cancelled'` are strictly excluded.
- Tasks already marked `reminder_triggered = 1` are never re-delivered.

### Delivery Guarantees & Crash Limitations
- **At-Most-Once Delivery Guarantee (No Exactly-Once)**:
  Exactly-once delivery across arbitrary system crashes without two-phase commit is impossible. Anaya deliberately implements **at-most-once delivery**: `reminder_triggered = 1` is committed atomically in SQLite **before** executing the notification callback. This strictly prevents concurrent workers or repeated polling ticks from ever delivering duplicate alerts.
- **The Crash Window & Notification Loss**:
  If the application process crashes, loses power, or is killed in the window after SQLite commits `reminder_triggered = 1` but before the notification callback finishes executing (or is perceived by the user), the reminder is considered consumed and will not be re-delivered upon restart.
- **Callback Failures**:
  If a custom notification callback raises an unhandled exception (e.g. temporary audio device failure or GUI crash), the runtime/scheduler catches and logs the error to prevent crashing the worker loop. Because the reminder was already claimed in SQLite, it is consumed and will not be retried automatically unless explicitly rescheduled by the user.
- **Shutdown Timeout & Restart Rejection Safety**:
  When `runtime.stop(timeout=...)` is invoked, the scheduler signals its worker thread to stop and waits for termination. If a hung user callback prevents the worker from terminating within the specified timeout:
  1. `AnayaRuntime` leaves the underlying SQLite `TaskManager` store open while the thread remains active, preventing `sqlite3.ProgrammingError` or handle corruption.
  2. The scheduler thread remains tracked, preventing multiple worker threads from being spawned.
  3. If `runtime.start()` is called while the previous worker thread is still alive, `AnayaRuntime` immediately rejects the call by raising `RuntimeError("Cannot start AnayaRuntime: previous scheduler worker thread is still active.")`. This prevents concurrent missed-reminder recovery or duplicate dispatches.
  4. Once the previous worker thread finishes its callback and terminates, subsequent `runtime.start()` calls recover missed reminders and launch a fresh worker normally.
- **Timezone & Daylight Saving Time (DST) Policy**:
  All timestamps in SQLite are strictly normalized and stored as UTC ISO-8601 strings. User-facing operations (such as `list_tasks_due_today`) convert stored UTC datetimes to the reference local timezone to preserve local calendar day boundaries. Date parsing utilizes the Python standard library's `zoneinfo` and `datetime` types. In accordance with Python's standard datetime semantics (PEP 495), ambiguous or nonexistent local times during DST transitions default to standard wall-clock mapping (`fold = 0`). Anaya does not prompt interactively for DST disambiguation; operations operate predictably based on system local time.

---

## 6. Task and Reminder Commands

Task commands operate with strict deterministic precedence. The conversational LLM cannot create, complete, or delete tasks.

### Command Capabilities

| User Phrase / Command | Mapped Operation | Description |
| :--- | :--- | :--- |
| `tasks.create "Buy groceries" "tomorrow"` | `tasks.create` | Creates task with deadline |
| `"Remind me to submit assignment at 6 PM"` | `tasks.create` | Creates task with independent reminder |
| `"Show my overdue tasks"` | `tasks.list overdue` | Lists pending tasks with `due_date < now` |
| `"What tasks are due today?"` | `tasks.list today` | Lists pending tasks with deadline today |
| `"Show upcoming tasks"` | `tasks.list upcoming` | Lists pending tasks with `due_date >= now` |
| `"Move deadline of assignment to tomorrow"` | `tasks.update` | Reschedules task deadline |
| `"Set a reminder for assignment at 4 PM"` | `tasks.update` | Sets or updates reminder timestamp |
| `"Cancel the reminder for assignment"` | `tasks.update` | Clears reminder without cancelling task |
| `"Complete task 1a2b3c4d"` | `tasks.complete` | Marks task as completed |
| `"Cancel task 1a2b3c4d"` | `tasks.cancel` | Marks task as cancelled |
| `"Delete task 1a2b3c4d"` | `tasks.delete` | Requires explicit two-step approval |

### Ambiguity and Date Validation
- **Ambiguous Task Names**: If a natural language query matches multiple tasks (e.g. `"Assignment Physics"` and `"Assignment Chemistry"`), Anaya returns `IntentStatus.AMBIGUOUS` with the candidate IDs, prompting the user for clarification.
- **Unparseable / Ambiguous Dates**: If a date or time expression cannot be parsed deterministically, Anaya returns `IntentStatus.INVALID` with a clear validation error. It **never guesses** and **never routes command failures to the LLM**.

---

## 7. Configuration Options

In `app/core/config.py`:

```python
@dataclass(frozen=True)
class AppConfig:
    ...
    # Task & reminder settings
    task_scheduling_enabled: bool = True
    task_scheduler_poll_interval: float = 15.0
    task_recover_missed_reminders: bool = True
```

- `task_scheduling_enabled`: If `False`, the background scheduler thread is not started.
- `task_scheduler_poll_interval`: Polling interval in seconds (default: 15.0s, minimum: 0.1s).
- `task_recover_missed_reminders`: If `False`, missed reminders from application downtime are skipped on startup.

---

## 8. Resource Efficiency & Zero Overhead

- **Single Background Worker**: Uses standard library `threading.Thread` with an `Event`-based wait mechanism. Zero busy-waiting loops.
- **Index-Accelerated Queries**: Reminder polling utilizes the composite SQLite index `idx_tasks_reminder ON tasks(reminder_at, reminder_triggered, status)`, executing queries in sub-millisecond time.
- **Zero AI Overhead**: Reminders and task queries do not trigger Ollama or local LLM inference. Reminder delivery functions fully even if Ollama is offline or disabled.
- **Zero Microphone Activation**: Reminders never automatically open the microphone or initiate audio capture.

---

## 9. Future Integration: GUI and Speech Subsystems

Phase 7B establishes clean injection points for future subsystems:
1. **Desktop GUI (Phase 8)**:
   A GUI frontend can subscribe to reminder alerts simply by passing an `on_reminder` callback to `AnayaRuntime(on_reminder=gui.show_toast_notification)`.
2. **Spoken Output**:
   To speak reminders aloud, a frontend can inject `SpeechNotificationHandler(tts_engine=kokoro_tts)` into `AnayaRuntime(notification_handler=...)`.
3. **Audio Safety**:
   Spoken notifications use existing one-way TTS output interfaces and do not interfere with push-to-talk state or microphone capture.
