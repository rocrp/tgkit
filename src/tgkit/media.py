"""Local-file media preparation and delivery for Telegram.

Preparation resizes oversized images (animated frames included), gates every
file against a byte budget, and cleans up temporary copies. Delivery batches
photos into media groups and sends videos and documents individually.

Callers own the *policy* — the byte budget and how to surface rejections — so
nothing here writes to a chat on the user's behalf. Files that cannot be sent
come back as :class:`MediaRejection` records for the caller to render.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, InputMediaPhoto, Message
from PIL import Image, ImageOps, ImageSequence

from ._log import logger
from .constants import MAX_CAPTION_LENGTH, truncate_caption
from .markdown import escape_telegram_markdown
from .retry import call_absorbing_rate_limits

MAX_IMAGE_DIMENSION = 4000
"""Longest edge Telegram accepts for a photo before it rejects or re-encodes it."""

MAX_MEDIA_GROUP_SIZE = 10
"""Items per sendMediaGroup call."""

DEFAULT_MEDIA_MAX_BYTES = 20 * 1024 * 1024
"""Byte budget applied to media when the caller does not bring a policy of its own."""

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".avi", ".mkv", ".webm"})
AUDIO_SUFFIXES = frozenset({".mp3", ".ogg", ".oga", ".m4a", ".wav", ".flac"})

RejectionReason = Literal["too_large", "prepare_failed", "unreadable", "send_failed"]


@dataclass(frozen=True, slots=True)
class MediaRejection:
    """One file that could not be delivered, and why."""

    path: Path
    reason: RejectionReason
    detail: str

    def describe(self) -> str:
        """One-line, user-facing explanation."""
        match self.reason:
            case "too_large":
                return f"Skipped {self.path.name}: {self.detail}"
            case "prepare_failed":
                return f"Failed to prepare {self.path.name}: {self.detail}"
            case "unreadable":
                return f"Failed to read {self.path.name}: {self.detail}"
            case "send_failed":
                return f"Failed to send {self.path.name}: {self.detail}"


@dataclass(frozen=True, slots=True)
class ReadyFile:
    """A file cleared for upload, paired with the path the caller asked for."""

    source: Path
    upload: Path


@dataclass(slots=True)
class PreparedMedia:
    """Files cleared for upload, plus the ones that were dropped."""

    ready: list[ReadyFile] = field(default_factory=list)
    rejected: list[MediaRejection] = field(default_factory=list)

    @property
    def paths(self) -> list[Path]:
        """Upload-ready paths, in input order."""
        return [item.upload for item in self.ready]


@dataclass(slots=True)
class MediaSendResult:
    """What one :func:`send_media` call actually delivered."""

    messages: list[Message] = field(default_factory=list)
    caption_used: bool = False
    rejected: list[MediaRejection] = field(default_factory=list)


def classify_media(path: Path) -> Literal["photo", "video", "audio", "document"]:
    """Map a file suffix onto the Telegram media kind used to send it."""
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return "photo"
    if suffix in VIDEO_SUFFIXES:
        return "video"
    if suffix in AUDIO_SUFFIXES:
        return "audio"
    return "document"


# ---------------------------------------------------------------------------
# Image preparation
# ---------------------------------------------------------------------------


def _scale_dimensions(width: int, height: int, max_side: int) -> tuple[int, int]:
    scale = max_side / max(width, height)
    new_width = max(1, int(width * scale))
    new_height = max(1, int(height * scale))
    if max(new_width, new_height) > max_side:
        if new_width >= new_height:
            new_width = max_side
            new_height = max(1, int(height * (max_side / width)))
        else:
            new_height = max_side
            new_width = max(1, int(width * (max_side / height)))
    return new_width, new_height


def _ensure_mode_for_format(image: Image.Image, image_format: str) -> Image.Image:
    if image_format.upper() in {"JPEG", "JPG"} and image.mode not in {"RGB", "L"}:
        return image.convert("RGB")
    return image


def _resize_static(image: Image.Image, dest: Path, image_format: str, max_side: int) -> None:
    image = ImageOps.exif_transpose(image)
    width, height = image.size
    image = image.resize(_scale_dimensions(width, height, max_side), Image.Resampling.LANCZOS)
    _ensure_mode_for_format(image, image_format).save(dest, format=image_format)


def _resize_animated(image: Image.Image, dest: Path, image_format: str, max_side: int) -> None:
    frames: list[Image.Image] = []
    durations: list[int] = []
    loop_count = image.info.get("loop", 0)
    default_duration = image.info.get("duration", 0)

    for frame in ImageSequence.Iterator(image):
        frame = ImageOps.exif_transpose(frame)
        width, height = frame.size
        resized = frame.convert("RGBA").resize(
            _scale_dimensions(width, height, max_side), Image.Resampling.LANCZOS
        )
        match image_format:
            case "GIF":
                frames.append(resized.convert("P", palette=Image.Palette.ADAPTIVE))
            case "WEBP":
                frames.append(resized)
            case _:
                frames.append(_ensure_mode_for_format(resized, image_format))
        durations.append(frame.info.get("duration", default_duration))

    if not frames:
        raise RuntimeError(f"Animated image contained no frames: {dest.name}")

    save_kwargs: dict[str, Any] = {
        "format": image_format,
        "save_all": True,
        "append_images": frames[1:],
        "loop": loop_count,
    }
    if any(durations):
        save_kwargs["duration"] = durations
    frames[0].save(dest, **save_kwargs)


def _prepare_image_sync(path: Path, max_dimension: int) -> tuple[Path, Path | None]:
    """Return an upload-ready path plus the temp dir holding it, if one was made."""
    with Image.open(path) as image:
        width, height = image.size
        image_format = (image.format or path.suffix.lstrip(".") or "PNG").upper()
        if max(width, height) <= max_dimension:
            return path, None

        temp_dir = Path(tempfile.mkdtemp(prefix="tgkit_image_"))
        try:
            dest = temp_dir / path.name
            if getattr(image, "is_animated", False):
                _resize_animated(image, dest, image_format, max_dimension)
            else:
                _resize_static(image, dest, image_format, max_dimension)

            with Image.open(dest) as resized:
                new_width, new_height = resized.size
            if max(new_width, new_height) > max_dimension:
                raise RuntimeError(
                    f"Resize left {path.name} above {max_dimension}px ({new_width}x{new_height})"
                )
            logger.info(
                "telegram.image.resized",
                path=str(path),
                dest=str(dest),
                orig_width=width,
                orig_height=height,
                new_width=new_width,
                new_height=new_height,
            )
            return dest, temp_dir
        except Exception:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise


async def _cleanup(temp_dirs: Iterable[Path]) -> None:
    for temp_dir in temp_dirs:
        try:
            await asyncio.to_thread(shutil.rmtree, temp_dir, True)
        except Exception as error:  # noqa: BLE001 - cleanup must never mask the real result
            logger.warning("telegram.temp.cleanup_failed", path=str(temp_dir), error=str(error))


def _too_large(path: Path, size: int, max_bytes: int) -> MediaRejection:
    return MediaRejection(
        path=path,
        reason="too_large",
        detail=f"{size / 1024 / 1024:.1f}MB exceeds the {max_bytes // (1024 * 1024)}MB limit",
    )


@asynccontextmanager
async def prepared_images(
    paths: Sequence[Path],
    *,
    max_bytes: int,
    max_dimension: int = MAX_IMAGE_DIMENSION,
) -> AsyncIterator[PreparedMedia]:
    """Resize oversized images, gate on ``max_bytes``, and clean up on exit.

    Resized copies live in temporary directories that are removed when the
    context closes, so the upload must happen inside the ``async with`` body.
    """
    temp_dirs: list[Path] = []
    prepared = PreparedMedia()
    try:
        for path in paths:
            temp_dir: Path | None = None
            try:
                path_to_send, temp_dir = await asyncio.to_thread(_prepare_image_sync, path, max_dimension)
                size = (await asyncio.to_thread(path_to_send.stat)).st_size
                if size > max_bytes:
                    logger.warning(
                        "telegram.image.skipped_size",
                        path=str(path),
                        size=size,
                        max_bytes=max_bytes,
                    )
                    prepared.rejected.append(_too_large(path, size, max_bytes))
                    continue
                prepared.ready.append(ReadyFile(source=path, upload=path_to_send))
            except Exception as error:  # noqa: BLE001 - one bad image must not sink the batch
                logger.error("telegram.image.prepare_failed", path=str(path), error=str(error))
                prepared.rejected.append(
                    MediaRejection(path=path, reason="prepare_failed", detail=_brief(error))
                )
            finally:
                if temp_dir:
                    temp_dirs.append(temp_dir)
        yield prepared
    finally:
        await _cleanup(temp_dirs)


async def filter_by_size(paths: Sequence[Path], *, max_bytes: int) -> PreparedMedia:
    """Drop files above ``max_bytes`` without touching their contents."""
    prepared = PreparedMedia()
    for path in paths:
        try:
            size = (await asyncio.to_thread(path.stat)).st_size
        except OSError as error:
            logger.error("telegram.media.stat_failed", path=str(path), error=str(error))
            prepared.rejected.append(MediaRejection(path=path, reason="unreadable", detail=_brief(error)))
            continue
        if size > max_bytes:
            logger.warning("telegram.media.skipped_size", path=str(path), size=size, max_bytes=max_bytes)
            prepared.rejected.append(_too_large(path, size, max_bytes))
            continue
        prepared.ready.append(ReadyFile(source=path, upload=path))
    return prepared


async def read_input_file(path: Path) -> BufferedInputFile:
    """Read a local file into an aiogram ``BufferedInputFile``."""
    data = await asyncio.to_thread(path.read_bytes)
    return BufferedInputFile(data, filename=path.name)


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Caption:
    """A caption carried through sends until one of them consumes it."""

    text: str | None
    parse_mode: str | None

    @property
    def pending(self) -> bool:
        return bool(self.text)

    def take(self) -> tuple[str | None, str | None]:
        text, parse_mode = self.text, self.parse_mode
        self.text, self.parse_mode = None, None
        return text, parse_mode


def _build_caption(text: str | None, *, escape: bool) -> _Caption:
    """Fit ``text`` into a caption, escaping for MarkdownV2 after truncation.

    Escaping after truncating is what keeps the result inside the caption
    limit: escaping first and cutting afterwards can slice a backslash off its
    escaped character, and cutting after escaping is what pushes a caption back
    over the limit. Truncate the raw text against a reserve, then escape.
    """
    if not text:
        return _Caption(text=None, parse_mode=None)
    if not escape:
        return _Caption(text=truncate_caption(text), parse_mode=None)

    candidate = truncate_caption(text) or ""
    escaped = escape_telegram_markdown(candidate)
    while escaped and len(escaped) > MAX_CAPTION_LENGTH:
        # Escaping at most doubles length; shrink the source until the escaped
        # form fits rather than cutting the escaped text mid-sequence.
        candidate = candidate[: max(1, len(candidate) // 2)]
        escaped = escape_telegram_markdown(candidate)
    return _Caption(text=escaped, parse_mode=ParseMode.MARKDOWN_V2)


async def _send_with_caption_fallback(
    send: Any,
    *,
    caption: _Caption,
    plain_caption: str | None,
    **kwargs: Any,
) -> tuple[Message, bool]:
    """Send once with the formatted caption, once more plain if it was rejected."""
    caption_text, parse_mode = caption.take()
    try:
        message = await call_absorbing_rate_limits(
            lambda: send(caption=caption_text, parse_mode=parse_mode, **kwargs)
        )
        return message, bool(caption_text)
    except Exception as error:
        if not isinstance(error, TelegramAPIError) or not caption_text:
            raise
        logger.debug("telegram.media.caption_markdown_failed", error=str(error))
        fallback = truncate_caption(plain_caption) if plain_caption else None
        message = await call_absorbing_rate_limits(lambda: send(caption=fallback, parse_mode=None, **kwargs))
        return message, bool(fallback)


async def send_photos(
    bot: Bot,
    *,
    chat_id: int,
    paths: Sequence[Path],
    caption: _Caption | None = None,
    plain_caption: str | None = None,
    thread_id: int | None = None,
    reply_params: Any = None,
) -> MediaSendResult:
    """Send photos, batching into media groups of ten.

    Only the first item of the first batch carries the caption. A media group
    that the API rejects falls back to sending its items one by one.
    """
    caption = caption or _Caption(text=None, parse_mode=None)
    result = MediaSendResult()

    for start in range(0, len(paths), MAX_MEDIA_GROUP_SIZE):
        batch = paths[start : start + MAX_MEDIA_GROUP_SIZE]
        extra = _send_kwargs(thread_id=thread_id, reply_params=reply_params if start == 0 else None)

        if len(batch) == 1:
            path = batch[0]
            try:
                photo = await read_input_file(path)
            except OSError as error:
                logger.error("telegram.image.read_failed", path=str(path), error=str(error))
                result.rejected.append(MediaRejection(path=path, reason="unreadable", detail=_brief(error)))
                continue
            try:
                message, used = await _send_with_caption_fallback(
                    bot.send_photo,
                    caption=caption,
                    plain_caption=plain_caption,
                    chat_id=chat_id,
                    photo=photo,
                    **extra,
                )
            except Exception as error:
                if not isinstance(error, TelegramAPIError):
                    raise
                logger.error("telegram.image.send_failed", path=str(path), error=str(error))
                result.rejected.append(MediaRejection(path=path, reason="send_failed", detail=_brief(error)))
                continue
            result.messages.append(message)
            result.caption_used = result.caption_used or used
            continue

        group: list[Any] = []
        for index, path in enumerate(batch):
            try:
                photo = await read_input_file(path)
            except OSError as error:
                logger.error("telegram.image.read_failed", path=str(path), error=str(error))
                result.rejected.append(MediaRejection(path=path, reason="unreadable", detail=_brief(error)))
                continue
            if index == 0 and caption.pending:
                caption_text, parse_mode = caption.take()
                group.append(InputMediaPhoto(media=photo, caption=caption_text, parse_mode=parse_mode))
                result.caption_used = True
            else:
                group.append(InputMediaPhoto(media=photo))

        if not group:
            continue
        try:
            messages = await call_absorbing_rate_limits(
                lambda group=group, extra=extra: bot.send_media_group(chat_id=chat_id, media=group, **extra)
            )
            result.messages.extend(messages)
            logger.info("telegram.image.media_group_sent", count=len(group), batch_start=start)
        except Exception as error:
            if not isinstance(error, TelegramAPIError):
                raise
            logger.error("telegram.image.media_group_failed", error=str(error))
            for item in group:
                with suppress(TelegramAPIError):
                    result.messages.append(
                        await call_absorbing_rate_limits(
                            lambda item=item, extra=extra: bot.send_photo(
                                chat_id=chat_id, photo=item.media, **extra
                            )
                        )
                    )
    return result


async def send_videos(
    bot: Bot,
    *,
    chat_id: int,
    paths: Sequence[Path],
    caption: _Caption | None = None,
    plain_caption: str | None = None,
    thread_id: int | None = None,
    reply_params: Any = None,
) -> MediaSendResult:
    """Send videos one at a time with streaming enabled."""
    return await _send_files(
        bot,
        send=bot.send_video,
        media_kwarg="video",
        chat_id=chat_id,
        paths=paths,
        caption=caption,
        plain_caption=plain_caption,
        thread_id=thread_id,
        reply_params=reply_params,
        supports_streaming=True,
    )


async def send_documents(
    bot: Bot,
    *,
    chat_id: int,
    paths: Sequence[Path],
    caption: _Caption | None = None,
    plain_caption: str | None = None,
    thread_id: int | None = None,
    reply_params: Any = None,
) -> MediaSendResult:
    """Send documents one at a time (media groups do not mix types cleanly)."""
    return await _send_files(
        bot,
        send=bot.send_document,
        media_kwarg="document",
        chat_id=chat_id,
        paths=paths,
        caption=caption,
        plain_caption=plain_caption,
        thread_id=thread_id,
        reply_params=reply_params,
    )


async def _send_files(
    bot: Bot,
    *,
    send: Any,
    media_kwarg: str,
    chat_id: int,
    paths: Sequence[Path],
    caption: _Caption | None,
    plain_caption: str | None,
    thread_id: int | None,
    reply_params: Any,
    **send_extra: Any,
) -> MediaSendResult:
    caption = caption or _Caption(text=None, parse_mode=None)
    result = MediaSendResult()
    for index, path in enumerate(paths):
        try:
            payload = await read_input_file(path)
        except OSError as error:
            logger.error("telegram.media.read_failed", path=str(path), error=str(error))
            result.rejected.append(MediaRejection(path=path, reason="unreadable", detail=_brief(error)))
            continue
        extra = _send_kwargs(thread_id=thread_id, reply_params=reply_params if index == 0 else None)
        try:
            message, used = await _send_with_caption_fallback(
                send,
                caption=caption,
                plain_caption=plain_caption,
                chat_id=chat_id,
                **{media_kwarg: payload},
                **extra,
                **send_extra,
            )
        except Exception as error:
            if not isinstance(error, TelegramAPIError):
                raise
            logger.error("telegram.media.send_failed", path=str(path), error=str(error))
            result.rejected.append(MediaRejection(path=path, reason="send_failed", detail=_brief(error)))
            continue
        result.messages.append(message)
        result.caption_used = result.caption_used or used
        plain_caption = None
    return result


async def send_media(
    bot: Bot,
    *,
    chat_id: int,
    images: Sequence[Path] = (),
    videos: Sequence[Path] = (),
    documents: Sequence[Path] = (),
    caption: str | None = None,
    max_bytes: int,
    max_dimension: int = MAX_IMAGE_DIMENSION,
    thread_id: int | None = None,
    reply_params: Any = None,
) -> MediaSendResult:
    """Prepare and send every file, attaching ``caption`` to the first send.

    The caption rides along only when a single file is being sent and the text
    fits the caption limit; otherwise the caller keeps it for a separate
    message. Files that cannot be prepared or sent come back in ``rejected``.
    """
    result = MediaSendResult()
    async with prepared_images(images, max_bytes=max_bytes, max_dimension=max_dimension) as ready:
        result.rejected.extend(ready.rejected)
        video_batch = await filter_by_size(videos, max_bytes=max_bytes)
        document_batch = await filter_by_size(documents, max_bytes=max_bytes)
        result.rejected.extend(video_batch.rejected)
        result.rejected.extend(document_batch.rejected)

        total = len(ready.paths) + len(video_batch.paths) + len(document_batch.paths)
        use_caption = bool(caption) and total == 1 and len(caption or "") <= MAX_CAPTION_LENGTH
        pending = _build_caption(caption if use_caption else None, escape=True)
        plain = caption if use_caption else None

        for sender, batch in (
            (send_photos, ready.paths),
            (send_videos, video_batch.paths),
            (send_documents, document_batch.paths),
        ):
            if not batch:
                continue
            sent = await sender(
                bot,
                chat_id=chat_id,
                paths=batch,
                caption=pending,
                plain_caption=plain if pending.pending else None,
                thread_id=thread_id,
                reply_params=reply_params if not result.messages else None,
            )
            result.messages.extend(sent.messages)
            result.rejected.extend(sent.rejected)
            result.caption_used = result.caption_used or sent.caption_used
    return result


def _send_kwargs(*, thread_id: int | None, reply_params: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if thread_id is not None:
        kwargs["message_thread_id"] = thread_id
    if reply_params is not None:
        kwargs["reply_parameters"] = reply_params
    return kwargs


def _brief(error: BaseException) -> str:
    """Short failure reason for user-visible text; full detail stays in the log."""
    return f"{type(error).__name__}: {error}"[:120]


__all__ = [
    "AUDIO_SUFFIXES",
    "DEFAULT_MEDIA_MAX_BYTES",
    "IMAGE_SUFFIXES",
    "MAX_IMAGE_DIMENSION",
    "MAX_MEDIA_GROUP_SIZE",
    "VIDEO_SUFFIXES",
    "MediaRejection",
    "MediaSendResult",
    "PreparedMedia",
    "ReadyFile",
    "classify_media",
    "filter_by_size",
    "prepared_images",
    "send_documents",
    "send_media",
    "send_photos",
    "send_videos",
]
