import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


class MemoryCategory(str, Enum):
    """Supported categories for stored user memories."""

    PREFERENCE = "preference"
    FACT = "fact"
    PROJECT = "project"
    GOAL = "goal"
    CONTEXT = "context"
    GENERAL = "general"

    @classmethod
    def from_str(cls, value: str) -> "MemoryCategory":
        """Convert a string to a MemoryCategory safely with fallback to GENERAL."""
        if not isinstance(value, str):
            return cls.GENERAL
        normalized = value.strip().casefold()
        for member in cls:
            if member.value == normalized:
                return member
        return cls.GENERAL


@dataclass(frozen=True)
class Memory:
    """Immutable data model representing a user-provided memory."""

    id: str
    content: str
    category: str = MemoryCategory.GENERAL.value
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    updated_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("Memory ID must be a non-empty string.")
        if not isinstance(self.content, str) or not self.content.strip():
            raise ValueError("Memory content must be a non-empty string.")
        if not isinstance(self.category, str) or not self.category.strip():
            raise ValueError("Memory category must be a non-empty string.")
        if not isinstance(self.tags, tuple) or not all(
            isinstance(t, str) for t in self.tags
        ):
            raise ValueError("Memory tags must be a tuple of strings.")
        if not isinstance(self.metadata, dict):
            raise ValueError("Memory metadata must be a dictionary.")

    @classmethod
    def create(
        cls,
        content: str,
        category: str | MemoryCategory = MemoryCategory.GENERAL,
        tags: tuple[str, ...] | list[str] = (),
        metadata: dict[str, Any] | None = None,
        memory_id: str | None = None,
    ) -> "Memory":
        """Create a new Memory instance with validated inputs and UUID."""
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Memory content must be a non-empty string.")

        category_str = (
            category.value
            if isinstance(category, MemoryCategory)
            else MemoryCategory.from_str(str(category)).value
        )

        cleaned_tags = tuple(
            t.strip()
            for t in tags
            if isinstance(t, str) and t.strip()
        )

        now = datetime.now(timezone.utc)
        return cls(
            id=memory_id if memory_id else str(uuid4()),
            content=content.strip(),
            category=category_str,
            created_at=now,
            updated_at=now,
            tags=cleaned_tags,
            metadata=dict(metadata or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert memory to serializable dictionary."""
        return {
            "id": self.id,
            "content": self.content,
            "category": self.category,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "tags": list(self.tags),
            "metadata": self.metadata,
        }
