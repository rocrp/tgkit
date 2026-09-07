from tgkit import (
    escape_telegram_markdown,
    format_for_telegram,
    format_link,
    prefix_within_utf16_limit,
    strip_markdown,
    unescape_telegram_markdown,
    utf16_len,
)
from tgkit.markdown import ITALIC_STAR_PATTERN


def test_escape_preserves_existing_escape():
    assert escape_telegram_markdown("hello\\_world") == "hello\\_world"
    assert escape_telegram_markdown("already\\escaped") == "already\\\\escaped"
    assert escape_telegram_markdown("a*b*c") == "a\\*b\\*c"


def test_unescape_round_trip():
    assert unescape_telegram_markdown("C:\\\\Users\\\\") == "C:\\Users\\"
    assert unescape_telegram_markdown(r"\\_literal") == r"\_literal"


def test_format_for_telegram_simple():
    result = format_for_telegram("Hello world")
    assert result.parse_mode == "MarkdownV2"
    assert "Hello world" in result.text


def test_strip_markdown_handles_nested_emphasis():
    assert strip_markdown("__a_b__") == "a_b"
    assert strip_markdown("**bold** and _italic_") == "bold and italic"


def test_italic_star_pattern_allows_single_char():
    match = ITALIC_STAR_PATTERN.search("*a*")
    assert match
    assert match.group(1) == "a"


def test_format_link_builds_markdown_link():
    result = format_link("click", "https://example.com")
    assert "[click]" in result
    assert "(https://example.com)" in result


def test_placeholder_restoration_handles_escaped_keys():
    # Simulate inline code that matches placeholder pattern; ensure it survives formatting
    text = "Workspace set to: `codeagentg`\nSession reset. Ready for new conversation."
    formatted = format_for_telegram(text)
    assert "codeagentg" in formatted.text
    assert "TG" not in formatted.text  # placeholder artifacts should not leak


def test_snake_case_not_italicized():
    text = "PULL_REQUEST_TEMPLATE"
    formatted = format_for_telegram(text)
    assert "\\_" in formatted.text  # underscores escaped
    assert formatted.text.startswith("PULL\\_REQUEST\\_TEMPLATE")


def test_escape_double_backslash_before_paren():
    # \\( in shell commands: literal backslash + paren needs both escaped
    # Input: two backslashes + paren → Output: escaped backslash + escaped paren
    assert escape_telegram_markdown("\\\\(") == "\\\\\\("


def test_escape_single_backslash_before_paren():
    # \( already escaped paren → preserve as-is
    assert escape_telegram_markdown("\\(") == "\\("


def test_escape_triple_backslash_before_paren():
    # \\\( → escaped backslash + escaped paren (already valid sequence)
    assert escape_telegram_markdown("\\\\\\(") == "\\\\\\("


def test_escape_double_backslash_preserved():
    # \\\\ in MarkdownV2 is valid escape for literal backslash - should not add more
    # C:\\Users → C:\\Users (two backslashes preserved, not becoming three)
    assert escape_telegram_markdown("C:\\\\Users") == "C:\\\\Users"
    assert escape_telegram_markdown("\\\\") == "\\\\"


def test_escape_shell_command_with_escaped_paren():
    # Real-world case: shell command with escaped parenthesis in regex
    text = 'rg -n "HackerNewsPlugin\\\\(" path'
    result = escape_telegram_markdown(text)
    # Should escape the ( that follows the \\
    assert "\\(" in result


# === Heading conversion tests ===


def test_heading_conversion_h1():
    text = "# Title"
    formatted = format_for_telegram(text)
    assert "▎*Title*" in formatted.text
    assert "#" not in formatted.text or "\\#" in formatted.text


def test_heading_conversion_h2():
    text = "## Subtitle"
    formatted = format_for_telegram(text)
    assert "▎*Subtitle*" in formatted.text


def test_heading_conversion_multiple():
    text = "# First\n\nSome text\n\n## Second"
    formatted = format_for_telegram(text)
    assert "▎*First*" in formatted.text
    assert "▎*Second*" in formatted.text


def test_blockquote_code_fence_keeps_language_on_fence_line():
    text = "\n".join(
        [
            ">```bash",
            ">echo hello",
            ">```",
        ]
    )
    formatted = format_for_telegram(text)
    assert ">```bash" in formatted.text
    assert ">```>bash" not in formatted.text


def test_code_fence_separates_following_text():
    text = "```\ncode\n```\nnext"
    formatted = format_for_telegram(text)
    lines = formatted.text.splitlines()
    fence_idx = max(idx for idx, line in enumerate(lines) if line == "```")
    assert lines[fence_idx + 1] == "next"


def test_blockquote_code_fence_separates_following_text():
    text = "\n".join(
        [
            ">```",
            ">code",
            ">```",
            ">next",
        ]
    )
    formatted = format_for_telegram(text)
    lines = formatted.text.splitlines()
    fence_idx = max(idx for idx, line in enumerate(lines) if line == ">```")
    assert lines[fence_idx + 1] == ">next"


def test_formulas_fall_through_as_source_at_the_fallback_tier():
    """Rich renders $LaTeX$ natively; the MarkdownV2 tier shows the source."""
    formatted = format_for_telegram(r"The formula is \( x^2 + y^2 = z^2 \)")
    assert "x²" not in formatted.text
    assert "x^2" in unescape_telegram_markdown(formatted.text)

    formatted = format_for_telegram(r"\[ E = mc^2 \]")
    assert "mc²" not in formatted.text
    assert "mc^2" in unescape_telegram_markdown(formatted.text)


# === Message splitting tests ===


def test_split_message_short():
    from tgkit import split_message

    text = "Short message"
    chunks = split_message(text)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_split_message_long():
    from tgkit import split_message

    text = "a" * 5000
    chunks = split_message(text, max_len=1000)
    assert len(chunks) > 1
    assert all(len(c) <= 1100 for c in chunks)  # Allow some margin for fence handling


def test_split_message_respects_utf16_limit():
    from tgkit import split_message

    text = "😀" * 2050
    chunks = split_message(text, max_len=4096)
    assert len(chunks) == 2
    assert "".join(chunks) == text
    assert all(utf16_len(chunk) <= 4096 for chunk in chunks)


def test_prefix_within_utf16_limit_preserves_code_points():
    text = "abc😀def"
    prefix = prefix_within_utf16_limit(text, 4)
    assert prefix == "abc"
    assert utf16_len(prefix) <= 4


def test_split_message_paragraph_boundary():
    from tgkit import split_message

    text = "First paragraph\n\nSecond paragraph\n\nThird paragraph"
    chunks = split_message(text, max_len=30)
    # Should split at paragraph boundaries when possible
    assert len(chunks) >= 2


def test_split_message_code_block_continuity():
    from tgkit import split_message

    # Long code block that needs splitting
    code = "x = 1\n" * 500
    text = f"```python\n{code}```"
    chunks = split_message(text, max_len=500)
    # Each chunk should have balanced code fences
    for chunk in chunks:
        fence_count = chunk.count("```")
        assert fence_count % 2 == 0, f"Unbalanced fences in chunk: {chunk[:100]}..."


# === Code block dedent tests ===


def test_code_block_dedent():
    # Indented code block should be dedented
    text = """```python
    def foo():
        return 1
    ```"""
    formatted = format_for_telegram(text)
    # Parentheses are NOT escaped inside code blocks
    assert "def foo():" in formatted.text
    assert "    return 1" in formatted.text


# === Image removal tests ===


def test_image_removed():
    """Telegram doesn't support images in markdown, so they are removed."""
    text = "Before ![alt text](https://example.com/img.png) after"
    formatted = format_for_telegram(text)
    assert "alt text" not in formatted.text
    assert "example.com" not in formatted.text
    assert "Before" in formatted.text
    assert "after" in formatted.text


def test_image_with_empty_alt_removed():
    text = "Check this ![](https://example.com/img.png) out"
    formatted = format_for_telegram(text)
    assert "example.com" not in formatted.text
    assert "Check this" in formatted.text
    assert "out" in formatted.text


def test_linked_image_converted_to_link():
    """Clickable image [![alt](img)](link) converts to link with URL as text."""
    text = "Click [![alt text](image.png)](https://example.com) here"
    formatted = format_for_telegram(text)
    # Image parts should be gone
    assert "alt text" not in formatted.text
    assert "image.png" not in formatted.text
    # Link preserved with URL as text (escaped for MarkdownV2)
    assert "example\\.com" in formatted.text
    assert "Click" in formatted.text
    assert "here" in formatted.text


def test_image_url_with_parentheses():
    """URLs with parentheses (Wikipedia style) should be handled correctly."""
    # Standalone image with parens in URL - should be fully removed
    text = "See ![info](https://en.wikipedia.org/wiki/Page_(info)) here"
    formatted = format_for_telegram(text)
    assert "See" in formatted.text
    assert "here" in formatted.text
    # No stray parenthesis left behind (the bug was leaving `) here`)
    assert formatted.text.count(")") == 0 or "\\)" in formatted.text

    # Linked image with parens in URL - should convert to link
    text2 = "[![alt](img.png)](https://en.wikipedia.org/wiki/Page_(info))"
    formatted2 = format_for_telegram(text2)
    assert "wikipedia" in formatted2.text
    assert "Page" in formatted2.text
    assert "info" in formatted2.text


# === Scheme-safe link tests ===


def test_local_path_link_renders_inline_code_not_link():
    out = format_for_telegram("see [AppState.swift](/Users/x/AppState.swift:337) end").text
    assert "](/Users" not in out
    assert "`/Users/x/AppState.swift:337`" in out
    assert "AppState\\.swift" in out


def test_unknown_scheme_link_renders_inline_code():
    out = format_for_telegram("[note](obsidian://open?vault=x)").text
    assert "](obsidian://" not in out
    assert "`obsidian://open?vault=x`" in out
    assert "note" in out


def test_https_link_still_renders_as_link():
    out = format_for_telegram("[site](https://example.com)").text
    assert "[site](https://example.com)" in out


def test_tg_scheme_link_still_renders_as_link():
    out = format_for_telegram("[Join](tg://resolve?domain=test)").text
    assert "[Join](tg://resolve?domain=test)" in out


def test_angle_bracket_wrapped_https_link_renders_as_link():
    out = format_for_telegram("[site](<https://example.com>)").text
    assert "[site](https://example.com)" in out
