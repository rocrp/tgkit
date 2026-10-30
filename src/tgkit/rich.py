"""Bot API 10.x Rich Message assembly and sends.

Rich Messages carry a GitHub-flavoured Markdown document — headings, tables,
``$LaTeX$``, task lists, ``<details>``, footnotes — in a single 32768-character
payload, replacing the escape-and-squeeze work that ``parse_mode=MarkdownV2``
demands. This module owns payload assembly and the three transports:

* :func:`send_rich` — the durable message (``sendRichMessage``)
* :func:`send_rich_draft` — a 30-second ephemeral preview (``sendRichMessageDraft``)
* :func:`edit_rich` — in-place update (``editMessageText(rich_message=)``)

Reference: https://core.telegram.org/bots/api#rich-messages
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.types import (
    InputMediaAudio,
    InputMediaPhoto,
    InputMediaVideo,
    InputRichMessage,
    InputRichMessageMedia,
    LinkPreviewOptions,
    Message,
)

from ._log import logger
from .constants import (
    MAX_RICH_MEDIA,
    MAX_RICH_MESSAGE_LENGTH,
    rich_len,
)
from .markdown_fences import collapse_blank_lines
from .media import (
    MAX_IMAGE_DIMENSION,
    MediaRejection,
    classify_media,
    filter_by_size,
    prepared_images,
    read_input_file,
)
from .message_format import is_parse_error
from .pacing import is_typed_retry_after, retry_after

# Media identifiers are referenced from the markdown as tg://<kind>?id=<id>.
# Telegram allows 1-64 characters from this alphabet only.
RICH_MEDIA_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Only these three kinds can be embedded in rich markdown; documents stay as
# separate sends by design.
_INLINE_SCHEMES = {"photo": "tg://photo", "video": "tg://video", "audio": "tg://audio"}

# ![alt](target "optional title"), with CommonMark <...> wrapping supported so
# paths containing spaces survive. Public so consumers can find the embeds a
# payload will lift without re-deriving the grammar.
MEDIA_EMBED_RE = re.compile(
    r"!\[(?P<alt>[^\]\n]*)\]\(\s*"
    r"(?:<(?P<angle>[^>\n]*)>|(?P<bare>[^()\s]*(?:\([^()]*\)[^()\s]*)*))"
    r'(?:\s+"(?P<title>[^"\n]*)")?'
    r"\s*\)"
)

# The draft-only thinking block. Valid in sendRichMessageDraft and nowhere else.
THINKING_OPEN = "<tg-thinking>"
THINKING_CLOSE = "</tg-thinking>"

_RICH_REJECTION_HINTS = (
    "rich_message",
    "rich message",
    "richmessage",
    "too many blocks",
    "too many media",
    "too much nesting",
    "nesting",
    "block",
    "unsupported",
    "not supported",
)


class RichContractError(RuntimeError):
    """A Rich Message contract was violated before anything hit the network."""


@dataclass(slots=True)
class RichPayload:
    """One assembled Rich Message: markdown plus the media it references.

    ``markdown`` carries ``tg://`` references only the Rich tier understands;
    ``fallback_markdown`` is the same document with alt text in their place,
    for the tiers below. ``inline_paths`` are the upload-ready files behind
    ``media``; ``spill`` are the ones beyond the inline limit. Both lists feed
    the Media Tail — see :meth:`undelivered_media`.
    """

    markdown: str
    media: list[Any] = field(default_factory=list)
    rejected: list[MediaRejection] = field(default_factory=list)
    fallback_markdown: str = ""
    inline_paths: list[Path] = field(default_factory=list)
    spill: list[Path] = field(default_factory=list)

    @property
    def degraded_markdown(self) -> str:
        """The form for tiers that cannot carry inline media."""
        return self.fallback_markdown or self.markdown

    def undelivered_media(self, tier: str) -> list[Path]:
        """Paths the final message did not carry, given the tier that delivered.

        The spill never fit by definition; below the Rich tier the inline
        media did not make it either.
        """
        return list(self.spill) if tier == "rich" else [*self.inline_paths, *self.spill]

    def validate(self) -> None:
        """Check the limits that can be checked without parsing the document.

        Block count, nesting depth and table width are only knowable to the
        server; a payload that breaks them comes back as a rejection and the
        caller degrades. What is cheap to check locally is checked here so it
        fails before a round trip.
        """
        if not self.markdown:
            raise RichContractError("rich markdown cannot be empty")
        if (length := rich_len(self.markdown)) > MAX_RICH_MESSAGE_LENGTH:
            raise RichContractError(f"rich markdown is {length} characters, over the {MAX_RICH_MESSAGE_LENGTH} limit")
        if len(self.media) > MAX_RICH_MEDIA:
            raise RichContractError(f"rich message carries {len(self.media)} media, over the {MAX_RICH_MEDIA} limit")

    def fits(self) -> bool:
        """Whether the payload is within the locally checkable limits."""
        return bool(self.markdown) and rich_len(self.markdown) <= MAX_RICH_MESSAGE_LENGTH


def is_rich_rejection(error: Exception) -> bool:
    """Whether a failure means "this payload cannot be delivered as Rich".

    Rate limits are explicitly excluded: they are retried within the tier, and
    treating one as a rejection would degrade a message that Rich can carry
    perfectly well.
    """
    if is_typed_retry_after(error) or retry_after(error) is not None:
        return False
    if is_parse_error(error):
        return True
    message = str(error).lower()
    return any(hint in message for hint in _RICH_REJECTION_HINTS)


def _input_rich_message(payload: RichPayload) -> InputRichMessage:
    kwargs: dict[str, Any] = {"markdown": payload.markdown}
    if payload.media:
        kwargs["media"] = payload.media
    return InputRichMessage(**kwargs)


def _as_payload(markdown: str | RichPayload) -> RichPayload:
    return markdown if isinstance(markdown, RichPayload) else RichPayload(markdown=markdown)


async def send_rich(
    bot: Bot,
    *,
    chat_id: int,
    markdown: str | RichPayload,
    thread_id: int | None = None,
    reply_params: Any = None,
    disable_notification: bool | None = None,
    protect_content: bool | None = None,
) -> Message:
    """Send a durable Rich Message."""
    payload = _as_payload(markdown)
    payload.validate()
    if THINKING_OPEN in payload.markdown:
        raise RichContractError(f"{THINKING_OPEN} is valid only in drafts and must never reach sendRichMessage")

    kwargs: dict[str, Any] = {"chat_id": chat_id, "rich_message": _input_rich_message(payload)}
    if thread_id is not None:
        kwargs["message_thread_id"] = thread_id
    if reply_params is not None:
        kwargs["reply_parameters"] = reply_params
    if disable_notification is not None:
        kwargs["disable_notification"] = disable_notification
    if protect_content is not None:
        kwargs["protect_content"] = protect_content
    return await bot.send_rich_message(**kwargs)


async def send_rich_draft(
    bot: Bot,
    *,
    chat_id: int,
    draft_id: int,
    markdown: str | RichPayload,
    thread_id: int | None = None,
) -> bool:
    """Stream one ephemeral draft frame. Returns ``True``, never a ``Message``.

    Contract enforced here rather than merely documented:

    * private chats only — ``chat_id`` must be positive;
    * ``draft_id`` non-zero and stable for the life of one stream, so frames
      animate into one another instead of stacking;
    * no file uploads — drafts cannot carry new media;
    * a draft is a 30-second preview, so the caller MUST persist the result
      with :func:`send_rich` even when the last frame looked complete.
    """
    if chat_id <= 0:
        raise RichContractError(f"sendRichMessageDraft works in private chats only, got chat_id={chat_id}")
    if draft_id == 0:
        raise RichContractError("draft_id must be non-zero")

    payload = _as_payload(markdown)
    payload.validate()
    if payload.media:
        raise RichContractError("drafts cannot upload files; send media with the persisted message")

    kwargs: dict[str, Any] = {
        "chat_id": chat_id,
        "draft_id": draft_id,
        "rich_message": _input_rich_message(payload),
    }
    if thread_id is not None:
        kwargs["message_thread_id"] = thread_id
    return await bot.send_rich_message_draft(**kwargs)


async def edit_rich(
    bot: Bot,
    *,
    chat_id: int,
    message_id: int,
    markdown: str | RichPayload,
    disable_link_preview: bool | None = None,
) -> Any:
    """Replace an existing message's content with a Rich Message."""
    payload = _as_payload(markdown)
    payload.validate()
    if THINKING_OPEN in payload.markdown:
        raise RichContractError(f"{THINKING_OPEN} is valid only in drafts and must never reach editMessageText")
    kwargs: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "rich_message": _input_rich_message(payload),
    }
    if disable_link_preview is not None:
        kwargs["link_preview_options"] = LinkPreviewOptions(is_disabled=disable_link_preview)
    return await bot.edit_message_text(**kwargs)


# ---------------------------------------------------------------------------
# Inline media embedding
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Embed:
    """One ``![alt](local/path)`` occurrence found in the source markdown."""

    span: tuple[int, int]
    path: Path
    alt: str
    title: str | None
    kind: str


def _find_embeds(markdown: str, *, kinds: frozenset[str]) -> list[_Embed]:
    """Locate image embeds that point at inline-able local files."""
    embeds: list[_Embed] = []
    for match in MEDIA_EMBED_RE.finditer(markdown):
        raw = match.group("angle")
        target = (raw if raw is not None else match.group("bare") or "").strip()
        if not target or not target.startswith(("/", "~")):
            continue
        path = Path(target).expanduser()
        kind = classify_media(path)
        if kind not in kinds:
            continue
        embeds.append(
            _Embed(
                span=match.span(),
                path=path,
                alt=match.group("alt") or "",
                title=match.group("title"),
                kind=kind,
            )
        )
    return embeds


def _media_reference(media_id: str, *, kind: str, alt: str, title: str | None) -> str:
    """Render a media embed as its own block.

    Telegram parses media only as a standalone block, so the reference is
    surrounded by blank lines; :func:`collapse_blank_lines` tidies the result.
    """
    caption = f' "{title}"' if title else ""
    return f"\n\n![{alt}]({_INLINE_SCHEMES[kind]}?id={media_id}{caption})\n\n"


def _input_media(kind: str, upload: Any) -> InputMediaPhoto | InputMediaVideo | InputMediaAudio:
    match kind:
        case "photo":
            return InputMediaPhoto(media=upload)
        case "video":
            return InputMediaVideo(media=upload)
        case "audio":
            return InputMediaAudio(media=upload)
    raise RichContractError(f"{kind} cannot be embedded in a rich message")


@asynccontextmanager
async def build_rich_payload(
    markdown: str,
    *,
    max_bytes: int,
    max_dimension: int = MAX_IMAGE_DIMENSION,
    inline_kinds: frozenset[str] = frozenset(_INLINE_SCHEMES),
) -> AsyncIterator[RichPayload]:
    """Turn markdown with local media links into an upload-ready Rich payload.

    Each ``![alt](/abs/path)`` embed becomes an ``InputRichMessageMedia`` entry
    and its link is rewritten to ``tg://photo?id=…`` in place, so the media
    renders exactly where it sat in the text. ``inline_kinds`` narrows which
    kinds are lifted; embeds of other kinds stay untouched in the markdown.
    Files that cannot be prepared are dropped from the markdown and reported
    in ``payload.rejected``; embeds beyond the inline limit land in
    ``payload.spill`` for the caller's Media Tail.

    Resized copies live in temporary directories removed when the context
    closes, so the send — including any tail send — must happen inside the
    ``async with`` body.
    """
    embeds = _find_embeds(markdown, kinds=inline_kinds)
    if not embeds:
        yield RichPayload(markdown=markdown)
        return

    images = [embed for embed in embeds if embed.kind == "photo"]
    others = [embed for embed in embeds if embed.kind != "photo"]

    async with prepared_images(
        [embed.path for embed in images], max_bytes=max_bytes, max_dimension=max_dimension
    ) as ready_images:
        ready_others = await filter_by_size([embed.path for embed in others], max_bytes=max_bytes)
        uploads = {item.source: item.upload for item in (*ready_images.ready, *ready_others.ready)}
        rejected = [*ready_images.rejected, *ready_others.rejected]

        payload = RichPayload(markdown="", rejected=rejected)
        pieces: list[str] = []
        fallback_pieces: list[str] = []
        cursor = 0
        for index, embed in enumerate(embeds):
            start, end = embed.span
            pieces.append(markdown[cursor:start])
            fallback_pieces.append(markdown[cursor:start])
            cursor = end

            upload_path = uploads.get(embed.path)
            if upload_path is None:
                # Dropped during preparation — keep the alt text, lose the embed.
                pieces.append(embed.alt)
                fallback_pieces.append(embed.alt)
                continue
            # Below the Rich tier no embed survives; every prepared file rides
            # the Media Tail there, so the fallback form always shows alt text.
            fallback_pieces.append(embed.alt)
            if len(payload.media) >= MAX_RICH_MEDIA:
                logger.info(
                    "telegram.rich.media_spilled",
                    limit=MAX_RICH_MEDIA,
                    path=str(embed.path),
                )
                payload.spill.append(upload_path)
                pieces.append(embed.alt)
                continue

            media_id = f"m{index}"
            if not RICH_MEDIA_ID_PATTERN.match(media_id):
                raise RichContractError(f"generated media id {media_id!r} is not acceptable")
            payload.media.append(
                InputRichMessageMedia(
                    id=media_id,
                    media=_input_media(embed.kind, await read_input_file(upload_path)),
                )
            )
            payload.inline_paths.append(upload_path)
            pieces.append(_media_reference(media_id, kind=embed.kind, alt=embed.alt, title=embed.title))
        pieces.append(markdown[cursor:])
        fallback_pieces.append(markdown[cursor:])

        payload.markdown = collapse_blank_lines("".join(pieces)).strip()
        payload.fallback_markdown = collapse_blank_lines("".join(fallback_pieces)).strip()
        yield payload


__all__ = [
    "MEDIA_EMBED_RE",
    "RICH_MEDIA_ID_PATTERN",
    "THINKING_CLOSE",
    "THINKING_OPEN",
    "RichContractError",
    "RichPayload",
    "build_rich_payload",
    "edit_rich",
    "is_rich_rejection",
    "send_rich",
    "send_rich_draft",
]
