"""Unit tests for VoiceSessionCoordinator and VoiceLease identity management."""

import threading

from app.hud.coordinator import VoiceLease, VoiceSessionCoordinator
from app.hud.models import VoiceSessionSource


def test_coordinator_initial_state() -> None:
    """Verify coordinator starts in an unacquired, inactive state."""
    coord = VoiceSessionCoordinator()
    assert not coord.is_active
    assert coord.active_source is None


def test_coordinator_acquire_returns_valid_lease() -> None:
    """Verify successful acquisition returns a valid lease and updates state."""
    coord = VoiceSessionCoordinator()
    lease = coord.acquire(VoiceSessionSource.HUD_BUTTON)

    assert isinstance(lease, VoiceLease)
    assert lease.source == VoiceSessionSource.HUD_BUTTON
    assert lease.acquired_at > 0
    assert coord.is_active
    assert coord.active_source == VoiceSessionSource.HUD_BUTTON


def test_coordinator_concurrent_acquisition_fails() -> None:
    """Verify non-blocking acquisition returns None if another session is active."""
    coord = VoiceSessionCoordinator()
    lease1 = coord.acquire(VoiceSessionSource.HUD_BUTTON)
    assert lease1 is not None

    lease2 = coord.acquire(VoiceSessionSource.GLOBAL_HOTKEY)
    assert lease2 is None
    assert coord.active_source == VoiceSessionSource.HUD_BUTTON


def test_coordinator_release_with_matching_lease_succeeds() -> None:
    """Verify release with the exact active lease succeeds and resets state."""
    coord = VoiceSessionCoordinator()
    lease = coord.acquire(VoiceSessionSource.SYSTEM_TRAY)
    assert lease is not None

    released = coord.release(lease)
    assert released is True
    assert not coord.is_active
    assert coord.active_source is None


def test_coordinator_stale_lease_rejected() -> None:
    """Verify a stale lease cannot release a newer active session."""
    coord = VoiceSessionCoordinator()
    lease1 = coord.acquire(VoiceSessionSource.HUD_BUTTON)
    assert lease1 is not None
    assert coord.release(lease1) is True

    # New session starts
    lease2 = coord.acquire(VoiceSessionSource.HUD_BUTTON)
    assert lease2 is not None

    # Stale lease1 attempts to release
    assert coord.release(lease1) is False
    assert coord.is_active
    assert coord.active_source == VoiceSessionSource.HUD_BUTTON

    # Clean release of lease2 succeeds
    assert coord.release(lease2) is True
    assert not coord.is_active


def test_coordinator_foreign_lease_rejected() -> None:
    """Verify an independently created lease object is rejected by exact identity."""
    coord = VoiceSessionCoordinator()
    lease = coord.acquire(VoiceSessionSource.HUD_BUTTON)
    assert lease is not None

    foreign_lease = VoiceLease(VoiceSessionSource.HUD_BUTTON)
    assert coord.release(foreign_lease) is False
    assert coord.is_active

    assert coord.release(lease) is True
    assert not coord.is_active


def test_coordinator_release_none_returns_false() -> None:
    """Verify passing None to release safely returns False without raising."""
    coord = VoiceSessionCoordinator()
    assert coord.release(None) is False


def test_coordinator_context_manager_normal() -> None:
    """Verify context manager automatically releases the lease on normal exit."""
    coord = VoiceSessionCoordinator()
    with coord.session(VoiceSessionSource.HUD_BUTTON) as lease:
        assert lease is not None
        assert coord.is_active

    assert not coord.is_active


def test_coordinator_context_manager_on_exception() -> None:
    """Verify context manager guarantees release even if an exception occurs."""
    coord = VoiceSessionCoordinator()
    try:
        with coord.session(VoiceSessionSource.GLOBAL_HOTKEY) as lease:
            assert lease is not None
            assert coord.is_active
            raise RuntimeError("Audio device crashed")
    except RuntimeError:
        pass

    assert not coord.is_active


def test_coordinator_multithreaded_race() -> None:
    """Deterministic barrier test: two threads race to acquire; exactly one wins."""
    coord = VoiceSessionCoordinator()
    barrier = threading.Barrier(2)
    results: list[VoiceLease | None] = [None, None]

    def worker(idx: int, source: VoiceSessionSource) -> None:
        barrier.wait()
        results[idx] = coord.acquire(source)

    t1 = threading.Thread(target=worker, args=(0, VoiceSessionSource.HUD_BUTTON))
    t2 = threading.Thread(target=worker, args=(1, VoiceSessionSource.GLOBAL_HOTKEY))

    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Exactly one lease acquired, one None
    successes = [r for r in results if r is not None]
    fails = [r for r in results if r is None]
    assert len(successes) == 1
    assert len(fails) == 1

    # Cleanup
    assert coord.release(successes[0]) is True
    assert not coord.is_active
