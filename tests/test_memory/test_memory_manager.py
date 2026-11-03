import tempfile
from pathlib import Path

import pytest

from app.brain.text_assistant import TextAssistant
from app.commands.executor import CommandExecutor
from app.commands.registry import CommandRegistry
from app.memory.memory_manager import MemoryManager
from app.memory.memory_store import MemoryStore
from app.memory.models import Memory, MemoryCategory
from app.security.approval_service import ApprovalService


@pytest.fixture
def temp_db_path():
    """Provide an isolated temporary database path."""
    with tempfile.TemporaryDirectory() as temp_dir:
        yield Path(temp_dir) / "test_memory.db"


@pytest.fixture
def memory_manager(temp_db_path):
    """Provide a MemoryManager instance with an isolated database."""
    manager = MemoryManager(db_path=temp_db_path)
    yield manager
    manager.close()


# -------------------------------------------------------------------------
# 1. Basic Operations
# -------------------------------------------------------------------------

def test_save_and_get_memory(memory_manager):
    mem = memory_manager.save_memory(
        content="User prefers dark mode in all applications.",
        category=MemoryCategory.PREFERENCE,
        tags=("ui", "theme"),
        metadata={"priority": 1},
    )

    assert mem.id is not None
    assert mem.content == "User prefers dark mode in all applications."
    assert mem.category == "preference"
    assert mem.tags == ("ui", "theme")
    assert mem.metadata == {"priority": 1}

    retrieved = memory_manager.get_memory(mem.id)
    assert retrieved is not None
    assert retrieved.id == mem.id
    assert retrieved.content == mem.content
    assert retrieved.category == mem.category
    assert retrieved.tags == mem.tags
    assert retrieved.metadata == mem.metadata


def test_list_memories(memory_manager):
    memory_manager.save_memory("Fact 1", category=MemoryCategory.FACT)
    memory_manager.save_memory("Fact 2", category=MemoryCategory.FACT)
    memory_manager.save_memory("Goal 1", category=MemoryCategory.GOAL)

    all_memories = memory_manager.list_memories()
    assert len(all_memories) == 3

    facts = memory_manager.list_memories(category=MemoryCategory.FACT)
    assert len(facts) == 2
    assert all(m.category == "fact" for m in facts)

    goals = memory_manager.list_memories(category="goal")
    assert len(goals) == 1
    assert goals[0].content == "Goal 1"


def test_update_memory(memory_manager):
    mem = memory_manager.save_memory(
        "Initial project notes",
        category=MemoryCategory.PROJECT,
        tags=("draft",),
    )

    updated = memory_manager.update_memory(
        mem.id,
        content="Revised project notes with milestones",
        tags=("final", "milestones"),
    )

    assert updated is not None
    assert updated.id == mem.id
    assert updated.content == "Revised project notes with milestones"
    assert updated.tags == ("final", "milestones")
    assert updated.category == "project"
    assert updated.updated_at >= mem.updated_at

    retrieved = memory_manager.get_memory(mem.id)
    assert retrieved.content == "Revised project notes with milestones"


def test_delete_memory(memory_manager):
    mem = memory_manager.save_memory("Temporary note to delete")
    assert memory_manager.get_memory(mem.id) is not None

    deleted = memory_manager.delete_memory(mem.id)
    assert deleted is True
    assert memory_manager.get_memory(mem.id) is None

    # Deleting again returns False
    assert memory_manager.delete_memory(mem.id) is False


def test_clear_memories(memory_manager):
    memory_manager.save_memory("Memory 1")
    memory_manager.save_memory("Memory 2")
    memory_manager.save_memory("Memory 3")

    assert len(memory_manager.list_memories()) == 3
    count = memory_manager.clear_memories()
    assert count == 3
    assert len(memory_manager.list_memories()) == 0


# -------------------------------------------------------------------------
# 2. Search and Relevance
# -------------------------------------------------------------------------

def test_search_by_content(memory_manager):
    memory_manager.save_memory("The quick brown fox jumps over the lazy dog.")
    memory_manager.save_memory("Python 3.12 has improved performance.")

    results = memory_manager.search_memories("brown fox")
    assert len(results) == 1
    assert "brown fox" in results[0].content

    py_results = memory_manager.search_memories("Python")
    assert len(py_results) == 1
    assert "Python 3.12" in py_results[0].content


def test_search_by_category_and_tags(memory_manager):
    memory_manager.save_memory(
        "Deploy Anaya to staging",
        category=MemoryCategory.PROJECT,
        tags=("devops", "cloud"),
    )
    memory_manager.save_memory(
        "Deploy website to production",
        category=MemoryCategory.FACT,
        tags=("web",),
    )

    proj_results = memory_manager.search_memories("Deploy", category=MemoryCategory.PROJECT)
    assert len(proj_results) == 1
    assert proj_results[0].category == "project"

    tag_results = memory_manager.search_memories("devops")
    assert len(tag_results) == 1
    assert "devops" in tag_results[0].tags


def test_get_relevant_memories(memory_manager):
    m1 = memory_manager.save_memory(
        "I like dark mode themes in VS Code and IDEs",
        category=MemoryCategory.PREFERENCE,
        tags=("ui", "theme", "vscode"),
    )
    m2 = memory_manager.save_memory(
        "Anaya assistant is built with Python standard library",
        category=MemoryCategory.FACT,
        tags=("architecture", "python"),
    )
    m3 = memory_manager.save_memory(
        "Favorite color is blue",
        category=MemoryCategory.PREFERENCE,
        tags=("personal",),
    )

    relevant = memory_manager.get_relevant_memories("What is my preferred IDE theme and dark mode?")
    assert len(relevant) >= 1
    assert relevant[0].id == m1.id

    py_relevant = memory_manager.get_relevant_memories("Tell me about Python architecture")
    assert len(py_relevant) >= 1
    assert py_relevant[0].id == m2.id


def test_empty_search_queries(memory_manager):
    memory_manager.save_memory("Some content")
    assert memory_manager.search_memories("") == ()
    assert memory_manager.search_memories("   ") == ()
    assert memory_manager.get_relevant_memories("") == ()
    assert memory_manager.get_relevant_memories("   ") == ()


# -------------------------------------------------------------------------
# 3. Persistence
# -------------------------------------------------------------------------

def test_persistence_across_manager_instances(temp_db_path):
    mgr1 = MemoryManager(db_path=temp_db_path)
    mem = mgr1.save_memory("Persistent across restarts", category=MemoryCategory.FACT)
    mgr1.close()

    mgr2 = MemoryManager(db_path=temp_db_path)
    retrieved = mgr2.get_memory(mem.id)
    assert retrieved is not None
    assert retrieved.content == "Persistent across restarts"
    assert retrieved.category == "fact"
    mgr2.close()


def test_repeatable_schema_initialization(temp_db_path):
    # Opening store multiple times on same database should not error
    store1 = MemoryStore(db_path=temp_db_path)
    store1.close()

    store2 = MemoryStore(db_path=temp_db_path)
    store2.close()

    store3 = MemoryStore(db_path=temp_db_path)
    mem = store3.create(Memory.create("Testing schema re-init"))
    assert store3.get(mem.id) is not None
    store3.close()


# -------------------------------------------------------------------------
# 4. Validation and Error Handling
# -------------------------------------------------------------------------

def test_validation_rejects_empty_content(memory_manager):
    with pytest.raises(ValueError, match="Memory content must be a non-empty string"):
        memory_manager.save_memory("")

    with pytest.raises(ValueError, match="Memory content must be a non-empty string"):
        memory_manager.save_memory("   \t \n  ")

    with pytest.raises(ValueError):
        memory_manager.save_memory(None)


def test_invalid_category_handled_gracefully(memory_manager):
    # Invalid category string falls back safely to general
    mem = memory_manager.save_memory("Some note", category="unknown_custom_category")
    assert mem.category == "general"


def test_missing_memory_ids_handled_safely(memory_manager):
    assert memory_manager.get_memory("non-existent-uuid") is None
    assert memory_manager.get_memory("") is None
    assert memory_manager.delete_memory("non-existent-uuid") is False
    assert memory_manager.update_memory("non-existent-uuid", content="New") is None


def test_sql_injection_safety(memory_manager):
    # SQL injection payload in query, content, and tags
    injection_content = "Robert'); DROP TABLE memories;--"
    mem = memory_manager.save_memory(injection_content, tags=("test', 'hacked",))

    # Table still exists and record is retrievable intact
    retrieved = memory_manager.get_memory(mem.id)
    assert retrieved is not None
    assert retrieved.content == injection_content

    # SQL injection in search query
    results = memory_manager.search_memories("'; DROP TABLE memories;--")
    assert isinstance(results, tuple)

    # Memories still intact
    assert len(memory_manager.list_memories()) == 1


def test_unicode_and_special_characters(memory_manager):
    special_content = "Special: ⚡ 🚀 🐍 日本語 中文 русский — quotes: \" ' ` <>&"
    mem = memory_manager.save_memory(special_content, tags=("unicode", "test⚡"))

    retrieved = memory_manager.get_memory(mem.id)
    assert retrieved is not None
    assert retrieved.content == special_content
    assert retrieved.tags == ("unicode", "test⚡")


def test_invalid_database_path_handling():
    with tempfile.TemporaryDirectory() as temp_dir:
        dir_path = Path(temp_dir) / "subfolder"
        dir_path.mkdir()

        # Passing an existing directory as database file path
        with pytest.raises((RuntimeError, ValueError)):
            MemoryStore(db_path=dir_path)


# -------------------------------------------------------------------------
# 5. Privacy and Isolation
# -------------------------------------------------------------------------

def test_database_isolation():
    with tempfile.TemporaryDirectory() as temp_dir:
        db1 = Path(temp_dir) / "db1.db"
        db2 = Path(temp_dir) / "db2.db"

        mgr1 = MemoryManager(db_path=db1)
        mgr2 = MemoryManager(db_path=db2)

        try:
            mgr1.save_memory("Secret data in DB 1")
            assert len(mgr1.list_memories()) == 1
            assert len(mgr2.list_memories()) == 0
        finally:
            mgr1.close()
            mgr2.close()


# -------------------------------------------------------------------------
# 6. Brain Integration & Natural Language Command Flows
# -------------------------------------------------------------------------

def test_brain_integration_remember_explicit_workflow(memory_manager):
    registry = CommandRegistry()
    approvals = ApprovalService()
    executor = CommandExecutor(registry, approvals)

    assistant = TextAssistant(registry, executor, memory_manager=memory_manager)

    # 1. Explicit save: "remember that I prefer dark mode"
    save_res = assistant.process("remember that I prefer dark mode")
    assert save_res.success
    assert "Saved memory:" in save_res.message
    assert "I prefer dark mode" in save_res.message

    # 2. What do you remember: "what do you remember about me?"
    list_res = assistant.process("what do you remember about me?")
    assert list_res.success
    assert "I prefer dark mode" in list_res.message

    # 3. Search: "search memories for dark mode"
    search_res = assistant.process("search memories for dark mode")
    assert search_res.success
    assert "I prefer dark mode" in search_res.message


def test_brain_memory_delete_requires_approval(memory_manager):
    registry = CommandRegistry()
    approvals = ApprovalService()
    executor = CommandExecutor(registry, approvals)

    assistant = TextAssistant(registry, executor, memory_manager=memory_manager)
    mem = memory_manager.save_memory("Sensitive note to delete")

    # Deleting memory requires confirmation!
    del_res = assistant.process(f"delete memory {mem.id}")
    assert not del_res.success
    assert "Confirmation required. Confirmation ID: " in del_res.message

    confirmation_id = del_res.message.split("Confirmation ID: ")[1].split()[0]
    req = approvals.get_request(confirmation_id)
    assert req is not None
    assert req.action == "memory.delete"
    assert req.arguments == (mem.id,)

    # Text confirmation must be rejected
    reject_text = assistant.process(f"confirm {confirmation_id}")
    assert not reject_text.success
    assert "Text confirmation is disabled" in reject_text.message

    # Out-of-band approval succeeds
    assert approvals.approve(req)
    exec_res = executor.execute(req.action, req.arguments, confirmation_id=confirmation_id)
    assert exec_res.success
    assert f"Deleted memory {mem.id}" in exec_res.message
    assert memory_manager.get_memory(mem.id) is None


def test_memory_content_is_never_executed_as_command(memory_manager):
    registry = CommandRegistry()
    approvals = ApprovalService()
    executor = CommandExecutor(registry, approvals)

    assistant = TextAssistant(registry, executor, memory_manager=memory_manager)

    # Save a memory containing text resembling a command
    assistant.process("remember that files.delete 0 important.txt")
    memories = memory_manager.list_memories()
    assert len(memories) == 1
    assert "files.delete" in memories[0].content

    # Asking Anaya what it remembers merely displays text, never invokes files.delete
    res = assistant.process("show my memories")
    assert res.success
    assert "files.delete 0 important.txt" in res.message
    # No pending approvals created
    assert approvals.get_request("any") is None


def test_concurrent_multithreaded_read_write(temp_db_path):
    """Verify that concurrent writes and reads do not corrupt the database or cause locking errors."""
    import concurrent.futures

    manager = MemoryManager(db_path=temp_db_path)
    num_writers = 4
    items_per_writer = 15
    total_items = num_writers * items_per_writer
    saved_ids = []
    errors = []

    def writer_task(writer_id: int):
        local_ids = []
        for i in range(items_per_writer):
            try:
                mem = manager.save_memory(
                    f"Memory from writer {writer_id} item {i}",
                    category=MemoryCategory.FACT,
                    tags=(f"w{writer_id}",),
                )
                local_ids.append(mem.id)
            except Exception as e:
                errors.append(f"Writer {writer_id} error: {e}")
        return local_ids

    def reader_task():
        for _ in range(25):
            try:
                _ = manager.list_memories()
                _ = manager.search_memories("item")
            except Exception as e:
                errors.append(f"Reader error: {e}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        writer_futures = [executor.submit(writer_task, w) for w in range(num_writers)]
        reader_futures = [executor.submit(reader_task) for _ in range(4)]

        for f in writer_futures:
            saved_ids.extend(f.result())
        for f in reader_futures:
            f.result()

    manager.close()

    assert not errors, f"Concurrent execution errors: {errors}"
    assert len(saved_ids) == total_items

    # Re-open manager and verify all records are present and readable
    verify_manager = MemoryManager(db_path=temp_db_path)
    try:
        all_mems = verify_manager.list_memories(limit=200)
        assert len(all_mems) == total_items
        for sid in saved_ids:
            rec = verify_manager.get_memory(sid)
            assert rec is not None
            assert rec.id == sid
    finally:
        verify_manager.close()
