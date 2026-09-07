from __future__ import annotations

import re
import textwrap

_BLANK_LINE_RE = re.compile(r"\n{3,}")


def normalize_newlines(value: str) -> str:
    """Normalize CRLF to LF."""
    return value.replace("\r\n", "\n")


def collapse_blank_lines(value: str) -> str:
    """Collapse 3+ consecutive newlines to two."""
    return _BLANK_LINE_RE.sub("\n\n", value)


def escape_code_block_body(value: str) -> str:
    """Escape backslashes and backticks in code block body."""
    escaped = value.replace("\\", "\\\\").replace("`", "\\`")
    return escaped if escaped.endswith("\n") else f"{escaped}\n"


def escape_code_fence_language(language: str) -> str:
    """Escape language specifier in code fence."""
    return language.replace("\\", "\\\\").replace("`", "\\`")


def sanitize_code_block_segment(segment: str) -> str:
    """Sanitize code block content (language + body)."""
    normalized = normalize_newlines(segment)
    newline_index = normalized.find("\n")

    if newline_index == -1:
        language = normalized.strip()
        return f"{escape_code_fence_language(language)}\n" if language else "\n"

    language = normalized[:newline_index].strip()
    body = normalized[newline_index + 1 :]
    body = textwrap.dedent(body)
    header = f"{escape_code_fence_language(language)}\n" if language else "\n"
    return f"{header}{escape_code_block_body(body)}"


def prefix_lines(value: str, prefix: str) -> str:
    if not value:
        return value
    return "".join(f"{prefix}{line}" for line in value.splitlines(keepends=True))


def split_code_fence_segments(value: str) -> tuple[list[tuple[bool, str, str]], bool]:
    """Split text into (is_code, content, blockquote_prefix) segments."""
    if "```" not in value:
        return [(False, value, "")], False

    segments: list[tuple[bool, str, str]] = []
    text_buf: list[str] = []
    code_buf: list[str] = []
    code_prefix = ""
    code_lang = ""
    in_code = False
    unmatched = False

    for line in value.splitlines(keepends=True):
        prefix, rest = _strip_blockquote_prefix(line)
        if not in_code:
            if rest.lstrip().startswith("```"):
                if text_buf:
                    segments.append((False, "".join(text_buf), ""))
                    text_buf = []
                in_code = True
                code_prefix = prefix
                fence = rest.lstrip()[3:]
                code_lang = fence.strip()
                code_buf = []
                continue
            text_buf.append(line)
            continue

        if prefix == code_prefix and rest.lstrip().startswith("```"):
            segment = f"{code_lang}\n{''.join(code_buf)}"
            segments.append((True, segment, code_prefix))
            in_code = False
            code_prefix = ""
            code_lang = ""
            code_buf = []
            continue

        body_line = line
        if code_prefix and body_line.startswith(code_prefix):
            body_line = body_line[len(code_prefix) :]
        code_buf.append(body_line)

    if in_code:
        unmatched = True
        segment = f"{code_lang}\n{''.join(code_buf)}"
        segments.append((True, segment, code_prefix))
    elif text_buf:
        segments.append((False, "".join(text_buf), ""))

    return segments, unmatched


def _strip_blockquote_prefix(line: str) -> tuple[str, str]:
    if line.startswith("***>"):
        return "***>", line[4:]
    if line.startswith("**>"):
        return "**>", line[3:]
    if line.startswith(">"):
        return ">", line[1:]
    return "", line
