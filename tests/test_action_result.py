
from app.core.action_result import ActionResult


def test_successful_action():
    result = ActionResult(success=True, message="Action completed.")
    assert result.success is True
    assert result.cancelled is False


def test_failed_action():
    result = ActionResult(success=False, message="Action failed.")
    assert result.success is False


def test_cancelled_action():
    result = ActionResult(
        success=False,
        message="Action cancelled by user.",
        cancelled=True,
    )
    assert result.cancelled is True
    assert result.success is False
