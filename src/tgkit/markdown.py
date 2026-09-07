"""Telegram MarkdownV2 formatting utilities.

Rewritten using md2tgmd patterns with:
- replace_all() for selective transformation
- Interleaved protect/escape/restore pattern
- PlaceholderRegistry for safe content protection
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import uuid4

from . import markdown_utils as _markdown_utils
from .markdown_fences import (
    collapse_blank_lines,
    escape_code_fence_language,
    normalize_newlines,
    prefix_lines,
    sanitize_code_block_segment,
    split_code_fence_segments,
)

strip_markdown = _markdown_utils.strip_markdown
split_message = _markdown_utils.split_message
ITALIC_STAR_PATTERN = _markdown_utils.ITALIC_STAR_PATTERN

# Characters that need escaping in MarkdownV2
TELEGRAM_MARKDOWN_SPECIALS = frozenset(
    ["_", "*", "[", "]", "(", ")", "~", "`", ">", "#", "+", "=", "|", "{", "}", ".", "!", "-"]
)

# Placeholder delimiters using non-whitespace ASCII control chars to avoid collisions
# Avoid \x1e/\x1f because regex \s may consume them during list-marker handling.
PLACEHOLDER_PREFIX = "\x02TG"
PLACEHOLDER_SUFFIX = "\x03"

_SPECIAL_CHARS_PATTERN = "".join(sorted(re.escape(ch) for ch in TELEGRAM_MARKDOWN_SPECIALS | {"\\"}))
TELEGRAM_MARKDOWN_UNESCAPE_PATTERN = re.compile(rf"\\([{_SPECIAL_CHARS_PATTERN}])")


@dataclass
class TelegramFormattedPayload:
    """Result from format_for_telegram()."""

    text: str
    parse_mode: str | None = "MarkdownV2"


@dataclass
class PlaceholderRegistry:
    """Store and restore formatted content to prevent double-escaping."""

    _counter: int = field(default=0, init=False)
    _placeholders: dict[str, str] = field(default_factory=dict, init=False)

    def store(self, value: str) -> str:
        """Store formatted value, return placeholder key."""
        key = f"{PLACEHOLDER_PREFIX}{self._counter}{uuid4().hex}{PLACEHOLDER_SUFFIX}"
        self._counter += 1
        self._placeholders[key] = value
        return key

    def restore(self, text: str) -> str:
        """Replace all placeholders with stored values (reverse order)."""
        result = text
        for key, value in reversed(list(self._placeholders.items())):
            if key in result:
                result = result.replace(key, value)
        return result


# === Core utilities ported from md2tgmd ===


def find_all_index(text: str, pattern: str) -> list[int]:
    """Find all capture group positions in text.

    Returns list: [0, start1, end1, start2, end2, ..., len(text)]
    """
    index_list = [0]
    for match in re.finditer(pattern, text, re.MULTILINE):
        if match.group(1) is not None:
            index_list.extend([match.start(1), match.end(1)])
    index_list.append(len(text))
    return index_list


def replace_all(text: str, pattern: str, transform: Callable[[str], str]) -> str:
    """Apply transform only to captured groups, preserve rest unchanged.

    This is the key utility from md2tgmd - allows patterns like
    "escape all `-` except those inside code blocks" by using:
    replace_all(text, r"```[\\s\\S]+?```|(-)", escape_func)
    """
    poslist = find_all_index(text, pattern)

    # Extract and transform captured groups
    transformed = []
    for i in range(1, len(poslist) - 1, 2):
        start, end = poslist[i], poslist[i + 1]
        transformed.append(transform(text[start:end]))

    # Extract non-matched text
    original = []
    for i in range(0, len(poslist), 2):
        start, end = poslist[i], poslist[i + 1]
        original.append(text[start:end])

    # Pad lists to equal length
    if len(transformed) < len(original):
        transformed.append("")
    elif len(original) < len(transformed):
        original.append("")

    # Interleave: [orig1, trans1, orig2, trans2, ...]
    result = []
    for o, t in zip(original, transformed, strict=False):
        result.extend([o, t])

    return "".join(result)


# === Escape functions ===


def escape_char(char: str) -> str:
    """Escape a single special character."""
    return "\\" + char


def escape_telegram_markdown(value: str) -> str:
    """Escape text for Telegram MarkdownV2 format.

    Handles existing escape sequences properly.
    """
    if not value:
        return ""

    specials = TELEGRAM_MARKDOWN_SPECIALS
    out: list[str] = []
    length = len(value)
    idx = 0

    while idx < length:
        ch = value[idx]
        nxt = value[idx + 1] if idx + 1 < length else None

        if ch == "\\":
            if nxt and (nxt in specials or nxt == "\\"):
                out.append("\\")
                out.append(nxt)
                idx += 2
                continue
            else:
                out.append("\\\\")
                idx += 1
                continue

        if ch in specials:
            out.append("\\")
            out.append(ch)
            idx += 1
            continue

        out.append(ch)
        idx += 1

    return "".join(out)


def unescape_telegram_markdown(value: str) -> str:
    """Reverse Telegram MarkdownV2 escaping."""
    if not value:
        return ""
    return TELEGRAM_MARKDOWN_UNESCAPE_PATTERN.sub(r"\1", value)


def escape_link_url(value: str) -> str:
    """Escape special characters in URL."""
    return re.sub(r"([()\\])", r"\\\1", value)


def escape_inline_code(value: str) -> str:
    """Escape backslashes and backticks in inline code."""
    return value.replace("\\", "\\\\").replace("`", "\\`")


def _split_expandable_marker(content: str) -> tuple[str, str]:
    stripped = content.rstrip()
    if stripped.count("||") != 1 or not stripped.endswith("||"):
        return content, ""
    suffix_whitespace = content[len(stripped) :]
    return stripped[:-2], f"||{suffix_whitespace}"


# Telegram only renders inline links for these schemes; anything else (local
# paths, obsidian://, …) is rejected and would collapse the whole message to
# plain text, so render those as inline code instead.
_TG_LINK_SCHEME_RE = re.compile(r"^(?:https?|tg)://", re.IGNORECASE)

# === Main escape function following md2tgmd order ===


def format_text_segment(value: str) -> str:
    """Format a text segment (not inside code fence).

    Follows md2tgmd's escape order with interleaved protect/escape/restore.
    """
    if not value:
        return ""

    text = value
    registry = PlaceholderRegistry()

    # --- 0. Remove images (Telegram doesn't support images in markdown) ---
    # URL pattern handles balanced parens (Wikipedia-style URLs like /wiki/Page_(info))
    _url = r"[^()\s]*(?:\([^()]*\)[^()\s]*)*"
    # First handle clickable images: [![alt](img.png)](link)
    # Convert to link with URL as text: [link](link)
    text = re.sub(rf"\[!\[[^\]]*\]\({_url}\)\]\(({_url})\)", r"[\1](\1)", text)
    # Then remove standalone images: ![alt](url) -> empty
    text = re.sub(rf"!\[[^\]]*\]\({_url}\)", "", text)

    # --- 0b. Protect markdown links [text](url) early ---
    # Must come before underscore/italic escaping to prevent URL mangling.
    def protect_link_early(m: re.Match) -> str:
        label = m.group(1)
        url = m.group(2)
        # CommonMark allows <...>-wrapped URLs; unwrap before validating.
        if len(url) >= 2 and url.startswith("<") and url.endswith(">"):
            url = url[1:-1]
        escaped_label = format_text_segment(label)
        if not _TG_LINK_SCHEME_RE.match(url):
            return registry.store(f"{escaped_label} `{escape_inline_code(url)}`")
        sanitized_url = escape_link_url(url)
        return registry.store(f"[{escaped_label}]({sanitized_url})")

    text = re.sub(rf"\[([^\]\n]+?)\]\(({_url})\)", protect_link_early, text)

    # --- 0c. Protect bare URLs (before any escaping mangles them) ---
    # Telegram auto-links bare URLs, but only if special chars aren't escaped.
    def protect_bare_url(m: re.Match) -> str:
        url = m.group(0)
        escaped_url = escape_link_url(url)
        escaped_label = escape_telegram_markdown(url)
        return registry.store(f"[{escaped_label}]({escaped_url})")

    text = re.sub(r"\bhttps?://[^\s)\]>]+", protect_bare_url, text)

    # Formulas are NOT converted here. The Rich tier renders $LaTeX$ natively;
    # this tier is the fallback, where a formula falls through as its source.

    # --- 1. Protect existing escaped specials ---
    def protect_escaped_special(m: re.Match) -> str:
        return registry.store(m.group(0))

    text = re.sub(rf"\\([{_SPECIAL_CHARS_PATTERN}])", protect_escaped_special, text)

    # --- 3. Inline code (protect before any escaping) ---
    def protect_inline_code(match_text: str) -> str:
        content = match_text[1:-1]  # Remove backticks
        escaped = escape_inline_code(content)
        return registry.store(f"`{escaped}`")

    text = re.sub(r"`([^`\n]+)`", lambda m: protect_inline_code(m.group(0)), text)

    # --- 4. Underline __...__ ---
    # MUST come before italic _..._ so the double underscore is not consumed as italic.
    def protect_underline(content: str) -> str:
        inner = format_text_segment(content)
        return registry.store(f"__{inner}__")

    text = re.sub(r"__(.+?)__", lambda m: protect_underline(m.group(1)), text, flags=re.DOTALL)

    # --- 5. Italic _..._ ---
    def protect_italic(content: str) -> str:
        inner = format_text_segment(content)  # Recursive for nested
        return registry.store(f"_{inner}_")

    text = re.sub(
        r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)", lambda m: protect_italic(m.group(1)), text, flags=re.DOTALL
    )
    text = re.sub(r"_", r"\_", text)  # Escape remaining underscores

    # --- 5. Bold **...** ---
    def protect_bold(content: str) -> str:
        inner = format_text_segment(content)
        return registry.store(f"*{inner}*")

    text = re.sub(r"\*\*(.+?)\*\*", lambda m: protect_bold(m.group(1)), text, flags=re.DOTALL)

    # --- 6. Single star italic *...* (after bold) ---
    def protect_star_italic(content: str) -> str:
        inner = format_text_segment(content)
        return registry.store(f"_{inner}_")

    text = re.sub(
        r"(?<!\*)\*(?!\*)([^\s*\n](?:[^*\n]*?[^\s*\n])?)\*(?!\*)",
        lambda m: protect_star_italic(m.group(1)),
        text,
    )

    # --- 7. Blockquotes > (MUST come before asterisk escaping for expandable **> and ***> syntax) ---
    def protect_blockquote(m: re.Match) -> str:
        prefix = m.group(1) or ""
        content = m.group(2) or ""
        content, marker = _split_expandable_marker(content)
        sanitized = format_text_segment(content)
        return registry.store(f"{prefix}>{sanitized}{marker}")

    text = re.sub(r"^(\*{2,3})?>(.*)$", protect_blockquote, text, flags=re.MULTILINE)

    # --- 8. List markers ---
    def replace_list_marker(m: re.Match) -> str:
        prefix = m.group(1)
        indent = m.group(2)
        return f"{prefix}{indent}• "

    text = re.sub(r"(^|\n)([\t ]*)([\*\+\-])\s+", replace_list_marker, text)
    text = re.sub(r"\*", r"\*", text)  # Escape remaining asterisks

    # --- 8. Links [text](url) --- (handled early in step 0b)
    text = re.sub(r"\[", r"\[", text)
    text = re.sub(r"\]", r"\]", text)
    text = re.sub(r"\(", r"\(", text)
    text = re.sub(r"\)", r"\)", text)

    # --- 9. Strikethrough ~~...~~ ---
    def protect_strikethrough(content: str) -> str:
        inner = format_text_segment(content)
        return registry.store(f"~{inner}~")

    text = re.sub(r"~~(.+?)~~", lambda m: protect_strikethrough(m.group(1)), text, flags=re.DOTALL)
    text = re.sub(r"~", r"\~", text)

    # --- 10. Spoiler ||...|| ---
    def protect_spoiler(content: str) -> str:
        inner = format_text_segment(content)
        return registry.store(f"||{inner}||")

    text = re.sub(r"\|\|(.+?)\|\|", lambda m: protect_spoiler(m.group(1)), text, flags=re.DOTALL)

    # --- 11. Headings # ---
    def protect_heading(m: re.Match) -> str:
        content = m.group(2)
        inner = format_text_segment(content)  # Recursive for nested formatting
        return registry.store(f"▎*{inner}*\n\n")

    text = re.sub(r"^(#{1,6})\s+(.+)$", protect_heading, text, flags=re.MULTILINE)

    # --- 12. Escape remaining specials (idempotent) ---
    text = escape_telegram_markdown(text)

    # --- 13. Restore placeholders ---
    return registry.restore(text)


# === Code fence handling ===
def format_for_telegram(payload: str) -> TelegramFormattedPayload:
    """Format text for Telegram MarkdownV2.

    Entry point for the fallback tier of the delivery ladder. Handles code
    fences, markdown formatting, and proper escaping. Expects unescaped, raw
    text input.
    """
    normalized = normalize_newlines(payload)
    segments, _unmatched = split_code_fence_segments(normalized)

    if len(segments) == 1 and not segments[0][0]:
        formatted = format_text_segment(segments[0][1])
        formatted = collapse_blank_lines(formatted)
        if PLACEHOLDER_PREFIX in formatted or PLACEHOLDER_SUFFIX in formatted:
            raise ValueError("telegram markdown formatting left unresolved placeholders")
        return TelegramFormattedPayload(text=formatted)

    text_parts = []
    last_index = len(segments) - 1
    for idx, (is_code, content, prefix) in enumerate(segments):
        if not is_code:
            text_parts.append(collapse_blank_lines(format_text_segment(content)))
            continue

        sanitized = sanitize_code_block_segment(content)
        trailing_newline = "\n" if idx < last_index else ""
        if prefix:
            newline_index = sanitized.find("\n")
            if newline_index == -1:
                lang = sanitized.strip()
                body = ""
            else:
                lang = sanitized[:newline_index].strip()
                body = sanitized[newline_index + 1 :]
            fence = f"{prefix}```{lang}\n" if lang else f"{prefix}```\n"
            text_parts.append(fence)
            if body:
                text_parts.append(prefix_lines(body, prefix))
            text_parts.append(f"{prefix}```{trailing_newline}")
        else:
            text_parts.append("```")
            text_parts.append(sanitized)
            text_parts.append(f"```{trailing_newline}")

    combined = "".join(text_parts)
    if PLACEHOLDER_PREFIX in combined or PLACEHOLDER_SUFFIX in combined:
        raise ValueError("telegram markdown formatting left unresolved placeholders")
    return TelegramFormattedPayload(text=combined)


# === Helper functions for manual formatting ===


def format_code_block(code: str, language: str = "") -> str:
    """Format code block for Telegram."""
    escaped_code = code.replace("\\", "\\\\").replace("`", "\\`")
    escaped_lang = escape_code_fence_language(language) if language else ""
    return f"```{escaped_lang}\n{escaped_code}\n```"


def format_inline_code(code: str) -> str:
    """Format inline code for Telegram."""
    escaped = code.replace("\\", "\\\\").replace("`", "\\`")
    return f"`{escaped}`"


def format_bold(text: str) -> str:
    """Format bold text for Telegram."""
    return f"*{escape_telegram_markdown(text)}*"


def format_italic(text: str) -> str:
    """Format italic text for Telegram."""
    return f"_{escape_telegram_markdown(text)}_"


def format_link(text: str, url: str) -> str:
    """Format link for Telegram."""
    escaped_url = escape_link_url(url)
    return f"[{escape_telegram_markdown(text)}]({escaped_url})"
