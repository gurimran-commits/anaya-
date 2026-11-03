import tempfile
from pathlib import Path

import pytest

from app.brain.context import ConversationContext
from app.brain.intent_handler import IntentHandler
from app.brain.models import CommandIntent, IntentStatus
from app.brain.natural_parser import NaturalLanguageParser
from app.brain.text_assistant import TextAssistant
from app.commands.executor import CommandExecutor
from app.commands.filesystem import FilesystemService
from app.commands.filesystem_commands import register_filesystem_commands
from app.commands.memory_commands import register_memory_commands
from app.commands.registry import CommandRegistry, CommandSpec
from app.memory.memory_manager import MemoryManager
from app.security.approval_service import ApprovalService


@pytest.fixture
def test_env():
    """Create an isolated test environment with temporary filesystem."""
    with tempfile.TemporaryDirectory() as temp_dir:
        root_path = Path(temp_dir).resolve()
        registry = CommandRegistry()
        filesystem = FilesystemService((root_path,))
        register_filesystem_commands(registry, filesystem)

        approvals = ApprovalService()
        executor = CommandExecutor(registry, approvals)
        assistant = TextAssistant(registry, executor)

        yield {
            "root": root_path,
            "registry": registry,
            "filesystem": filesystem,
            "approvals": approvals,
            "executor": executor,
            "assistant": assistant,
        }
        filesystem.close()


# -------------------------------------------------------------------------
# 1. Supported Intent Recognition & Natural Phrasing Variations
# -------------------------------------------------------------------------

def test_natural_list_files(test_env):
    assistant = test_env["assistant"]
    filesystem = test_env["filesystem"]

    filesystem.create_text(0, "file1.txt", "content 1")
    filesystem.create_text(0, "file2.txt", "content 2")

    for phrase in [
        "list files",
        "show files",
        "show my files",
        "what files are here?",
        "dir",
        "ls",
        "files.list 0",
    ]:
        result = assistant.process(phrase)
        assert result.success, f"Failed for phrase: {phrase}"
        assert "file1.txt" in result.message
        assert "file2.txt" in result.message


def test_natural_list_files_in_specific_directory(test_env):
    root = test_env["root"]
    dir1 = root / "sub1"
    dir1.mkdir()
    (dir1 / "subfile.txt").write_text("sub content", encoding="utf-8")

    registry = CommandRegistry()
    fs = FilesystemService((root, dir1))
    register_filesystem_commands(registry, fs)
    assistant = TextAssistant(registry, CommandExecutor(registry))

    result = assistant.process("list files in directory 1")
    assert result.success
    assert "subfile.txt" in result.message
    fs.close()


def test_natural_read_file(test_env):
    assistant = test_env["assistant"]
    filesystem = test_env["filesystem"]
    filesystem.create_text(0, "notes.txt", "Secret notes content")

    for phrase in [
        "read notes.txt",
        "read file notes.txt",
        "show contents of notes.txt",
        "view file notes.txt",
        "open and read notes.txt",
        "cat notes.txt",
    ]:
        result = assistant.process(phrase)
        assert result.success, f"Failed for phrase: {phrase}"
        assert result.message == "Secret notes content"


def test_natural_create_file(test_env):
    assistant = test_env["assistant"]
    filesystem = test_env["filesystem"]

    res1 = assistant.process("create file hello.txt with content Hello World")
    assert res1.success
    assert filesystem.read_text(0, "hello.txt") == "Hello World"

    res2 = assistant.process("make a new file called sample.txt containing Sample text")
    assert res2.success
    assert filesystem.read_text(0, "sample.txt") == "Sample text"

    res3 = assistant.process("write Quick brown fox to file fox.txt")
    assert res3.success
    assert filesystem.read_text(0, "fox.txt") == "Quick brown fox"


def test_natural_create_empty_file(test_env):
    assistant = test_env["assistant"]
    filesystem = test_env["filesystem"]

    result = assistant.process("create file empty.txt")
    assert result.success
    assert filesystem.read_text(0, "empty.txt") == ""


def test_natural_delete_file_requires_approval(test_env):
    assistant = test_env["assistant"]
    filesystem = test_env["filesystem"]
    approvals = test_env["approvals"]
    executor = test_env["executor"]

    filesystem.create_text(0, "trash.txt", "delete me")

    for phrase in [
        "delete file trash.txt",
        "delete trash.txt",
        "remove file trash.txt",
        "erase trash.txt",
    ]:
        result = assistant.process(phrase)
        assert not result.success
        assert "Confirmation required. Confirmation ID: " in result.message

        confirmation_id = result.message.split("Confirmation ID: ")[1].split()[0]
        request = approvals.get_request(confirmation_id)
        assert request is not None
        assert request.action == "files.delete"
        assert request.arguments == ("0", "trash.txt")
        # Reject so we can test the next phrase cleanly
        approvals.reject(request)


# -------------------------------------------------------------------------
# 2. Argument Extraction with Quotes & Spaces
# -------------------------------------------------------------------------

def test_quoted_filenames_and_spaces(test_env):
    assistant = test_env["assistant"]
    filesystem = test_env["filesystem"]

    # Double quotes with spaces
    res = assistant.process('create file "my secret notes.txt" with content "classified data"')
    assert res.success
    assert filesystem.read_text(0, "my secret notes.txt") == "classified data"

    # Single quotes with spaces
    read_res = assistant.process("read 'my secret notes.txt'")
    assert read_res.success
    assert read_res.message == "classified data"

    # Delete with quotes requires confirmation
    del_res = assistant.process('delete file "my secret notes.txt"')
    assert not del_res.success
    assert "Confirmation required. Confirmation ID: " in del_res.message
    confirmation_id = del_res.message.split("Confirmation ID: ")[1].split()[0]
    req = test_env["approvals"].get_request(confirmation_id)
    assert req.arguments == ("0", "my secret notes.txt")


def test_unquoted_filename_with_spaces_in_read(test_env):
    assistant = test_env["assistant"]
    filesystem = test_env["filesystem"]

    filesystem.create_text(0, "my notes (draft).txt", "content here")
    result = assistant.process("read file my notes (draft).txt")
    assert result.success
    assert result.message == "content here"


# -------------------------------------------------------------------------
# 3. Missing Arguments and Clarification Flow
# -------------------------------------------------------------------------

def test_missing_argument_read_clarification(test_env):
    assistant = test_env["assistant"]
    filesystem = test_env["filesystem"]
    filesystem.create_text(0, "report.txt", "Report content")

    # Missing filename
    clarify = assistant.process("read file")
    assert not clarify.success
    assert "Which file would you like to read? Please specify the filename." in clarify.message
    assert assistant.context.has_pending

    # Answer clarification
    answered = assistant.process("report.txt")
    assert answered.success
    assert answered.message == "Report content"
    assert not assistant.context.has_pending


def test_missing_argument_delete_clarification(test_env):
    assistant = test_env["assistant"]
    filesystem = test_env["filesystem"]
    approvals = test_env["approvals"]

    filesystem.create_text(0, "old.txt", "old content")

    clarify = assistant.process("delete file")
    assert not clarify.success
    assert "Which file would you like to delete?" in clarify.message
    assert assistant.context.has_pending

    answered = assistant.process("old.txt")
    assert not answered.success
    assert "Confirmation required. Confirmation ID: " in answered.message
    assert not assistant.context.has_pending


def test_clarification_cancellation(test_env):
    assistant = test_env["assistant"]

    clarify = assistant.process("read file")
    assert not clarify.success
    assert assistant.context.has_pending

    cancel = assistant.process("cancel")
    assert not cancel.success
    assert "Action cancelled." in cancel.message
    assert not assistant.context.has_pending


# -------------------------------------------------------------------------
# 4. Ambiguous Requests Handling
# -------------------------------------------------------------------------

def test_ambiguous_requests_without_context(test_env):
    assistant = test_env["assistant"]

    for phrase in [
        "open it",
        "read it",
        "delete it",
        "remove it",
        "do it",
        "process it",
        "run",
    ]:
        result = assistant.process(phrase)
        assert not result.success
        assert "ambiguous" in result.message.casefold()


# -------------------------------------------------------------------------
# 5. Unsupported Requests Explanation
# -------------------------------------------------------------------------

def test_unsupported_requests_explained_clearly(test_env):
    assistant = test_env["assistant"]

    folder_res = assistant.process("create folder Projects")
    assert not folder_res.success
    assert "Folder operations are not currently supported" in folder_res.message

    email_res = assistant.process("send email to Alice")
    assert not email_res.success
    assert "Messaging and email operations are not supported" in email_res.message

    web_res = assistant.process("browse the web for news")
    assert not web_res.success
    assert "Web browsing and search operations are not supported" in web_res.message

    power_res = assistant.process("shutdown computer")
    assert not power_res.success
    assert "System power and process operations are not supported" in power_res.message


# -------------------------------------------------------------------------
# 6. Follow-Up Context & Continuity
# -------------------------------------------------------------------------

def test_followup_read_then_delete_it(test_env):
    assistant = test_env["assistant"]
    filesystem = test_env["filesystem"]
    approvals = test_env["approvals"]
    executor = test_env["executor"]

    filesystem.create_text(0, "todo.txt", "Buy milk")

    # Step 1: Read the file
    read_res = assistant.process("read todo.txt")
    assert read_res.success
    assert read_res.message == "Buy milk"
    assert assistant.context.last_target == "todo.txt"

    # Step 2: Follow-up using pronoun "delete it"
    delete_res = assistant.process("delete it")
    assert not delete_res.success
    assert "Confirmation required. Confirmation ID: " in delete_res.message

    confirmation_id = delete_res.message.split("Confirmation ID: ")[1].split()[0]
    req = approvals.get_request(confirmation_id)
    assert req is not None
    assert req.action == "files.delete"
    assert req.arguments == ("0", "todo.txt")

    # Step 3: Approve out-of-band and execute
    assert approvals.approve(req)
    final_res = executor.execute(req.action, req.arguments, confirmation_id=confirmation_id)
    assert final_res.success
    assert "Deleted todo.txt" in final_res.message


def test_followup_create_another(test_env):
    assistant = test_env["assistant"]
    filesystem = test_env["filesystem"]

    res1 = assistant.process("create file part1.txt with content Part 1 data")
    assert res1.success
    assert assistant.context.last_target == "part1.txt"

    res2 = assistant.process("now create another file part2.txt with content Part 2 data")
    assert res2.success
    assert assistant.context.last_target == "part2.txt"

    assert filesystem.read_text(0, "part1.txt") == "Part 1 data"
    assert filesystem.read_text(0, "part2.txt") == "Part 2 data"


def test_context_reset(test_env):
    assistant = test_env["assistant"]
    filesystem = test_env["filesystem"]
    filesystem.create_text(0, "memo.txt", "memo notes")

    assistant.process("read memo.txt")
    assert assistant.context.last_target == "memo.txt"

    # Explicit reset via text command
    reset_res = assistant.process("reset context")
    assert reset_res.success
    assert "Context has been reset." in reset_res.message
    assert assistant.context.last_target is None

    # Follow-up "read it" should now fail as ambiguous
    after_res = assistant.process("read it")
    assert not after_res.success
    assert "ambiguous" in after_res.message.casefold()


def test_programmatic_context_reset(test_env):
    assistant = test_env["assistant"]
    filesystem = test_env["filesystem"]
    filesystem.create_text(0, "memo.txt", "memo notes")

    assistant.process("read memo.txt")
    assert assistant.context.last_target == "memo.txt"

    assistant.reset_context()
    assert assistant.context.last_target is None


# -------------------------------------------------------------------------
# 7. Security Safeguards & Invariant Preservation
# -------------------------------------------------------------------------

def test_security_rejection_of_text_confirmation(test_env):
    assistant = test_env["assistant"]
    filesystem = test_env["filesystem"]
    filesystem.create_text(0, "vault.txt", "secure data")

    del_res = assistant.process("delete file vault.txt")
    assert not del_res.success
    confirmation_id = del_res.message.split("Confirmation ID: ")[1].split()[0]

    for attempt in [
        f"confirm {confirmation_id}",
        f"approve {confirmation_id}",
        "yes",
        "confirm",
        "approve",
    ]:
        res = assistant.process(attempt)
        assert not res.success
        assert "Text confirmation is disabled" in res.message


def test_path_traversal_in_natural_language_rejected(test_env):
    assistant = test_env["assistant"]

    for phrase in [
        "read file ../secret.txt",
        "read ../../passwd",
        "delete file ../outside.txt",
        'create file "../evil.txt" with content payload',
    ]:
        res = assistant.process(phrase)
        assert not res.success
        assert "invalid" in res.message.casefold() or "not recognized" in res.message.casefold()


# -------------------------------------------------------------------------
# 8. Input Validation & Edge Cases
# -------------------------------------------------------------------------

def test_invalid_input_types(test_env):
    assistant = test_env["assistant"]
    assert not assistant.process(None).success
    assert not assistant.process(12345).success
    assert not assistant.process(["read"]).success


def test_empty_and_whitespace_input(test_env):
    assistant = test_env["assistant"]
    assert not assistant.process("").success
    assert not assistant.process("   \t  \n ").success


def test_input_length_limit(test_env):
    assistant = test_env["assistant"]
    oversized = "a" * 5000
    res = assistant.process(oversized)
    assert not res.success
    assert "exceeds maximum permitted length" in res.message


# -------------------------------------------------------------------------
# 9. Custom Command & Exact Syntax Preservation
# -------------------------------------------------------------------------

def test_custom_command_exact_syntax(test_env):
    registry = test_env["registry"]
    executor = test_env["executor"]

    executed = []
    registry.register(
        CommandSpec(
            name="demo.greet",
            description="Greet someone",
            handler=lambda name: executed.append(name) or f"Hello, {name}!",
            validate_arguments=lambda args: len(args) == 1,
        )
    )

    assistant = TextAssistant(registry, executor)
    res = assistant.process('demo.greet "Alice in Wonderland"')
    assert res.success
    assert res.message == "Hello, Alice in Wonderland!"
    assert executed == ["Alice in Wonderland"]


# -------------------------------------------------------------------------
# 10. Phase 2 Refinement Regressions
# -------------------------------------------------------------------------

def test_clarification_precedence_new_command_clears_and_executes(test_env):
    assistant = test_env["assistant"]
    filesystem = test_env["filesystem"]

    filesystem.create_text(0, "alpha.txt", "Alpha content")
    filesystem.create_text(0, "beta.txt", "Beta content")

    # Step 1: Trigger clarification
    clarify_res = assistant.process("read file")
    assert not clarify_res.success
    assert "Which file would you like to read?" in clarify_res.message
    assert assistant.context.has_pending

    # Step 2: User changes their mind and inputs a new command intent: "list files"
    # Should execute files.list, NOT attempt to read a file named "list files"
    list_res = assistant.process("list files")
    assert list_res.success
    assert "alpha.txt" in list_res.message
    assert "beta.txt" in list_res.message
    assert not assistant.context.has_pending


def test_clarification_precedence_with_create_command(test_env):
    assistant = test_env["assistant"]
    filesystem = test_env["filesystem"]

    # Trigger clarification
    assistant.process("delete file")
    assert assistant.context.has_pending

    # User instead issues a create command
    create_res = assistant.process("create file newdoc.txt with content New document")
    assert create_res.success
    assert filesystem.read_text(0, "newdoc.txt") == "New document"
    assert not assistant.context.has_pending


def test_clarification_normal_filename_continues(test_env):
    assistant = test_env["assistant"]
    filesystem = test_env["filesystem"]

    filesystem.create_text(0, "target.txt", "Target content")

    assistant.process("read file")
    assert assistant.context.has_pending

    read_res = assistant.process("target.txt")
    assert read_res.success
    assert read_res.message == "Target content"
    assert not assistant.context.has_pending


def test_clarification_filename_with_keywords_not_mistaken_for_command(test_env):
    assistant = test_env["assistant"]
    filesystem = test_env["filesystem"]

    # A filename containing common words like "files" or "list"
    filesystem.create_text(0, "my list of files.txt", "List of files content")

    assistant.process("read file")
    assert assistant.context.has_pending

    res = assistant.process("my list of files.txt")
    assert res.success
    assert res.message == "List of files content"
    assert not assistant.context.has_pending


def test_clarification_cancellation_keywords(test_env):
    assistant = test_env["assistant"]

    for cancel_word in ["cancel", "abort", "nevermind", "stop"]:
        assistant.process("read file")
        assert assistant.context.has_pending

        cancel_res = assistant.process(cancel_word)
        assert not cancel_res.success
        assert "Action cancelled." in cancel_res.message
        assert not assistant.context.has_pending


def test_quoted_filename_with_directory_phrase_preserved(test_env):
    assistant = test_env["assistant"]
    filesystem = test_env["filesystem"]

    # Filename literally contains "in folder 2"
    filename = "quarterly report in folder 2.txt"
    filesystem.create_text(0, filename, "Report for Q2")

    res = assistant.process(f'read "{filename}"')
    assert res.success
    assert res.message == "Report for Q2"
    assert assistant.context.last_target == filename


def test_quoted_filename_with_external_directory_instruction(test_env):
    root = test_env["root"]
    dir1 = root / "folder1"
    dir1.mkdir()
    filename = "report in folder 2.txt"
    (dir1 / filename).write_text("Data in folder 1", encoding="utf-8")

    registry = CommandRegistry()
    fs = FilesystemService((root, dir1))
    register_filesystem_commands(registry, fs)
    assistant = TextAssistant(registry, CommandExecutor(registry))

    # Directory instruction is OUTSIDE the quotes
    res = assistant.process(f'read "{filename}" in folder 1')
    assert res.success
    assert res.message == "Data in folder 1"
    assert assistant.context.last_target == filename
    assert assistant.context.last_directory_index == "1"
    fs.close()


def test_context_has_no_last_content_attribute():
    context = ConversationContext()
    assert not hasattr(context, "last_content")


# -------------------------------------------------------------------------
# 8. Memory Brain Natural Language Integration Tests
# -------------------------------------------------------------------------

@pytest.fixture
def memory_brain_env():
    """Environment with both filesystem and memory commands registered."""
    import gc

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
        temp_path = Path(temp_dir).resolve()
        fs_root = temp_path / "fs"
        fs_root.mkdir()
        db_path = temp_path / "brain_memory.db"

        registry = CommandRegistry()
        filesystem = FilesystemService((fs_root,))
        register_filesystem_commands(registry, filesystem)

        memory_manager = MemoryManager(db_path=db_path)

        approvals = ApprovalService()
        executor = CommandExecutor(registry, approvals)
        assistant = TextAssistant(registry, executor, memory_manager=memory_manager)

        yield {
            "root": fs_root,
            "registry": registry,
            "filesystem": filesystem,
            "memory": memory_manager,
            "approvals": approvals,
            "executor": executor,
            "assistant": assistant,
        }
        filesystem.close()
        memory_manager.close()
        gc.collect()


def test_natural_memory_remember_and_recall_workflow(memory_brain_env):
    assistant = memory_brain_env["assistant"]

    # 1. "Remember that I prefer dark mode."
    save_res = assistant.process("Remember that I prefer dark mode.")
    assert save_res.success
    assert "Saved memory:" in save_res.message
    assert "I prefer dark mode" in save_res.message

    # 2. "What do you remember about me?"
    about_res = assistant.process("What do you remember about me?")
    assert about_res.success
    assert "I prefer dark mode" in about_res.message

    # 3. "List memories."
    list_res = assistant.process("List memories.")
    assert list_res.success
    assert "I prefer dark mode" in list_res.message

    # 4. "Recall my preferences."
    recall_res = assistant.process("Recall my preferences.")
    assert recall_res.success
    assert "I prefer dark mode" in recall_res.message


def test_natural_memory_search_phrasing_variations(memory_brain_env):
    assistant = memory_brain_env["assistant"]

    # Save preference
    assistant.process("Remember that I prefer dark mode.")

    for phrase in [
        "Search memories for dark mode.",
        "Find my memories about dark mode.",
        "Lookup memories about dark mode.",
        "Look up memories about dark mode.",
        "search memories dark mode",
    ]:
        res = assistant.process(phrase)
        assert res.success, f"Failed for phrase: {phrase}"
        assert "I prefer dark mode" in res.message


def test_natural_memory_clear_all_workflow_and_security(memory_brain_env):
    assistant = memory_brain_env["assistant"]
    approvals = memory_brain_env["approvals"]
    executor = memory_brain_env["executor"]
    memory_manager = memory_brain_env["memory"]

    # Save initial memory
    assistant.process("Remember that I prefer dark mode.")
    assert len(memory_manager.list_memories()) == 1

    # Phrasing variations must all route to memory.clear and require approval
    clear_phrases = [
        "Clear all memories.",
        "Please clear all memories",
        "Wipe all memories",
        "Erase all memories",
        "Forget all memories",
    ]

    for phrase in clear_phrases:
        res = assistant.process(phrase)
        # Clear-all must NEVER execute immediately without approval
        assert not res.success
        assert "Confirmation required. Confirmation ID: " in res.message
        assert "Approve or reject this request" in res.message

        conf_id = res.message.split("Confirmation ID: ")[1].split()[0]
        req = approvals.get_request(conf_id)
        assert req is not None
        assert req.action == "memory.clear"
        assert req.arguments == ()

        # In-band responses must be rejected
        for in_band in ["yes", "confirm", "approve"]:
            reject_res = assistant.process(in_band)
            assert not reject_res.success
            assert "Text confirmation is disabled" in reject_res.message

        # Reject the pending request to reset for next phrase test
        approvals.reject(req)

    # Now test successful out-of-band authorization for "Clear all memories."
    res = assistant.process("Clear all memories.")
    conf_id = res.message.split("Confirmation ID: ")[1].split()[0]
    req = approvals.get_request(conf_id)

    # Approve out-of-band
    assert approvals.approve(req)
    exec_res = executor.execute(req.action, req.arguments, confirmation_id=conf_id)
    assert exec_res.success
    assert "Cleared 1 memories." in exec_res.message
    assert len(memory_manager.list_memories()) == 0

    # Verify list memories is now empty
    empty_list = assistant.process("List memories.")
    assert empty_list.success
    assert empty_list.message == "No memories stored."


def test_natural_memory_delete_clarification_and_approval(memory_brain_env):
    assistant = memory_brain_env["assistant"]
    approvals = memory_brain_env["approvals"]
    executor = memory_brain_env["executor"]
    memory_manager = memory_brain_env["memory"]

    mem = memory_manager.save_memory("Temporary configuration note")
    mem_id = mem.id

    # 1. Incomplete deletion: "Forget memory"
    prompt_res = assistant.process("Forget memory")
    assert not prompt_res.success
    assert prompt_res.message == "Which memory ID would you like to delete?"
    assert assistant.context.has_pending
    assert assistant.context.pending_intent == "memory.delete"

    # Memory must NOT be deleted during clarification
    assert memory_manager.get_memory(mem_id) is not None

    # 2. Provide the memory ID
    id_res = assistant.process(mem_id)
    # Providing memory ID only requests the operation; it does NOT authorize it
    assert not id_res.success
    assert "Confirmation required. Confirmation ID: " in id_res.message
    assert not assistant.context.has_pending

    conf_id = id_res.message.split("Confirmation ID: ")[1].split()[0]
    req = approvals.get_request(conf_id)
    assert req is not None
    assert req.action == "memory.delete"
    assert req.arguments == (mem_id,)

    # In-band approval rejected
    reject_res = assistant.process("confirm")
    assert not reject_res.success
    assert "Text confirmation is disabled" in reject_res.message

    # Memory still exists
    assert memory_manager.get_memory(mem_id) is not None

    # Out-of-band approval
    assert approvals.approve(req)
    exec_res = executor.execute(req.action, req.arguments, confirmation_id=conf_id)
    assert exec_res.success
    assert f"Deleted memory {mem_id}" in exec_res.message
    assert memory_manager.get_memory(mem_id) is None


def test_natural_memory_delete_clarification_cancellation(memory_brain_env):
    assistant = memory_brain_env["assistant"]

    for cancel_phrase in ["cancel", "nevermind", "abort", "stop"]:
        prompt_res = assistant.process("Delete memory")
        assert not prompt_res.success
        assert prompt_res.message == "Which memory ID would you like to delete?"
        assert assistant.context.has_pending

        cancel_res = assistant.process(cancel_phrase)
        assert not cancel_res.success
        assert "Action cancelled." in cancel_res.message
        assert not assistant.context.has_pending


def test_natural_memory_clarification_superseded_by_new_commands(memory_brain_env):
    assistant = memory_brain_env["assistant"]
    filesystem = memory_brain_env["filesystem"]

    filesystem.create_text(0, "notes.txt", "Notes content")

    # Start memory deletion clarification
    assistant.process("Delete memory")
    assert assistant.context.has_pending

    # Supersede with a filesystem command
    list_res = assistant.process("list files")
    assert list_res.success
    assert "notes.txt" in list_res.message
    assert not assistant.context.has_pending

    # Start memory deletion clarification again
    assistant.process("Forget memory")
    assert assistant.context.has_pending

    # Supersede with memory listing
    mem_res = assistant.process("list memories")
    assert mem_res.success
    assert not assistant.context.has_pending


def test_empty_memory_list_and_search_formatting(memory_brain_env):
    assistant = memory_brain_env["assistant"]

    empty_list = assistant.process("List memories.")
    assert empty_list.success
    assert empty_list.message == "No memories stored."

    empty_search = assistant.process("Search memories for dark mode.")
    assert empty_search.success
    assert empty_search.message == "No memories found matching 'dark mode'."


def test_unrelated_filesystem_and_web_commands_not_confused_with_memory(memory_brain_env):
    assistant = memory_brain_env["assistant"]
    filesystem = memory_brain_env["filesystem"]

    filesystem.create_text(0, "project.txt", "Project specs")

    # Filesystem operations
    fs_read = assistant.process("read project.txt")
    assert fs_read.success
    assert fs_read.message == "Project specs"

    # Web search operations must remain unsupported
    for web_query in [
        "search google for python",
        "browse the web",
        "lookup recipes online",
        "look up news",
    ]:
        web_res = assistant.process(web_query)
        assert not web_res.success
        assert "Web browsing and search operations are not supported" in web_res.message
