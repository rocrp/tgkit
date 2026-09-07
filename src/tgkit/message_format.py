"""Shared formatting for Telegram message sends and edits."""

from __future__ import annotations

import re

from aiogram.enums import ParseMode

from ._log import logger
from .constants import truncate_markdown_v2, truncate_text, utf16_len
from .markdown import format_for_telegram


def prepare_text_for_send(
    text: str,
    *,
    parse_mode: str | None,
    max_length: int | None = None,
) -> tuple[str, str | None]:
    """Normalize parse mode and format/escape MarkdownV2 when requested."""
    raw_text = text

    # parse_mode is taken literally: None means plain text, never MarkdownV2.
    if parse_mode == ParseMode.MARKDOWN_V2:
        formatted = format_for_telegram(text)
        text, parse_mode = formatted.text, formatted.parse_mode
        if max_length is not None and utf16_len(text) > max_length:
            fallback_text = truncate_text(raw_text, limit=max_length, show_remaining=True)
            _log_truncation(fallback_text, original=text, max_length=max_length, markdown=False)
            return fallback_text, None

    if max_length is not None:
        original = text
        is_markdown = parse_mode == ParseMode.MARKDOWN_V2
        text = (
            truncate_markdown_v2(text, limit=max_length, show_remaining=True)
            if is_markdown
            else truncate_text(text, limit=max_length, show_remaining=True)
        )
        if utf16_len(original) > max_length:
            _log_truncation(text, original=original, max_length=max_length, markdown=is_markdown)
    return text, parse_mode


def is_parse_error(error: Exception) -> bool:
    message = str(error).lower()
    return "parse" in message or "markdown" in message


def _log_truncation(text: str, *, original: str, max_length: int, markdown: bool) -> None:
    pattern = r"\\\[(\d+) chars\\\]\\\.\\\.\\\.$" if markdown else r"\[(\d+) chars\]\.\.\.$"
    match = re.search(pattern, text)
    logger.debug(
        "text_truncated",
        original_units=utf16_len(original),
        max_length=max_length,
        remaining=int(match.group(1)) if match else None,
        markdown=markdown,
    )
