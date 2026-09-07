"""Telegram API constants."""

# aiogram's ParseMode.MARKDOWN_V2 value, spelled here so modules can default to
# it without importing aiogram. A parse_mode of None means plain text.
MARKDOWN_V2 = "MarkdownV2"

# Message limits, measured in UTF-16 code units (see utf16_len).
MAX_MESSAGE_LENGTH = 4096
MAX_CAPTION_LENGTH = 1024

# Rich Message limits (Bot API 10.x, https://core.telegram.org/bots/api#rich-messages).
# The text budget is documented in *characters*, not UTF-16 code units — use
# rich_len, not utf16_len, when measuring a rich payload.
MAX_RICH_MESSAGE_LENGTH = 32768
MAX_RICH_BLOCKS = 500
MAX_RICH_NESTING_DEPTH = 16
MAX_RICH_MEDIA = 50
MAX_RICH_TABLE_COLUMNS = 20
MARKDOWN_V2_ELLIPSIS = r"\.\.\."
_MARKDOWN_V2_SPECIALS = frozenset(
    ["_", "*", "[", "]", "(", ")", "~", "`", ">", "#", "+", "=", "|", "{", "}", ".", "!", "-", "\\"]
)


def utf16_len(text: str) -> int:
    """Return Telegram-style text length in UTF-16 code units."""
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def rich_len(text: str) -> int:
    """Return Rich Message text length.

    Rich Messages budget 32768 *characters* — unlike the classic 4096-unit text
    limit, which counts UTF-16 code units. Keeping the two metrics as separate
    named functions stops one from being "corrected" into the other.
    """
    return len(text)


def prefix_within_utf16_limit(text: str, limit: int) -> str:
    """Return the longest prefix whose UTF-16 length is <= ``limit``."""
    if limit < 0:
        raise ValueError(f"limit must be >= 0, got {limit}")

    total = 0
    for idx, ch in enumerate(text):
        width = 2 if ord(ch) > 0xFFFF else 1
        if total + width > limit:
            return text[:idx]
        total += width
    return text


def truncate_text(
    text: str,
    limit: int = MAX_MESSAGE_LENGTH,
    *,
    ellipsis: str = "...",
    show_remaining: bool = False,
) -> str:
    """Truncate text to a UTF-16 limit, appending ellipsis when needed."""
    if not ellipsis:
        raise ValueError("ellipsis must not be empty")

    ellipsis_length = utf16_len(ellipsis)
    if limit <= ellipsis_length:
        raise ValueError(f"limit must be greater than {ellipsis_length}, got {limit}")
    if utf16_len(text) <= limit:
        return text
    if not show_remaining:
        return f"{prefix_within_utf16_limit(text, limit - ellipsis_length)}{ellipsis}"
    return _truncate_with_remaining(text, limit, ellipsis=ellipsis, markdown=False)


def truncate_markdown_v2(
    text: str,
    limit: int = MAX_MESSAGE_LENGTH,
    *,
    ellipsis: str = MARKDOWN_V2_ELLIPSIS,
    show_remaining: bool = False,
) -> str:
    """Truncate escaped MarkdownV2 text to a UTF-16 limit, appending escaped ellipsis."""
    if not ellipsis:
        raise ValueError("ellipsis must not be empty")

    ellipsis_length = utf16_len(ellipsis)
    if limit <= ellipsis_length:
        raise ValueError(f"limit must be greater than {ellipsis_length}, got {limit}")

    if utf16_len(text) <= limit:
        return text
    if not show_remaining:
        truncated = prefix_within_utf16_limit(text, limit - ellipsis_length)
        trailing_backslashes = len(truncated) - len(truncated.rstrip("\\"))
        if trailing_backslashes % 2 == 1:
            truncated = truncated[:-1]
        return f"{truncated}{ellipsis}"
    return _truncate_with_remaining(text, limit, ellipsis=ellipsis, markdown=True)


def truncate_caption(caption: str | None, *, ellipsis: str = "...") -> str | None:
    """Truncate caption to Telegram's MAX_CAPTION_LENGTH limit.

    Args:
        caption: Caption text or None
        ellipsis: Suffix to add when truncating (default: "...")

    Returns:
        Truncated caption with ellipsis if needed, or None if input was None
    """
    if caption is None:
        return None
    return truncate_text(caption, MAX_CAPTION_LENGTH, ellipsis=ellipsis)


def _truncate_with_remaining(text: str, limit: int, *, ellipsis: str, markdown: bool) -> str:
    remaining = len(text) - len(prefix_within_utf16_limit(text, limit))
    while True:
        suffix = build_remaining_suffix(remaining, ellipsis=ellipsis, markdown=markdown)
        available = limit - utf16_len(suffix)
        if available <= 0:
            raise ValueError("limit too small for remaining suffix")
        truncated = prefix_within_utf16_limit(text, available)
        if markdown:
            trailing_backslashes = len(truncated) - len(truncated.rstrip("\\"))
            if trailing_backslashes % 2 == 1:
                truncated = truncated[:-1]
        new_remaining = len(text) - len(truncated)
        if new_remaining == remaining:
            return f"{truncated}{suffix}"
        remaining = new_remaining


def build_remaining_suffix(remaining: int, *, ellipsis: str, markdown: bool = False) -> str:
    """Render the "N characters were dropped" suffix used by every truncator."""
    label = f"[{remaining} chars]"
    if markdown:
        label = _escape_markdown_v2_literal(label)
    return f" {label}{ellipsis}"


def _escape_markdown_v2_literal(text: str) -> str:
    return "".join(f"\\{ch}" if ch in _MARKDOWN_V2_SPECIALS else ch for ch in text)
