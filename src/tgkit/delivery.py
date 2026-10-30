"""The degradation ladder: Rich Markdown -> MarkdownV2 -> plain.

One send enters at the top and walks down until a tier delivers. Each step
down is recorded, so the caller can see from the returned
:class:`DeliveryOutcome` that a message arrived as plain text and why.

The tiers do not share a length budget: Rich carries 32768 characters while
everything below it is capped at 4096 UTF-16 units, so a payload that was
valid as Rich is re-chunked on the way down. Overflow at the lower tiers is
resolved by the long-text fallback — split into several messages, or attach
the whole thing as a document.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, LinkPreviewOptions, Message, ReplyParameters

from ._log import logger
from .constants import (
    MAX_MESSAGE_LENGTH,
    MAX_RICH_MESSAGE_LENGTH,
    prefix_within_utf16_limit,
    rich_len,
    utf16_len,
)
from .markdown import format_for_telegram, split_message, unescape_telegram_markdown
from .message_format import is_parse_error
from .outcome import DeliveryOutcome, DeliveryTier, TierFailure
from .retry import call_absorbing_rate_limits, describe, send_or_plain
from .rich import RichPayload, is_rich_rejection, send_rich

LongTextFallback = Literal["split", "document"] | None

_CAPTION_PREVIEW_LIMIT = 1000


def link_preview_options(disabled: bool | None) -> LinkPreviewOptions | None:
    """Build ``LinkPreviewOptions``; ``disable_web_page_preview`` is deprecated."""
    if disabled is None:
        return None
    return LinkPreviewOptions(is_disabled=disabled)


def reply_parameters(message_id: int | None) -> ReplyParameters | None:
    """Build ``ReplyParameters``; ``reply_to_message_id`` is deprecated."""
    if message_id is None:
        return None
    return ReplyParameters(message_id=message_id)


async def reply_text(
    message: Message,
    text: str,
    *,
    tier: DeliveryTier | None = None,
    fallback: LongTextFallback = "document",
    disable_link_preview: bool | None = None,
) -> DeliveryOutcome:
    """Reply in the chat the message came from, walking the ladder."""
    bot = message.bot
    if not bot:
        raise RuntimeError("Bot instance missing on message")
    if not message.chat:
        raise RuntimeError("Chat missing on message")
    return await send_text(
        bot,
        chat_id=message.chat.id,
        text=text,
        reply_to_message_id=message.message_id,
        message_thread_id=getattr(message, "message_thread_id", None),
        tier=tier,
        fallback=fallback,
        disable_link_preview=disable_link_preview,
    )


async def send_text(
    bot: Bot,
    *,
    chat_id: int,
    text: str | RichPayload,
    tier: DeliveryTier | None = None,
    reply_to_message_id: int | None = None,
    message_thread_id: int | None = None,
    disable_notification: bool | None = None,
    disable_link_preview: bool | None = None,
    fallback: LongTextFallback = "document",
) -> DeliveryOutcome:
    """Deliver text at the highest tier that accepts it.

    ``tier`` pins the entry point: leave it ``None`` to start from Rich, or
    name a lower tier to skip the ones above it.
    """
    payload = text if isinstance(text, RichPayload) else RichPayload(markdown=text)
    if not payload.markdown:
        raise ValueError("text cannot be empty")

    failures: list[TierFailure] = []
    context = _SendContext(
        bot=bot,
        chat_id=chat_id,
        reply_to_message_id=reply_to_message_id,
        message_thread_id=message_thread_id,
        disable_notification=disable_notification,
        disable_link_preview=disable_link_preview,
        fallback=fallback,
    )

    if tier is None or tier == "rich":
        if not payload.fits():
            failures.append(
                TierFailure(
                    tier="rich",
                    reason=(f"{rich_len(payload.markdown)} chars over the {MAX_RICH_MESSAGE_LENGTH} rich limit"),
                )
            )
        else:
            try:
                message = await call_absorbing_rate_limits(
                    lambda: send_rich(
                        bot,
                        chat_id=chat_id,
                        markdown=payload,
                        thread_id=message_thread_id,
                        reply_params=reply_parameters(reply_to_message_id),
                        disable_notification=disable_notification,
                    )
                )
                return DeliveryOutcome(tier="rich", messages=[message], failures=failures)
            except Exception as error:
                if not is_rich_rejection(error):
                    raise
                failures.append(TierFailure(tier="rich", reason=describe(error)))
                _log_degradation("rich", "markdownv2", error)

    # Below the Rich tier the ``tg://`` references would render as dead links,
    # so the descent switches to the payload's alt-text form.
    if tier != "plain":
        try:
            return await _deliver_markdown_v2(context, payload.degraded_markdown, failures)
        except _TierRejected as rejection:
            failures.append(TierFailure(tier="markdownv2", reason=describe(rejection.error)))
            _log_degradation("markdownv2", "plain", rejection.error)

    return await _deliver_plain(context, payload.degraded_markdown, failures)


class _TierRejected(Exception):
    """Internal signal that a tier refused the payload and the ladder must descend."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        super().__init__(str(error))


class _SendContext:
    """The addressing and overflow policy shared by every tier of one send."""

    def __init__(
        self,
        *,
        bot: Bot,
        chat_id: int,
        reply_to_message_id: int | None,
        message_thread_id: int | None,
        disable_notification: bool | None,
        disable_link_preview: bool | None,
        fallback: LongTextFallback,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.reply_to_message_id = reply_to_message_id
        self.message_thread_id = message_thread_id
        self.disable_notification = disable_notification
        self.disable_link_preview = disable_link_preview
        self.fallback = fallback

    def send_kwargs(self, *, first: bool) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"chat_id": self.chat_id}
        if first and (params := reply_parameters(self.reply_to_message_id)) is not None:
            kwargs["reply_parameters"] = params
        if self.message_thread_id is not None:
            kwargs["message_thread_id"] = self.message_thread_id
        if self.disable_notification is not None:
            kwargs["disable_notification"] = self.disable_notification
        if (preview := link_preview_options(self.disable_link_preview)) is not None:
            kwargs["link_preview_options"] = preview
        return kwargs


async def _deliver_markdown_v2(
    context: _SendContext,
    raw_text: str,
    failures: list[TierFailure],
) -> DeliveryOutcome:
    """Tier 2: the legacy escape pipeline, re-chunked against the 4096 budget."""
    formatted = format_for_telegram(raw_text)
    chunks = _chunks_for_tier(context, formatted.text)
    if chunks is None:
        return await _deliver_document(context, raw_text, tier="markdownv2", failures=failures)

    messages: list[Message] = []
    degraded = False
    for index, chunk in enumerate(chunks):
        kwargs = context.send_kwargs(first=index == 0)
        send = lambda text, mode, kwargs=kwargs: context.bot.send_message(  # noqa: E731
            text=text, parse_mode=mode, **kwargs
        )
        try:
            # Before anything is on the wire, a formatting rejection belongs to
            # the ladder: descend and re-chunk as plain. Once part of the reply
            # has been delivered, descending would duplicate it, so the rest of
            # the chunks degrade individually instead.
            message, kept_format = await send_or_plain(
                send,
                formatted=chunk,
                parse_mode=formatted.parse_mode,
                plain=unescape_telegram_markdown(chunk) if messages else None,
            )
        except Exception as error:
            if not messages and _is_formatting_rejection(error):
                raise _TierRejected(error) from error
            raise
        messages.append(message)
        if not kept_format:
            degraded = True
            failures.append(TierFailure(tier="markdownv2", reason=f"chunk {index} sent unformatted"))
    # A reply that had to drop formatting mid-way is no longer a MarkdownV2
    # delivery, and saying so is the point of the outcome.
    return DeliveryOutcome(tier="plain" if degraded else "markdownv2", messages=messages, failures=failures)


async def _deliver_plain(
    context: _SendContext,
    raw_text: str,
    failures: list[TierFailure],
) -> DeliveryOutcome:
    """Tier 3: no parse mode at all. Plain means plain."""
    chunks = _chunks_for_tier(context, raw_text)
    if chunks is None:
        return await _deliver_document(context, raw_text, tier="plain", failures=failures)

    messages: list[Message] = []
    for index, chunk in enumerate(chunks):
        kwargs = context.send_kwargs(first=index == 0)
        messages.append(
            await call_absorbing_rate_limits(
                lambda chunk=chunk, kwargs=kwargs: context.bot.send_message(text=chunk, parse_mode=None, **kwargs)
            )
        )
    return DeliveryOutcome(tier="plain", messages=messages, failures=failures)


def _chunks_for_tier(context: _SendContext, text: str) -> list[str] | None:
    """Split ``text`` for a 4096-unit tier, or None when it must become a document."""
    if utf16_len(text) <= MAX_MESSAGE_LENGTH:
        return [text]
    match context.fallback:
        case "split":
            return split_message(text, max_len=MAX_MESSAGE_LENGTH)
        case "document":
            return None
        case None:
            raise ValueError(f"Text exceeds {MAX_MESSAGE_LENGTH} units and no long-text fallback was given")
    raise ValueError(f"unknown long-text fallback: {context.fallback}")


async def _deliver_document(
    context: _SendContext,
    text: str,
    *,
    tier: DeliveryTier,
    failures: list[TierFailure],
) -> DeliveryOutcome:
    """Attach the full text as a .txt file with a readable preview caption."""
    prefix = f"Text too long ({len(text)} chars), sent as .txt"
    remaining = _CAPTION_PREVIEW_LIMIT - utf16_len(prefix) - 1
    preview = text.strip()
    if utf16_len(preview) > remaining:
        preview = prefix_within_utf16_limit(preview, remaining - 1).rstrip() + "…"
    filename = f"message-{datetime.now(UTC).isoformat().replace(':', '-')}.txt"

    kwargs = context.send_kwargs(first=True)
    kwargs.pop("link_preview_options", None)  # documents have no link preview
    logger.info("telegram.long_text_fallback", strategy="document", chat_id=context.chat_id, length=len(text))
    message = await call_absorbing_rate_limits(
        lambda: context.bot.send_document(
            document=BufferedInputFile(text.encode(), filename=filename),
            caption=f"{prefix}\n{preview}",
            parse_mode=None,
            **kwargs,
        )
    )
    return DeliveryOutcome(tier=tier, messages=[message], failures=failures)


def _is_formatting_rejection(error: Exception) -> bool:
    return isinstance(error, TelegramAPIError) and is_parse_error(error)


def _log_degradation(from_tier: DeliveryTier, to_tier: DeliveryTier, error: Exception) -> None:
    logger.warning(
        "telegram.delivery_degraded",
        from_tier=from_tier,
        to_tier=to_tier,
        reason=describe(error),
    )


__all__ = [
    "LongTextFallback",
    "link_preview_options",
    "reply_parameters",
    "reply_text",
    "send_text",
]
