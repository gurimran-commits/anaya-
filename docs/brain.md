# Brain and Intent Understanding Architecture

## 1. Overview

The Anaya Brain subsystem (`app/brain/`) translates user input into validated command intents while strictly preserving the application's security model, out-of-band approval requirements, and deterministic execution pipeline.

The Brain operates under a **local-first, zero-mandatory-third-party-runtime-dependency** rule, using Python 3.12 standard library components to provide rule-based natural language understanding, robust argument extraction, clarification handling, and bounded conversational continuity.

---

## 2. Component Architecture

```
User Input (Text)
       │
       ▼
[ TextAssistant ] ──(Context reset / help / block in-band confirm)
       │
       ▼
[ IntentHandler.resolve() ]
       │
       ├─► 1. Exact Syntax Match (shlex) ───────► CommandIntent (if valid)
       │
       └─► 2. NaturalLanguageParser ─────────────► IntentResolution
                   │
                   ├── Checks pending clarification
                   ├── Extracts directory specification
                   ├── Matches natural intent patterns
                   ├── Detects missing arguments / ambiguity
                   └── Explains unsupported intents
       │
       ▼
[ CommandExecutor.execute() ]
       │
       ├─► Unconfirmed action ────► Command Handler ────► ActionResult
       │
       └─► Sensitive action ──────► ApprovalService ────► ActionResult (Confirmation Required)
```

### Module Responsibilities

| Module | Location | Primary Responsibility |
|---|---|---|
| `models` | `app/brain/models.py` | Data structures: `CommandIntent`, `IntentResolution`, `IntentStatus`. |
| `context` | `app/brain/context.py` | `ConversationContext`: In-memory, bounded single-turn conversational state (prior target, prior command, directory index, pending clarification). |
| `natural_parser` | `app/brain/natural_parser.py` | `NaturalLanguageParser`: Deterministic regex and token-based parser for supported phrasing, argument extraction, missing argument detection, and unsupported operation detection. |
| `intent_handler` | `app/brain/intent_handler.py` | `IntentHandler`: Unified parser routing between exact command syntax (`shlex`) and natural language resolution. Re-validates all arguments against `CommandRegistry`. |
| `text_assistant` | `app/brain/text_assistant.py` | `TextAssistant`: Main conversational entrypoint. Maintains conversation context, intercepts help/reset/text-approval commands, dispatches to `CommandExecutor`, and updates context upon execution. |

---

## 3. Supported Intent Patterns and Phrasings

The Brain only recognizes intents for operations registered in `CommandRegistry`. Currently registered operations:

### 1. File Listing (`files.list`)
- `"list files"`, `"show files"`, `"show my files"`, `"what files are here?"`, `"dir"`, `"ls"`
- Explicit directory selection: `"list files in directory 1"`, `"show files in folder 2"` (defaults to index `"0"`).

### 2. File Reading (`files.read`)
- `"read <file>"`, `"read file <file>"`, `"show contents of <file>"`, `"view file <file>"`, `"cat <file>"`
- Pronoun reference (with context): `"read it"`, `"read that file"`, `"show its contents"`

### 3. File Creation (`files.create`)
- `"create file <name> with content <content>"`
- `"create a file named <name> containing <content>"`
- `"make a file called <name> with <content>"`
- `"write <content> to file <name>"`
- `"create file <name>"` (creates empty file)
- Follow-up: `"now create another file <name> with <content>"`

### 4. File Deletion (`files.delete` - Sensitive, requires approval)
- `"delete file <name>"`, `"delete <name>"`, `"remove file <name>"`, `"erase <name>"`
- Pronoun reference (with context): `"delete it"`, `"remove that file"`

---

## 4. Argument Extraction Design

1. **Quotation Handling:**
   - Single (`'...'`) and double (`"..."`) quotes are extracted cleanly via `clean_quotes()`.
   - Arguments containing spaces are preserved intact (e.g. `"annual report (draft).txt"`).
2. **Directory Specification:**
   - Optional clauses like `"in directory 1"` or `"in folder 0"` are extracted cleanly, leaving the core command text stripped of directory tokens. Defaults to `"0"`.
3. **No Security Duplication:**
   - The Brain does not reinvent path sanitization. All extracted filenames and arguments are verified by calling `registry.validate()`, which in turn leverages `app.security.command_validator.resolve_safe_path()`.
   - Malformed arguments, traversal attempts (`../../secret.txt`), or Windows reserved device names (`CON`, `PRN`) fail registry validation closed.

---

## 5. Ambiguity and Clarification Mechanism

The Brain never guesses when critical information is missing or ambiguous.

1. **Missing Arguments (`IntentStatus.MISSING_ARGUMENT`):**
   - If the user says `"read file"` or `"delete file"` without specifying a target, the Brain records the pending intent in `context.pending_intent` and prompts:
     `"Which file would you like to read? Please specify the filename."`
   - In the subsequent turn, the user's answer (e.g. `"report.txt"`) automatically resolves the pending intent.
   - The user can cancel pending clarifications at any time by saying `"cancel"`, `"nevermind"`, or `"abort"`.
2. **Ambiguous Requests (`IntentStatus.AMBIGUOUS`):**
   - Vague commands (e.g. `"do it"`, `"run"`, `"open it"` when no target exists in context) return:
     `"Your request is ambiguous because no previous file or target was referenced. Please specify the action and target."`
   - No command is executed until ambiguity is resolved.
3. **Clear Unsupported Explanations (`IntentStatus.UNSUPPORTED`):**
   - Common out-of-scope actions (folder creation, email, web search, system power) provide clear explanations of what is currently supported.

---

## 6. Follow-Up Context Handling

The Brain includes a lightweight, bounded conversational context (`ConversationContext`):
- **Single-Turn Continuity:** Tracks only the most recent successful or pending operation (`last_command`, `last_target`, `last_directory_index`).
- **No Unbounded Memory:** Does not store unlimited chat transcripts or historical logs.
- **Resettable:**
  - By text command: `"reset"`, `"reset context"`, `"clear context"`, `"clear history"`.
  - By API method: `assistant.reset_context()`.
- **Security Invariant:** Conversational context **never overrides security controls**. When a pronoun resolves to a filename for `files.delete`, the command executor still insists on explicit approval through `ApprovalService`.

---

## 7. Security Boundaries

The Brain strictly adheres to the following mandatory security rules:

1. **No In-Band Text Approvals:**
   - Text inputs such as `"confirm <id>"`, `"approve <id>"`, or `"yes"` are rejected immediately by `TextAssistant`. Approvals must be granted exclusively via the authorized `ApprovalService` interface.
2. **No Direct OS Invocations:**
   - The Brain never calls system functions or filesystem APIs directly. All actions must be registered as `CommandSpec` instances in `CommandRegistry` and dispatched via `CommandExecutor`.
3. **No Forged Tokens:**
   - The Brain cannot manufacture approval tokens or bypass expiration limits.
4. **Input Length Limits:**
   - Input strings are strictly capped at 4096 characters to prevent denial-of-service through regex catastrophic backtracking.

---

## 8. Extension Points

1. **Custom Skills & Commands:**
   - When new commands are registered in `CommandRegistry`, they are immediately callable using exact syntax.
   - Natural language patterns can be mapped to new skills by adding matching rules in `NaturalLanguageParser`.
2. **Optional Future LLM Provider:**
   - An LLM-based intent parser can be introduced as an optional plugin. Any structured intent produced by an LLM will simply produce a `CommandIntent`, which must still pass `CommandRegistry.validate()` and `ApprovalService` workflows before execution.
