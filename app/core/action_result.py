
from dataclasses import dataclass


@dataclass(frozen=True)
class ActionResult:
    success: bool
    message: str
    cancelled: bool = False
