"""Shared aiogram stand-ins for tgkit tests.

aiogram is a hard dependency, so tests raise its *real* exception types: the
delivery code dispatches on ``isinstance(error, TelegramAPIError)`` and a
look-alike would silently take the "unexpected error, re-raise" path.
"""

from __future__ import annotations

from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter


class _NoMethod:
    """Placeholder for the ``TelegramMethod`` aiogram attaches to its errors."""

    chat_id = None


def api_error(message: str) -> TelegramAPIError:
    """A real ``TelegramAPIError`` carrying ``message``.

    ``str(error)`` becomes ``"Telegram server says - <message>"``, exactly as
    aiogram renders it, so hint matching in the code under test sees what it
    would see in production.
    """
    return TelegramAPIError(method=_NoMethod(), message=message)  # type: ignore[arg-type]


def retry_after_error(seconds: int) -> TelegramRetryAfter:
    """aiogram's typed flood-control error."""
    return TelegramRetryAfter(method=_NoMethod(), message="Too Many Requests", retry_after=seconds)  # type: ignore[arg-type]


__all__ = ["api_error", "retry_after_error"]
