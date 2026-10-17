from dataclasses import replace
from datetime import datetime, timedelta, timezone

from app.security.approval_service import ApprovalService
from app.security.confirmation import ConfirmationManager


def test_request_contains_exact_action_and_arguments():
    service = ApprovalService()

    request = service.request_approval(
        "delete_file",
        ("notes.txt",),
    )

    assert request.action == "delete_file"
    assert request.arguments == ("notes.txt",)
    assert "delete_file" in request.message
    assert "notes.txt" in request.message
    assert service.get_request(request.confirmation_id) == request


def test_approval_creates_one_time_authorization():
    service = ApprovalService()
    request = service.request_approval("shutdown", ())

    assert service.approve(request) is True
    assert service.approve(request) is False

    assert service.consume_authorization(
        request.confirmation_id,
        "shutdown",
        (),
    ) is True

    assert service.consume_authorization(
        request.confirmation_id,
        "shutdown",
        (),
    ) is False


def test_unapproved_request_cannot_authorize_execution():
    service = ApprovalService()
    request = service.request_approval("shutdown", ())

    assert service.consume_authorization(
        request.confirmation_id,
        "shutdown",
        (),
    ) is False


def test_rejected_request_cannot_be_approved():
    service = ApprovalService()
    request = service.request_approval("shutdown", ())

    assert service.reject(request) is True
    assert service.approve(request) is False

    assert service.consume_authorization(
        request.confirmation_id,
        "shutdown",
        (),
    ) is False


def test_altered_request_cannot_be_approved():
    service = ApprovalService()
    request = service.request_approval(
        "delete_file",
        ("notes.txt",),
    )

    altered_request = replace(
        request,
        arguments=("passwords.txt",),
    )

    assert service.approve(altered_request) is False
    assert service.consume_authorization(
        request.confirmation_id,
        "delete_file",
        ("passwords.txt",),
    ) is False

    # The original request remains available for a legitimate decision.
    assert service.approve(request) is True


def test_authorization_is_bound_to_exact_arguments():
    service = ApprovalService()
    request = service.request_approval(
        "delete_file",
        ("notes.txt",),
    )

    assert service.approve(request) is True

    # A mismatched attempt consumes the authorization safely.
    assert service.consume_authorization(
        request.confirmation_id,
        "delete_file",
        ("important.txt",),
    ) is False

    assert service.consume_authorization(
        request.confirmation_id,
        "delete_file",
        ("notes.txt",),
    ) is False


def test_approval_request_expires():
    current_time = [
        datetime(2026, 1, 1, tzinfo=timezone.utc)
    ]

    manager = ConfirmationManager(
        expiry_seconds=60,
        clock=lambda: current_time[0],
    )
    service = ApprovalService(manager)

    request = service.request_approval("shutdown", ())

    current_time[0] += timedelta(seconds=61)

    assert service.approve(request) is False


def test_authorization_expires_at_original_deadline():
    current_time = [
        datetime(2026, 1, 1, tzinfo=timezone.utc)
    ]

    manager = ConfirmationManager(
        expiry_seconds=60,
        clock=lambda: current_time[0],
    )
    service = ApprovalService(manager)

    request = service.request_approval("shutdown", ())

    assert service.approve(request) is True

    current_time[0] += timedelta(seconds=61)

    assert service.consume_authorization(
        request.confirmation_id,
        "shutdown",
        (),
    ) is False