import asyncio
import re
from typing import TYPE_CHECKING, cast

import pytest
from fakes import api_error
from tgkit import prefix_within_utf16_limit, utf16_len
from tgkit.constants import truncate_text
from tgkit.reply import DraftOutcome, EditMessageOutcome, is_draft_unsupported_error, split_message

if TYPE_CHECKING:
    from aiogram import Bot


def test_split_message_keeps_every_chunk_within_the_limit():
    text = "\n".join(f"line {index}" for index in range(2000))
    chunks = split_message(text, 4096)
    assert len(chunks) > 1
    assert all(utf16_len(chunk) <= 4096 for chunk in chunks)
    # Seam whitespace is dropped, but no visible content is lost.
    assert "".join(chunk.replace("\n", "") for chunk in chunks) == text.replace("\n", "")


def test_split_message_keeps_code_fences_balanced():
    body = "\n".join("x" * 80 for _ in range(80))
    text = f"intro\n\n```python\n{body}\n```\n"
    chunks = split_message(text, 1000)
    assert len(chunks) > 1
    assert all(chunk.count("```") % 2 == 0 for chunk in chunks)


def test_split_message_invalid_max_length():
    with pytest.raises(ValueError, match="max_len must be positive"):
        split_message("hello", 0)


def test_truncate_text_rejects_too_small_limit():
    with pytest.raises(ValueError, match="limit must be greater than 3"):
        truncate_text("abcd", 3)


def test_split_text_short():
    assert split_message("hello", 100) == ["hello"]


def test_outcome_bool_requires_explicit_ok():
    with pytest.raises(TypeError, match="use \\.ok"):
        bool(EditMessageOutcome.FAILED)
    with pytest.raises(TypeError, match="use \\.ok"):
        bool(DraftOutcome.SENT)


def test_utf16_helpers_handle_astral_chars():
    text = "a😀b"
    assert utf16_len(text) == 4
    assert prefix_within_utf16_limit(text, 0) == ""
    assert prefix_within_utf16_limit(text, 1) == "a"
    assert prefix_within_utf16_limit(text, 2) == "a"
    assert prefix_within_utf16_limit(text, 3) == "a😀"
    assert prefix_within_utf16_limit(text, 4) == text


def test_split_text_respects_utf16_limit():
    text = "😀" * 2050
    chunks = split_message(text, 4096)
    assert len(chunks) == 2
    assert "".join(chunks) == text
    assert all(utf16_len(chunk) <= 4096 for chunk in chunks)


def test_truncate_text_respects_utf16_limit():
    text = "😀" * 2050
    truncated = truncate_text(text, 4096)
    assert utf16_len(truncated) <= 4096
    assert truncated.endswith("...")


def test_truncate_text_with_remaining_suffix():
    text = "a" * 30
    result = truncate_text(text, 20, show_remaining=True)
    match = re.search(r" \[(\d+) chars\]\.\.\.$", result)
    assert match
    remaining = int(match.group(1))
    prefix_len = len(result) - len(match.group(0))
    assert remaining == len(text) - prefix_len
    assert len(result) == 20


def test_edit_message_text_raw_retries_on_rate_limit(monkeypatch):
    from tgkit import message_edit as edit_mod
    from tgkit import retry as retry_mod

    call_count = 0
    sleep_calls: list[float] = []

    class FakeBot:
        async def edit_message_text(self, **_kwargs: object) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise api_error("Too Many Requests: retry after 1")

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(retry_mod.asyncio, "sleep", fake_sleep)

    bot = FakeBot()
    result = asyncio.run(
        edit_mod.edit_message_text_raw(
            cast("Bot", bot),
            chat_id=1,
            message_id=2,
            text="hello",
            parse_mode="MarkdownV2",
        )
    )
    assert result is EditMessageOutcome.EDITED
    assert call_count == 2
    assert sleep_calls


# ---------------------------------------------------------------------------
# Draft-unsupported detection
# ---------------------------------------------------------------------------


def test_textdraft_peer_invalid_is_draft_unsupported() -> None:
    assert is_draft_unsupported_error(api_error("Bad Request: TEXTDRAFT_PEER_INVALID")) is True


def test_generic_topic_error_is_not_draft_unsupported() -> None:
    """TOPIC_CLOSED etc. are real send failures, not missing draft support."""
    assert is_draft_unsupported_error(api_error("Bad Request: TOPIC_CLOSED")) is False


def test_generic_topic_hint_counts_when_method_is_referenced() -> None:
    error = api_error("sendMessageDraft: Bad Request: message thread not found")

    assert is_draft_unsupported_error(error) is True
