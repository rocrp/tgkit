"""One retry policy for Telegram calls.

Two shapes were copy-pasted across every send and edit path: absorbing a rate
limit with a backoff, and re-sending unformatted text after the API rejected
the formatting. Both live here now.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter

from ._log import logger
from .message_format import is_parse_error
from .pacing import Backoff, is_typed_retry_after, retry_after

DEFAULT_MAX_RETRIES = 3


class RateLimitError(RuntimeError):
    """Raised when Telegram rate limits without aiogram's typed error."""

    def __init__(self, retry_after: int | float, message: str | None = None) -> None:
        self.retry_after = retry_after
        super().__init__(message or f"Rate limited, retry after {retry_after} seconds")


def as_rate_limit(error: Exception) -> Exception | None:
    """Return the rate-limit error to re-raise, or None if this is not one."""
    if is_typed_retry_after(error) or isinstance(error, RateLimitError):
        return error
    if (delay := retry_after(error)) is not None:
        return RateLimitError(delay, str(error))
    return None


async def with_retry[**P, T](
    coro_fn: Callable[P, Awaitable[T]],
    *args: P.args,
    **kwargs: P.kwargs,
) -> T:
    """Execute an async Telegram call with the shared retry-after policy."""
    backoff = Backoff(base_interval=0, max_interval=30)
    for attempt in range(DEFAULT_MAX_RETRIES):
        try:
            return await coro_fn(*args, **kwargs)
        except (TelegramRetryAfter, RateLimitError) as error:
            if attempt == DEFAULT_MAX_RETRIES - 1:
                raise
            if not backoff.penalize(error):
                raise RuntimeError("TelegramRetryAfter missing retry_after") from error
            logger.warning(
                "telegram_rate_limit_retry",
                attempt=attempt + 1,
                max_retries=DEFAULT_MAX_RETRIES,
                retry_after=error.retry_after,
                wait_time=backoff.interval,
            )
            await asyncio.sleep(backoff.interval)
    raise RuntimeError("Max retries exceeded")


async def call_absorbing_rate_limits[T](
    call: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 4,
    event: str = "telegram_rate_limited_retrying",
) -> T:
    """Run ``call``, sleeping through rate limits until ``max_attempts`` is hit.

    Any other failure — including a parse rejection — propagates immediately so
    the caller can decide whether to degrade.
    """
    backoff = Backoff(base_interval=0, max_interval=30)
    for attempt in range(max_attempts):
        try:
            return await call()
        except Exception as error:
            if not isinstance(error, (TelegramAPIError, RateLimitError)):
                raise
            if attempt == max_attempts - 1 or not backoff.penalize(error):
                raise
            logger.warning(
                event,
                attempt=attempt + 1,
                retry_after=retry_after(error),
                wait_seconds=backoff.interval,
            )
            await asyncio.sleep(backoff.interval)
    raise RuntimeError("unreachable retry state")


async def send_or_plain[T](
    send: Callable[[str, str | None], Awaitable[T]],
    *,
    formatted: str,
    parse_mode: str | None,
    plain: str | None,
    reraise_rate_limit: bool = False,
    max_attempts: int = 4,
) -> tuple[T, bool]:
    """Send formatted text, re-sending it unformatted if the API rejects it.

    Returns the call result and whether the formatted attempt survived. Rate
    limits are retried in place — they say nothing about the formatting, so
    they must never trigger the plain retry.
    """

    async def attempt(text: str, mode: str | None) -> T:
        return await call_absorbing_rate_limits(lambda: send(text, mode), max_attempts=max_attempts)

    try:
        return await attempt(formatted, parse_mode), True
    except Exception as error:
        if not isinstance(error, (TelegramAPIError, RateLimitError)):
            raise
        if reraise_rate_limit and (rate_limit := as_rate_limit(error)) is not None:
            raise rate_limit from error
        if not parse_mode or plain is None or not is_parse_error(error):
            raise
        logger.warning("telegram.format_rejected_sending_plain", error=str(error))
        return await attempt(plain, None), False


def describe(error: BaseException) -> str:
    """Short, log-safe rendering of a failure."""
    return f"{type(error).__name__}: {error}"


# Only the two names bots call. call_absorbing_rate_limits, send_or_plain,
# as_rate_limit and describe are shared plumbing between delivery modules and
# are imported by module path, not re-exported from the package.
__all__ = [
    "RateLimitError",
    "with_retry",
]
