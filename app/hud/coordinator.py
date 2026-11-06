"""Thread-safe voice session coordinator with exact-identity lease management."""

from contextlib import contextmanager
import logging
import threading
import time
from typing import Generator

from app.hud.models import VoiceSessionSource

logger = logging.getLogger(__name__)


class VoiceLease:
    """Opaque, immutable lease token granting single-tenant audio input ownership."""

    def __init__(self, source: VoiceSessionSource) -> None:
        self._source = source
        self._acquired_at = time.monotonic()

    @property
    def source(self) -> VoiceSessionSource:
        """Input source that acquired this lease."""
        return self._source

    @property
    def acquired_at(self) -> float:
        """Monotonic timestamp recorded when this lease was acquired."""
        return self._acquired_at

    def __repr__(self) -> str:
        return f"<VoiceLease source={self._source.value} acquired_at={self._acquired_at:.3f}>"


class VoiceSessionCoordinator:
    """Thread-safe coordinator enforcing single-tenant microphone access.

    Guarantees that exactly one voice session is active across all input surfaces
    at any instant. Callers must acquire a unique VoiceLease before recording audio
    and return that exact lease to release the microphone.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active_lease: VoiceLease | None = None

    @property
    def is_active(self) -> bool:
        """True strictly while an active voice session lease is currently held."""
        with self._lock:
            return self._active_lease is not None

    @property
    def active_source(self) -> VoiceSessionSource | None:
        """Expose current tenant's source type without exposing the lease reference."""
        with self._lock:
            return self._active_lease.source if self._active_lease is not None else None

    def acquire(self, source: VoiceSessionSource) -> VoiceLease | None:
        """Atomically acquire the microphone lease.

        Non-blocking. Returns the unique VoiceLease instance if acquired;
        returns None immediately if another session is already active.
        """
        with self._lock:
            if self._active_lease is not None:
                logger.debug(
                    "Voice session acquisition rejected for %s: active tenant is %s",
                    source.value,
                    self._active_lease.source.value,
                )
                return None

            lease = VoiceLease(source)
            self._active_lease = lease
            logger.debug("Voice session acquired by %s", source.value)
            return lease

    def release(self, lease: VoiceLease | None) -> bool:
        """Atomically release the microphone lease if and only if 'lease is self._active_lease'.

        Uses exact object identity ('is'). Stale or foreign leases cannot release
        an active lease.
        """
        if lease is None:
            return False

        with self._lock:
            if self._active_lease is lease:
                self._active_lease = None
                logger.debug("Voice session released by %s", lease.source.value)
                return True

            logger.warning(
                "Stale or foreign VoiceLease rejected during release. "
                "Provided lease=%s, active lease=%s",
                lease,
                self._active_lease,
            )
            return False

    @contextmanager
    def session(self, source: VoiceSessionSource) -> Generator[VoiceLease | None, None, None]:
        """Exception-safe context manager yielding the lease or None if busy.

        Guarantees that any successfully acquired lease is released in a finally block.
        """
        lease = self.acquire(source)
        try:
            yield lease
        finally:
            if lease is not None:
                self.release(lease)
