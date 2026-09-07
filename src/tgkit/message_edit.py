"""Telegram message edit operations."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import LinkPreviewOptions

from ._log import logger
from .constants import MARKDOWN_V2, MAX_MESSAGE_LENGTH, truncate_text
from .message_format import prepare_text_for_send
from .retry import RateLimitError, as_rate_limit, call_absorbing_rate_limits, send_or_plain

__all__ = [
    "EditMessageOutcome",
    "RateLimitError",
    "is_not_modified",
    "delete_message_safe",
    "edit_message_safe",
    "edit_message_text_raw",
]


class EditMessageOutcome(StrEnum):
    EDITED = "edited"
    NOT_MODIFIED = "not_modified"
    FAILED = "failed"

    @property
    def ok(self) -> bool:
        return self is not EditMessageOutcome.FAILED

    def __bool__(self) -> bool:
        raise TypeError("EditMessageOutcome has no truth value; use .ok or compare explicit members")


def _link_preview(disabled: bool | None) -> Any:
    if disabled is None:
        return None
    return LinkPreviewOptions(is_disabled=disabled)


def is_not_modified(error: Exception) -> bool:
    """Whether Telegram refused an edit because the content already matches.

    Not a failure: the message on screen is already what we wanted to show.
    """
    return "message is not modified" in str(error).lower()


_is_not_modified = is_not_modified


async def edit_message_safe(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    parse_mode: str | None = MARKDOWN_V2,
    *,
    reraise_rate_limit: bool = False,
    disable_link_preview: bool | None = None,
) -> EditMessageOutcome:
    """Edit a message, degrading to plain text if the formatting is rejected."""
    original_text = text
    text, parse_mode = prepare_text_for_send(text, parse_mode=parse_mode, max_length=MAX_MESSAGE_LENGTH)
    kwargs: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id}
    if (preview := _link_preview(disable_link_preview)) is not None:
        kwargs["link_preview_options"] = preview

    try:
        await send_or_plain(
            lambda body, mode: bot.edit_message_text(text=body, parse_mode=mode, **kwargs),
            formatted=text,
            parse_mode=parse_mode,
            plain=truncate_text(original_text, MAX_MESSAGE_LENGTH),
            reraise_rate_limit=reraise_rate_limit,
        )
        return EditMessageOutcome.EDITED
    except Exception as error:
        if not isinstance(error, (TelegramAPIError, RateLimitError)):
            raise
        if reraise_rate_limit and as_rate_limit(error) is not None:
            raise
        if _is_not_modified(error):
            return EditMessageOutcome.NOT_MODIFIED
        logger.warning("failed to edit message", error=str(error))
        return EditMessageOutcome.FAILED


async def edit_message_text_raw(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    parse_mode: str | None,
    *,
    fallback_text: str | None = None,
    disable_link_preview: bool | None = None,
) -> EditMessageOutcome:
    """Edit an already formatted payload, retrying rate limits and plain text."""
    kwargs: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id}
    if (preview := _link_preview(disable_link_preview)) is not None:
        kwargs["link_preview_options"] = preview

    try:
        await send_or_plain(
            lambda body, mode: bot.edit_message_text(text=body, parse_mode=mode, **kwargs),
            formatted=text,
            parse_mode=parse_mode,
            plain=truncate_text(fallback_text, MAX_MESSAGE_LENGTH) if fallback_text else None,
        )
        return EditMessageOutcome.EDITED
    except Exception as error:
        if not isinstance(error, (TelegramAPIError, RateLimitError)):
            raise
        if _is_not_modified(error):
            return EditMessageOutcome.NOT_MODIFIED
        logger.warning("failed to edit message", error=str(error))
        return EditMessageOutcome.FAILED


async def delete_message_safe(bot: Bot, chat_id: int, message_id: int) -> bool:
    try:
        await call_absorbing_rate_limits(lambda: bot.delete_message(chat_id=chat_id, message_id=message_id))
        return True
    except Exception as error:
        if isinstance(error, (TelegramAPIError, RateLimitError)):
            logger.warning("failed to delete message", error=str(error))
            return False
        raise
