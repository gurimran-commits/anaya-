
from app.security.approval_service import ApprovalService


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


def test_matching_request_can_be_approved_once():
    service = ApprovalService()
    request = service.request_approval("shutdown", ())

    assert service.approve(request) is True
    assert service.approve(request) is False


def test_rejected_request_cannot_be_approved():
    service = ApprovalService()
    request = service.request_approval("shutdown", ())

    assert service.reject(request) is True
    assert service.approve(request) is False


def test_approval_request_expires():
    from datetime import datetime, timedelta, timezone

    from app.security.confirmation import ConfirmationManager

    current_time = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    manager = ConfirmationManager(
        expiry_seconds=60,
        clock=lambda: current_time[0],
    )
    service = ApprovalService(manager)
    request = service.request_approval("shutdown", ())

    current_time[0] += timedelta(seconds=61)

    assert service.approve(request) is False
