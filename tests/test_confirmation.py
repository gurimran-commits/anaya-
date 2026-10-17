from datetime import datetime, timedelta, timezone

from app.security.confirmation import ConfirmationManager


def test_matching_confirmation_is_approved():
    manager = ConfirmationManager()
    request = manager.request("shutdown", ("computer",))

    assert manager.approve(
        request.confirmation_id,
        "shutdown",
        ("computer",),
    )


def test_different_action_is_rejected():
    manager = ConfirmationManager()
    request = manager.request("shutdown", ("computer",))

    assert not manager.approve(
        request.confirmation_id,
        "delete_file",
        ("important.txt",),
    )


def test_different_arguments_are_rejected():
    manager = ConfirmationManager()
    request = manager.request("open_file", ("notes.txt",))

    assert not manager.approve(
        request.confirmation_id,
        "open_file",
        ("passwords.txt",),
    )


def test_confirmation_cannot_be_reused():
    manager = ConfirmationManager()
    request = manager.request("shutdown", ("computer",))

    assert manager.approve(
        request.confirmation_id,
        "shutdown",
        ("computer",),
    )
    assert not manager.approve(
        request.confirmation_id,
        "shutdown",
        ("computer",),
    )


def test_rejected_confirmation_cannot_be_reused():
    manager = ConfirmationManager()
    request = manager.request("shutdown", ("computer",))

    assert manager.reject(request.confirmation_id)
    assert not manager.approve(
        request.confirmation_id,
        "shutdown",
        ("computer",),
    )


def test_expired_confirmation_is_rejected():
    current_time = [datetime(2026, 1, 1, tzinfo=timezone.utc)]

    manager = ConfirmationManager(
        expiry_seconds=60,
        clock=lambda: current_time[0],
    )

    request = manager.request("shutdown", ("computer",))

    # Advance time beyond the 60-second expiry.
    current_time[0] += timedelta(seconds=61)

    assert not manager.approve(
        request.confirmation_id,
        "shutdown",
        ("computer",),
    )
