"""Small Telegram message, file, typing, and photo operations."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.enums import ChatAction
from aiogram.types import BufferedInputFile, FSInputFile, Message, ReplyParameters

from .constants import MARKDOWN_V2, MAX_CAPTION_LENGTH, truncate_text, utf16_len
from .markdown import escape_telegram_markdown
from .outcome import DeliveryOutcome, DeliveryTier, TierFailure
from .pacing import _TYPING_INTERVAL
from .retry import send_or_plain


def fit_caption(caption: str | None, *, parse_mode: str | None) -> str | None:
    """Fit a caption inside Telegram's limit, escaping *after* truncating.

    Escaping grows the text, so escaping first and cutting afterwards can slice
    a backslash away from the character it escapes, while cutting first without
    re-checking leaves the escaped form over the limit. Truncate the raw text,
    escape it, then shrink until the escaped form fits.
    """
    if not caption:
        return None
    fitted = truncate_text(caption, MAX_CAPTION_LENGTH)
    if parse_mode != MARKDOWN_V2:
        return fitted
    escaped = escape_telegram_markdown(fitted)
    while utf16_len(escaped) > MAX_CAPTION_LENGTH:
        fitted = truncate_text(fitted, max(4, utf16_len(fitted) // 2))
        escaped = escape_telegram_markdown(fitted)
    return escaped


async def reply_document(
    message: Message,
    content: str | bytes,
    filename: str,
    caption: str | None = None,
) -> DeliveryOutcome:
    """Attach in-memory content as a document."""
    encoded = content.encode() if isinstance(content, str) else content
    file = BufferedInputFile(encoded, filename=filename)
    return await _send_attachment(
        lambda **kwargs: message.reply_document(document=file, **kwargs), caption=caption
    )


async def reply_file(
    message: Message,
    file_path: Path,
    caption: str | None = None,
) -> DeliveryOutcome:
    """Attach a file from disk as a document."""
    if not file_path.exists():
        raise FileNotFoundError(f"cannot attach missing file: {file_path}")
    file = FSInputFile(file_path)
    return await _send_attachment(
        lambda **kwargs: message.reply_document(document=file, **kwargs), caption=caption
    )


async def send_typing(bot: Bot, chat_id: int, *, message_thread_id: int | None = None) -> None:
    """Show the typing indicator once; it lapses after about five seconds."""
    kwargs: dict[str, Any] = {"chat_id": chat_id, "action": ChatAction.TYPING}
    if message_thread_id is not None:
        kwargs["message_thread_id"] = message_thread_id
    with contextlib.suppress(Exception):
        await bot.send_chat_action(**kwargs)


@asynccontextmanager
async def keep_typing(
    bot: Bot,
    chat_id: int,
    *,
    thread_id: int | None = None,
) -> AsyncIterator[None]:
    """Hold the typing indicator up for as long as the block runs.

    Telegram's indicator lapses after about five seconds, so it is refreshed on
    a four-second beat. The refresher is cancelled and awaited on exit, and it
    never raises into the caller: a missing indicator is cosmetic.
    """

    async def refresh() -> None:
        try:
            while True:
                await send_typing(bot, chat_id, message_thread_id=thread_id)
                await asyncio.sleep(_TYPING_INTERVAL)
        except asyncio.CancelledError:
            pass

    task = asyncio.create_task(refresh(), name=f"keep_typing:{chat_id}")
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def send_photo(
    bot: Bot,
    *,
    chat_id: int,
    photo: Any,
    caption: str | None = None,
    parse_mode: str | None = MARKDOWN_V2,
    reply_to_message_id: int | None = None,
) -> DeliveryOutcome:
    extra: dict[str, Any] = {"chat_id": chat_id}
    if reply_to_message_id is not None:
        extra["reply_parameters"] = ReplyParameters(message_id=reply_to_message_id)
    return await _send_attachment(
        lambda **kwargs: bot.send_photo(photo=photo, **extra, **kwargs),
        caption=caption,
        parse_mode=parse_mode,
    )


async def reply_photo(
    message: Message,
    photo: Any,
    caption: str | None = None,
    parse_mode: str | None = MARKDOWN_V2,
) -> DeliveryOutcome:
    return await _send_attachment(
        lambda **kwargs: message.reply_photo(photo=photo, **kwargs),
        caption=caption,
        parse_mode=parse_mode,
    )


async def _send_attachment(
    send: Callable[..., Awaitable[Message]],
    *,
    caption: str | None,
    parse_mode: str | None = None,
) -> DeliveryOutcome:
    """Send an attachment, degrading the caption to plain text if rejected."""
    tier: DeliveryTier = "markdownv2" if parse_mode == MARKDOWN_V2 else "plain"
    formatted = fit_caption(caption, parse_mode=parse_mode)
    plain = fit_caption(caption, parse_mode=None)

    message, kept_format = await send_or_plain(
        lambda body, mode: send(caption=body or None, parse_mode=mode),
        formatted=formatted or "",
        parse_mode=parse_mode if formatted else None,
        plain=plain,
    )
    if kept_format:
        return DeliveryOutcome(tier=tier, messages=[message])
    return DeliveryOutcome(
        tier="plain",
        messages=[message],
        failures=[TierFailure(tier=tier, reason="caption sent unformatted after a parse rejection")],
    )


__all__ = [
    "fit_caption",
    "keep_typing",
    "reply_document",
    "reply_file",
    "reply_photo",
    "send_photo",
    "send_typing",
]
