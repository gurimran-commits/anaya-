
from app.core.permissions import check_internet_permission


def test_internet_denied_by_default():
    decision = check_internet_permission(False)
    assert decision.allowed is False


def test_internet_allowed_when_enabled():
    decision = check_internet_permission(True)
    assert decision.allowed is True
