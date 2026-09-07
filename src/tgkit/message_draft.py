"""Low-level sendMessageDraft helpers."""

from __future__ import annotations

from enum import StrEnum

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from ._log import logger
from .constants import MAX_MESSAGE_LENGTH, truncate_text
from .message_format import prepare_text_for_send
from .retry import RateLimitError, as_rate_limit, send_or_plain


class DraftOutcome(StrEnum):
    SENT = "sent"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"

    @property
    def ok(self) -> bool:
        return self is DraftOutcome.SENT

    def __bool__(self) -> bool:
        raise TypeError("DraftOutcome has no truth value; use .ok or compare explicit members")


_DRAFT_UNSUPPORTED_HINTS = (
    "textdraft_peer_invalid",
    "forum topic mode",
    "forum_topic",
    "topic mode",
)
_DRAFT_UNSUPPORTED_METHOD_HINTS = (
    "message thread",
    "thread id",
    "topics",
    "forum",
    "topic",
)


def is_draft_unsupported_error(error: Exception) -> bool:
    message = str(error).lower()
    if any(hint in message for hint in _DRAFT_UNSUPPORTED_HINTS):
        return True
    return "sendmessagedraft" in message and any(hint in message for hint in _DRAFT_UNSUPPORTED_METHOD_HINTS)


async def send_message_draft_safe(
    bot: Bot,
    chat_id: int,
    draft_id: int,
    text: str,
    *,
    message_thread_id: int | None = None,
    parse_mode: str | None = None,
) -> DraftOutcome:
    """Send one draft update, distinguishing unsupported chats and failures."""
    original_text = text
    text, parse_mode = prepare_text_for_send(text, parse_mode=parse_mode, max_length=MAX_MESSAGE_LENGTH)
    try:
        await send_or_plain(
            lambda body, mode: bot.send_message_draft(
                chat_id=chat_id,
                draft_id=draft_id,
                text=body,
                message_thread_id=message_thread_id,
                parse_mode=mode,
            ),
            formatted=text,
            parse_mode=parse_mode,
            plain=truncate_text(original_text, MAX_MESSAGE_LENGTH),
            reraise_rate_limit=True,
        )
        return DraftOutcome.SENT
    except Exception as error:
        if not isinstance(error, (TelegramAPIError, RateLimitError)):
            raise
        if (rate_limit := as_rate_limit(error)) is not None:
            raise rate_limit from error
        if is_draft_unsupported_error(error):
            return DraftOutcome.UNSUPPORTED
        logger.warning("send_draft_failed", error=str(error))
        return DraftOutcome.FAILED


__all__ = [
    "DraftOutcome",
    "is_draft_unsupported_error",
    "send_message_draft_safe",
]
