"""Update pacing for live Telegram views: intervals and adaptive backoff."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from aiogram.exceptions import TelegramRetryAfter

# Internal defaults
_MIN_UPDATE_INTERVAL = 1.5
# Groups share a ~20 API-calls/min per-chat flood budget and edits count toward
# it; pace group edits so a full turn (ticker + per-event updates) stays under.
_GROUP_MIN_UPDATE_INTERVAL = 5.0
_MAX_UPDATE_INTERVAL = 30.0
_RECOVERY_FACTOR = 0.85
_RATE_LIMIT_PADDING = 0.25
_ELAPSED_TICK_SECONDS = 8.0
_TYPING_INTERVAL = 4.0


_RETRY_AFTER_PATTERN = re.compile(r"(?:retry after|retry in)\s+(\d+(?:\.\d+)?)", re.IGNORECASE)


def retry_after(error: object) -> float | None:
    """Extract Telegram's requested delay from typed or textual errors."""
    value = getattr(error, "retry_after", None)
    if isinstance(value, int | float) and value >= 0:
        return float(value)
    if match := _RETRY_AFTER_PATTERN.search(str(error)):
        return float(match.group(1))
    return None


def is_typed_retry_after(error: object) -> bool:
    """Return whether aiogram supplied its dedicated retry-after error."""
    return isinstance(error, TelegramRetryAfter)


@dataclass(slots=True)
class Backoff:
    """Single adaptive Telegram update-rate policy."""

    base_interval: float = _MIN_UPDATE_INTERVAL
    max_interval: float = _MAX_UPDATE_INTERVAL
    padding: float = _RATE_LIMIT_PADDING
    recovery_factor: float = _RECOVERY_FACTOR
    interval: float = 0.0

    def __post_init__(self) -> None:
        if self.base_interval < 0:
            raise ValueError("base_interval must be >= 0")
        if self.max_interval < self.base_interval:
            raise ValueError("max_interval must be >= base_interval")
        self.interval = self.base_interval

    def penalize(self, error: object) -> bool:
        """Apply a retry-after error; return False when it is not a rate limit."""
        if (delay := retry_after(error)) is None:
            return False
        self.interval = min(self.max_interval, delay + self.padding)
        return True

    def recover(self) -> None:
        self.interval = max(self.base_interval, self.interval * self.recovery_factor)

    def remaining(self, *, last_update: float, now: float, padding: float = 0.0) -> float:
        if padding < 0:
            raise ValueError("padding must be >= 0")
        return max(0.0, self.interval - (now - last_update) + padding)


def _now() -> float:
    return time.monotonic()
