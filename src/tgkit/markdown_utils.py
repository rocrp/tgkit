"""Markdown helper utilities."""

from __future__ import annotations

import re

from .constants import (
    MAX_MESSAGE_LENGTH,
    build_remaining_suffix,
    prefix_within_utf16_limit,
    utf16_len,
)


def strip_markdown(text: str) -> str:
    """Remove markdown formatting from text, preserving content."""
    if not text:
        return ""

    text = re.sub(r"```(?:[^\n`]*)?\n?([\s\S]*?)```", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\|\|(.+?)\|\|", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"(?<!\w)__(.+?)__(?!\w)", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"(?<!\w)\*\*(.+?)\*\*(?!\w)", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"(?<!\w)\*(.+?)\*(?!\w)", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"(?<!\w)~~(.+?)~~(?!\w)", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"(?<!\w)~(.+?)~(?!\w)", r"\1", text, flags=re.DOTALL)
    return text.strip()


def _format_truncated(truncated: str, ellipsis: str, original: str, show_remaining: bool) -> str:
    """Format truncated text with an optional dropped-character count.

    The count is measured in UTF-16 units, the same metric Telegram applies to
    the limit itself, and the suffix is built by the shared renderer.
    """
    if not show_remaining:
        return truncated + ellipsis
    remaining = utf16_len(original) - utf16_len(truncated)
    return f"{truncated}{build_remaining_suffix(remaining, ellipsis=ellipsis)}"


def _suffix_reserve(show_remaining: bool, original: str, ellipsis: str) -> int:
    """Room the dropped-character suffix will need, rather than a guessed 20."""
    if not show_remaining:
        return 0
    return utf16_len(build_remaining_suffix(utf16_len(original), ellipsis=ellipsis))


def truncate_text_by_paragraphs(
    text: str,
    max_len: int,
    *,
    ellipsis: str = "...",
    show_remaining: bool = True,
) -> str:
    """Truncate text on paragraph boundaries within max_len."""
    if not ellipsis:
        raise ValueError("ellipsis must not be empty")
    ellipsis_len = utf16_len(ellipsis)
    if max_len <= ellipsis_len:
        raise ValueError(f"max_len must be greater than {ellipsis_len}, got {max_len}")
    if utf16_len(text) <= max_len:
        return text

    cleaned = text.strip()
    if not cleaned:
        truncated = prefix_within_utf16_limit(text, max_len - ellipsis_len)
        return _format_truncated(truncated, ellipsis, text, show_remaining)

    paragraphs = re.split(r"\n{2,}", cleaned)
    out: list[str] = []
    total = 0
    suffix_reserve = _suffix_reserve(show_remaining, text, ellipsis)
    limit = max_len - ellipsis_len - suffix_reserve

    for para in paragraphs:
        if not para:
            continue
        candidate = para if not out else f"\n\n{para}"
        if total + utf16_len(candidate) > limit:
            break
        out.append(para)
        total += utf16_len(candidate)

    if out:
        truncated = "\n\n".join(out).rstrip()
        return _format_truncated(truncated, ellipsis, text, show_remaining)

    return truncate_text_by_lines(text, max_len, ellipsis=ellipsis, show_remaining=show_remaining)


def truncate_text_by_lines(
    text: str,
    max_len: int,
    *,
    ellipsis: str = "...",
    allow_partial_last_line: bool = False,
    show_remaining: bool = True,
) -> str:
    """Truncate text on line boundaries within max_len."""
    if not ellipsis:
        raise ValueError("ellipsis must not be empty")
    ellipsis_len = utf16_len(ellipsis)
    if max_len <= ellipsis_len:
        raise ValueError(f"max_len must be greater than {ellipsis_len}, got {max_len}")
    if utf16_len(text) <= max_len:
        return text

    cleaned = text.strip()
    if not cleaned:
        truncated = prefix_within_utf16_limit(text, max_len - ellipsis_len)
        return _format_truncated(truncated, ellipsis, text, show_remaining)

    lines = cleaned.splitlines()
    out: list[str] = []
    total = 0
    suffix_reserve = _suffix_reserve(show_remaining, text, ellipsis)
    limit = max_len - ellipsis_len - suffix_reserve

    for line in lines:
        if not line and not out:
            continue
        prefix = "" if not out else "\n"
        candidate = f"{prefix}{line}"
        if total + utf16_len(candidate) > limit:
            if allow_partial_last_line:
                space = limit - total - utf16_len(prefix)
                if space > 0:
                    out.append(prefix_within_utf16_limit(line, space).rstrip())
                truncated = "\n".join(out).rstrip()
                return _format_truncated(truncated, ellipsis, text, show_remaining)
            break
        out.append(line)
        total += utf16_len(candidate)

    if out:
        truncated = "\n".join(out).rstrip()
        return _format_truncated(truncated, ellipsis, text, show_remaining)

    truncated = prefix_within_utf16_limit(text, max_len - ellipsis_len - suffix_reserve)
    return _format_truncated(truncated, ellipsis, text, show_remaining)


def split_message(text: str, max_len: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Split a formatted message into chunks, keeping code fences balanced.

    The single splitter for the library: chunk boundaries never land inside a
    code fence, and an unbalanced fence is closed and reopened across the seam.
    Whitespace at a seam is dropped, so chunks rejoin readably but not byte for
    byte.
    """
    if max_len <= 0:
        raise ValueError(f"max_len must be positive, got {max_len}")
    if utf16_len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    remaining = text

    while remaining:
        if utf16_len(remaining) <= max_len:
            chunks.append(remaining)
            break

        split_at = _find_split_point(remaining, max_len)
        chunk = remaining[:split_at].rstrip()
        remaining = remaining[split_at:].lstrip()

        chunk, remaining = _handle_code_block_split(chunk, remaining)
        chunks.append(chunk)

    return chunks


def _find_split_point(text: str, max_len: int) -> int:
    """Find best split point within max_len."""
    prefix = prefix_within_utf16_limit(text, max_len)
    if not prefix:
        return 1

    half_limit = max_len // 2

    para_break = prefix.rfind("\n\n")
    if para_break >= 0 and utf16_len(prefix[: para_break + 2]) > half_limit:
        return para_break + 2

    line_break = prefix.rfind("\n")
    if line_break >= 0 and utf16_len(prefix[: line_break + 1]) > half_limit:
        return line_break + 1

    space = prefix.rfind(" ")
    if space >= 0 and utf16_len(prefix[: space + 1]) > half_limit:
        return space + 1

    return len(prefix)


def _handle_code_block_split(chunk: str, remaining: str) -> tuple[str, str]:
    """Handle code block continuity at split points."""
    fence_count = chunk.count("```")
    if fence_count % 2 == 1:
        last_fence = chunk.rfind("```")
        after_fence = chunk[last_fence + 3 :]
        newline_pos = after_fence.find("\n")
        lang = after_fence[:newline_pos].strip() if newline_pos != -1 else ""

        chunk = chunk + "\n```"
        remaining = f"```{lang}\n{remaining}"

    return chunk, remaining


# Keep for backwards compatibility with tests
ITALIC_STAR_PATTERN = re.compile(r"(?<!\*)\*(?!\*)([^\s*\n](?:[^*\n]*?[^\s*\n])?)\*(?!\*)")
