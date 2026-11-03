import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.memory.memory_store import MemoryStore
from app.memory.models import Memory, MemoryCategory


class MemoryManager:
    """High-level public manager for Anaya persistent user memories.

    Provides validated memory persistence, deterministic relevance retrieval,
    and user privacy controls without cloud dependencies.
    """

    def __init__(
        self,
        store: MemoryStore | None = None,
        db_path: Path | str | None = None,
    ) -> None:
        self._store = store if store is not None else MemoryStore(db_path=db_path)

    @property
    def store(self) -> MemoryStore:
        """Expose underlying store for configuration and inspection."""
        return self._store

    def save_memory(
        self,
        content: str,
        category: str | MemoryCategory = MemoryCategory.GENERAL,
        tags: tuple[str, ...] | list[str] = (),
        metadata: dict[str, Any] | None = None,
    ) -> Memory:
        """Validate input and save a new persistent memory."""
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Memory content must be a non-empty string.")

        memory = Memory.create(
            content=content,
            category=category,
            tags=tags,
            metadata=metadata,
        )
        return self._store.create(memory)

    def get_memory(self, memory_id: str) -> Memory | None:
        """Retrieve a stored memory by ID."""
        if not isinstance(memory_id, str) or not memory_id.strip():
            return None
        return self._store.get(memory_id.strip())

    def list_memories(
        self,
        category: str | MemoryCategory | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[Memory, ...]:
        """List stored memories with optional category filter and pagination."""
        cat_str = (
            category.value
            if isinstance(category, MemoryCategory)
            else (str(category).strip() if category else None)
        )
        return self._store.list_all(category=cat_str, limit=limit, offset=offset)

    def search_memories(
        self,
        query: str,
        category: str | MemoryCategory | None = None,
        limit: int = 100,
    ) -> tuple[Memory, ...]:
        """Search stored memories by keyword query."""
        if not isinstance(query, str) or not query.strip():
            return ()

        cat_str = (
            category.value
            if isinstance(category, MemoryCategory)
            else (str(category).strip() if category else None)
        )
        direct_matches = self._store.search(
            query=query.strip(), category=cat_str, limit=limit
        )
        if direct_matches:
            return direct_matches

        return self.get_relevant_memories(
            query=query.strip(), category=cat_str, limit=limit
        )

    def update_memory(
        self,
        memory_id: str,
        content: str | None = None,
        category: str | MemoryCategory | None = None,
        tags: tuple[str, ...] | list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Memory | None:
        """Update fields of an existing memory."""
        if not isinstance(memory_id, str) or not memory_id.strip():
            return None

        existing = self.get_memory(memory_id)
        if existing is None:
            return None

        new_content = existing.content
        if content is not None:
            if not isinstance(content, str) or not content.strip():
                raise ValueError("Updated content must be a non-empty string.")
            new_content = content.strip()

        new_category = existing.category
        if category is not None:
            new_category = (
                category.value
                if isinstance(category, MemoryCategory)
                else MemoryCategory.from_str(str(category)).value
            )

        new_tags = existing.tags
        if tags is not None:
            new_tags = tuple(
                t.strip()
                for t in tags
                if isinstance(t, str) and t.strip()
            )

        new_metadata = existing.metadata
        if metadata is not None:
            if not isinstance(metadata, dict):
                raise ValueError("Metadata must be a dictionary.")
            new_metadata = dict(metadata)

        updated_memory = Memory(
            id=existing.id,
            content=new_content,
            category=new_category,
            created_at=existing.created_at,
            updated_at=datetime.now(timezone.utc),
            tags=new_tags,
            metadata=new_metadata,
        )

        return self._store.update(updated_memory)

    def delete_memory(self, memory_id: str) -> bool:
        """Delete an existing memory by ID."""
        if not isinstance(memory_id, str) or not memory_id.strip():
            return False
        return self._store.delete(memory_id.strip())

    def clear_memories(self) -> int:
        """Permanently clear all stored memories."""
        return self._store.clear()

    def get_relevant_memories(
        self,
        query: str,
        limit: int = 5,
        category: str | MemoryCategory | None = None,
    ) -> tuple[Memory, ...]:
        """Deterministic keyword and relevance scoring for contextual retrieval.

        Scoring heuristic:
        - Exact full phrase match in content: +10
        - Full keyword match in tags: +5 per matching tag
        - Individual keyword match in content: +2 per keyword
        - Category alignment: +3
        - Recency tie-breaker
        """
        if not isinstance(query, str) or not query.strip():
            return ()
        if not isinstance(limit, int) or limit <= 0:
            limit = 5

        cat_str = (
            category.value
            if isinstance(category, MemoryCategory)
            else (str(category).strip() if category else None)
        )

        # Retrieve candidates from the store.
        candidates = self._store.list_all(category=cat_str, limit=500)
        if not candidates:
            return ()

        query_clean = query.strip().casefold()
        # Tokenize query into alphanumeric words of length >= 2.
        tokens = set(re.findall(r"\b\w{2,}\b", query_clean))
        if not tokens:
            return ()

        scored: list[tuple[float, Memory]] = []

        for mem in candidates:
            score = 0.0
            content_lower = mem.content.casefold()

            # 1. Exact phrase match.
            if query_clean in content_lower:
                score += 10.0

            # 2. Tag matches.
            for tag in mem.tags:
                tag_lower = tag.casefold()
                if tag_lower in tokens or tag_lower == query_clean:
                    score += 5.0

            # 3. Individual token matches.
            for token in tokens:
                if token in content_lower:
                    score += 2.0
                elif any(
                    (w.startswith(token) or token.startswith(w))
                    for w in re.findall(r"\b\w{2,}\b", content_lower)
                    if len(w) >= 4 and len(token) >= 4
                ):
                    score += 2.0

            # 4. Category relevance match.
            if mem.category.casefold() in tokens:
                score += 3.0

            if score > 0.0:
                scored.append((score, mem))

        # Sort primarily by score descending, then by updated_at descending.
        scored.sort(
            key=lambda pair: (pair[0], pair[1].updated_at),
            reverse=True,
        )

        return tuple(mem for _, mem in scored[:limit])

    def close(self) -> None:
        """Close underlying store connection."""
        self._store.close()

    def __enter__(self) -> "MemoryManager":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()
