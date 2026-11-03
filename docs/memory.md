# Anaya Memory Engine Architecture

## 1. Overview

The Anaya Memory Engine (`app/memory/`) provides a reliable, local-first, persistent store for user-provided context across application restarts.

The engine is built on Python's standard-library `sqlite3` module. In accordance with Anaya's core architectural constraints:
- **Zero Cloud / External Database Dependencies:** Completely local and self-contained.
- **Zero Vector / Embedding Dependencies:** Uses deterministic standard-library text matching and relevance scoring.
- **Privacy-First:** User conversations and files are **never** silently recorded. Memories are saved exclusively through explicit user actions.
- **Passive Context:** Stored memory text is never evaluated or executed as commands.

---

## 2. Architecture & Data Flow

```
                      [ User Input ]
                            │
                            ▼
                    [ TextAssistant ]
                            │
               ┌────────────┴────────────┐
               ▼                         ▼
      [ CommandExecutor ]       [ MemoryManager ]
               │                         │
               ▼                         ▼
    [ Command Registry ]          [ MemoryStore ]
                                         │
                                         ▼
                               [ SQLite (memory.db) ]
```

### Module Responsibilities

| Module | Location | Primary Responsibility |
|---|---|---|
| `models` | `app/memory/models.py` | `Memory` dataclass (UUID, content, category, timestamps, tags, metadata) and `MemoryCategory` enum. |
| `memory_store` | `app/memory/memory_store.py` | Low-level SQLite persistence, schema migrations, WAL mode pragmas, parameterized queries, and connection lifecycle. |
| `memory_manager` | `app/memory/memory_manager.py` | High-level public API: validation, CRUD operations, keyword search, and deterministic relevance ranking. |
| `memory_commands` | `app/commands/memory_commands.py` | Command adapters (`memory.save`, `memory.list`, `memory.search`, `memory.delete`, `memory.clear`) registered with `CommandRegistry`. |
| `__init__` | `app/memory/__init__.py` | Public API surface exporting `Memory`, `MemoryCategory`, `MemoryManager`, and `MemoryStore`. |

---

## 3. Database Location and Configuration

By default, the database is stored in the user's home directory to isolate user data from the application source tree:
- **Default Path:** `~/.anaya/memory.db` (`Path.home() / ".anaya" / "memory.db"`)
- **Custom Path:** Fully configurable by passing `db_path` to `MemoryManager(db_path=...)` or `MemoryStore(db_path=...)`.
- **Automatic Directory Creation:** The store automatically creates parent directories as needed.
- **Connection Safety:** Configures `PRAGMA journal_mode = WAL`, `PRAGMA synchronous = NORMAL`, and `PRAGMA foreign_keys = ON`. Thread synchronization is guarded via `threading.RLock`.

---

## 4. Memory Model

Each memory record is defined by the immutable `Memory` dataclass:

```python
@dataclass(frozen=True)
class Memory:
    id: str                                  # Unique UUID string
    content: str                             # User memory text (non-empty)
    category: str = "general"                # Category identifier
    created_at: datetime                     # UTC creation timestamp
    updated_at: datetime                     # UTC last updated timestamp
    tags: tuple[str, ...] = ()               # Optional string tags
    metadata: dict[str, Any] = field(...)    # JSON-serializable dictionary
```

### Categories (`MemoryCategory`)
- `preference`: User preferences (e.g., UI theme, editor choice, tone).
- `fact`: User-provided facts (e.g., system details, key dates, names).
- `project`: Project-related notes, active tasks, repository locations.
- `goal`: User goals, milestones, and targets.
- `context`: Working context, environment details.
- `general`: Default fallback category.

---

## 5. Public API and Usage Examples

### Programmatic Usage

```python
from app.memory import MemoryManager, MemoryCategory

# Initialize manager (uses default ~/.anaya/memory.db or custom path)
with MemoryManager(db_path="./data/custom_memory.db") as memory:
    # 1. Save memory
    mem = memory.save_memory(
        content="I prefer dark mode and high contrast themes.",
        category=MemoryCategory.PREFERENCE,
        tags=("ui", "theme"),
    )
    print(f"Saved memory {mem.id}")

    # 2. Retrieve by ID
    item = memory.get_memory(mem.id)

    # 3. List memories by category
    prefs = memory.list_memories(category=MemoryCategory.PREFERENCE)

    # 4. Search memories
    matches = memory.search_memories("dark mode")

    # 5. Deterministic relevance retrieval
    relevant = memory.get_relevant_memories("What UI theme do I like?")

    # 6. Update memory
    memory.update_memory(mem.id, content="Updated: dark mode only")

    # 7. Delete memory
    memory.delete_memory(mem.id)
```

### Conversational / Natural Language Usage

When `MemoryManager` is integrated with `TextAssistant`:

- **Save:** `"remember that I prefer dark mode"` -> calls `memory.save` (default category: `general`)
- **List:** `"what do you remember about me?"` / `"list memories"` / `"show all memories"` -> calls `memory.list`
- **Search & Recall:**
  - `"search memories for dark mode"`
  - `"find my memories about dark mode"`
  - `"lookup memories about dark mode"` / `"look up memories about dark mode"`
  - `"recall my preferences"` / `"recall dark mode"`
  -> calls `memory.search` with deterministic keyword and lexical-stem fallback
- **Delete:**
  - Complete: `"delete memory <id>"` / `"forget memory <id>"` -> calls `memory.delete` *(Sensitive: triggers out-of-band approval workflow)*
  - Incomplete: `"delete memory"` / `"forget memory"` -> asks clarification: *"Which memory ID would you like to delete?"*
- **Clear All:**
  - `"clear all memories"` / `"wipe all memories"` / `"erase all memories"` / `"forget all memories"` -> calls `memory.clear` *(Sensitive: triggers out-of-band approval workflow)*

---

## 6. Search and Deterministic Relevance Retrieval

The `get_relevant_memories(query, limit=5)` method ranks stored memories without external neural embeddings:

1. **Tokenization:** Query is cleaned and split into alphanumeric tokens of length $\ge 2$.
2. **Exact Phrase Match (+10):** Query appears verbatim within memory content.
3. **Tag Matches (+5 per tag):** Memory tags matching query keywords.
4. **Token Matches (+2 per token):** Individual keyword occurrences in content.
5. **Category Alignment (+3):** Category name appears in query.
6. **Recency Tie-Breaker:** Memories with equal scores are ordered by `updated_at DESC`.

---

## 7. Privacy and User-Control Guarantees

1. **No Silent Ingestion:** Anaya never automatically indexes user chat history, terminal output, or local files into memory.
2. **Full Auditability:** Users can list, search, inspect, update, and delete any stored memory item at any time.
3. **Explicit Approvals for Destructive Operations:**
   - `memory.delete` requires explicit out-of-band confirmation.
   - `memory.clear` requires explicit out-of-band confirmation.
   - Confirmation cannot be granted in chat/text.
4. **No Code Execution:** Memory content is passive context. When retrieved, it is treated strictly as plain text and is never fed into command dispatch.

---

## 8. Error Handling

- **Validation:** Empty content, whitespace-only content, and invalid types raise immediate `ValueError`.
- **Database Failures:** SQLite errors during query execution or connection failures raise descriptive `RuntimeError` or `ValueError` without crashing the application.
- **SQL Injection Prevention:** All SQL statements use parameterized queries (`?`). Malicious SQL syntax in memory content or search strings is safely treated as literal text.
- **Connection Leak Prevention:** Connections are managed via locks and context managers.

---

## 9. Testing

The Memory Engine is thoroughly verified by tests in `tests/test_memory/test_memory_manager.py`:

```powershell
pytest tests/test_memory -v
```

Test coverage includes:
- Basic CRUD operations (save, get, list, update, delete, clear).
- Search filtering by content, category, and tags.
- Deterministic relevance scoring.
- Persistence across store instances and database restarts.
- Repeatable schema initialization.
- Input validation (empty content, invalid types, invalid database paths).
- SQL injection immunity and Unicode preservation.
- Brain integration, command registration, and approval preservation.

---

## 10. Current Limitations

1. **Text-Based Relevance:** Scoring relies on lexical token and phrase matching. Synonyms (e.g. "car" vs "automobile") are not matched unless tagged.
2. **Single Database File:** The store manages a single SQLite database file. Multi-user partitioning or multi-tenant namespaces are not implemented in this phase.
3. **Local Machine Scope:** Memory is stored locally on disk and does not sync across machines.
