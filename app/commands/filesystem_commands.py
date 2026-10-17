
from app.commands.filesystem import FilesystemService
from app.commands.registry import CommandRegistry, CommandSpec


def register_filesystem_commands(
    registry: CommandRegistry,
    filesystem: FilesystemService,
) -> None:
    """Register filesystem operations with the command registry."""

    def valid_index(value: str) -> bool:
        try:
            index = int(value)
            return (
                str(index) == value
                and 0 <= index < filesystem.directory_count
            )
        except (TypeError, ValueError):
            return False

    def valid_name(index: str, name: str) -> bool:
        """Validate a filename within its selected authorized directory."""
        if not valid_index(index):
            return False

        return filesystem.validate_filename(int(index), name)

    def list_files(index: str) -> object:
        return filesystem.list_files(int(index))

    def read_file(index: str, name: str) -> object:
        return filesystem.read_text(int(index), name)

    def create_file(index: str, name: str, content: str) -> object:
        return filesystem.create_text(int(index), name, content)

    def delete_file(index: str, name: str) -> object:
        return filesystem.delete_file(int(index), name)

    registry.register(
        CommandSpec(
            name="files.list",
            description="List files in an authorized directory",
            handler=list_files,
            validate_arguments=lambda args: (
                len(args) == 1 and valid_index(args[0])
            ),
        )
    )

    registry.register(
        CommandSpec(
            name="files.read",
            description="Read a text file from an authorized directory",
            handler=read_file,
            validate_arguments=lambda args: (
                len(args) == 2
                and valid_name(args[0], args[1])
            ),
        )
    )

    registry.register(
        CommandSpec(
            name="files.create",
            description="Create a text file without overwriting",
            handler=create_file,
            validate_arguments=lambda args: (
                len(args) == 3
                and valid_name(args[0], args[1])
            ),
        )
    )

    registry.register(
        CommandSpec(
            name="files.delete",
            description="Delete a file after explicit approval",
            handler=delete_file,
            requires_confirmation=True,
            validate_arguments=lambda args: (
                len(args) == 2
                and valid_name(args[0], args[1])
            ),
        )
    )
