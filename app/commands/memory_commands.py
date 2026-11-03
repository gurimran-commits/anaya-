from app.commands.registry import CommandRegistry, CommandSpec
from app.memory.memory_manager import MemoryManager
from app.memory.models import MemoryCategory


def register_memory_commands(
    registry: CommandRegistry,
    memory: MemoryManager,
) -> None:
    """Register persistent memory operations with the command registry."""

    def save_memory(content: str) -> object:
        mem = memory.save_memory(content)
        return f"Saved memory: {mem.content} (ID: {mem.id})"

    def list_memories() -> object:
        memories = memory.list_memories()
        if not memories:
            return "No memories stored."
        lines = [f"- [{m.category}] {m.content} (ID: {m.id})" for m in memories]
        return "\n".join(lines)

    def search_memories(query: str) -> object:
        results = memory.search_memories(query)
        if not results:
            return f"No memories found matching '{query}'."
        lines = [f"- [{m.category}] {m.content} (ID: {m.id})" for m in results]
        return "\n".join(lines)

    def delete_memory(memory_id: str) -> object:
        success = memory.delete_memory(memory_id)
        if not success:
            raise ValueError(f"Memory with ID '{memory_id}' not found.")
        return f"Deleted memory {memory_id}"

    def clear_memories() -> object:
        count = memory.clear_memories()
        return f"Cleared {count} memories."

    registry.register(
        CommandSpec(
            name="memory.save",
            description="Save a persistent user memory",
            handler=save_memory,
            validate_arguments=lambda args: (
                len(args) == 1 and isinstance(args[0], str) and bool(args[0].strip())
            ),
        )
    )

    registry.register(
        CommandSpec(
            name="memory.list",
            description="List all stored user memories",
            handler=list_memories,
            validate_arguments=lambda args: len(args) == 0,
        )
    )

    registry.register(
        CommandSpec(
            name="memory.search",
            description="Search stored user memories by query",
            handler=search_memories,
            validate_arguments=lambda args: (
                len(args) == 1 and isinstance(args[0], str) and bool(args[0].strip())
            ),
        )
    )

    registry.register(
        CommandSpec(
            name="memory.delete",
            description="Delete a stored memory by ID after explicit approval",
            handler=delete_memory,
            requires_confirmation=True,
            validate_arguments=lambda args: (
                len(args) == 1 and isinstance(args[0], str) and bool(args[0].strip())
            ),
        )
    )

    registry.register(
        CommandSpec(
            name="memory.clear",
            description="Clear all stored memories after explicit approval",
            handler=clear_memories,
            requires_confirmation=True,
            validate_arguments=lambda args: len(args) == 0,
        )
    )
