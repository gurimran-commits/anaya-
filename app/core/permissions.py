
from dataclasses import dataclass


@dataclass(frozen=True)
class PermissionDecision:
    allowed: bool
    reason: str


def check_internet_permission(internet_enabled: bool) -> PermissionDecision:
    """Allow internet use only when the user has enabled it."""
    if not internet_enabled:
        return PermissionDecision(
            allowed=False,
            reason="Internet access is disabled.",
        )

    return PermissionDecision(
        allowed=True,
        reason="Internet access is enabled.",
    )

