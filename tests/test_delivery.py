"""The degradation ladder, Delivery Outcome, and the traps #114 closed."""

from __future__ import annotations

from typing import Any

import pytest
from fakes import api_error
from tgkit.constants import MAX_CAPTION_LENGTH, MAX_MESSAGE_LENGTH, utf16_len
from tgkit.delivery import link_preview_options, reply_parameters, send_text
from tgkit.message_misc import fit_caption
from tgkit.outcome import DeliveryOutcome, TierFailure


class FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


class LadderBot:
    """A bot that rejects the tiers named in ``reject``."""

    def __init__(self, reject: set[str] | None = None) -> None:
        self.reject = reject or set()
        self.rich_calls: list[dict[str, Any]] = []
        self.message_calls: list[dict[str, Any]] = []
        self.document_calls: list[dict[str, Any]] = []
        self._next_id = 0

    def _message(self) -> FakeMessage:
        self._next_id += 1
        return FakeMessage(self._next_id)

    async def send_rich_message(self, **kwargs: Any) -> FakeMessage:
        self.rich_calls.append(kwargs)
        if "rich" in self.reject:
            raise api_error("Bad Request: can't parse rich_message")
        return self._message()

    async def send_message(self, **kwargs: Any) -> FakeMessage:
        self.message_calls.append(kwargs)
        if "markdownv2" in self.reject and kwargs.get("parse_mode"):
            raise api_error("Bad Request: can't parse entities")
        return self._message()

    async def send_document(self, **kwargs: Any) -> FakeMessage:
        self.document_calls.append(kwargs)
        return self._message()


# ---------------------------------------------------------------------------
# Tier selection
# ---------------------------------------------------------------------------


async def test_rich_is_the_default_tier():
    bot = LadderBot()
    outcome = await send_text(bot, chat_id=42, text="# Title\n\nBody")  # ty: ignore[invalid-argument-type]

    assert outcome.tier == "rich"
    assert not outcome.degraded
    assert outcome.failures == []
    assert outcome.message_ids == [1]
    assert len(bot.rich_calls) == 1
    assert bot.message_calls == []


async def test_rich_rejection_degrades_to_markdown_v2():
    bot = LadderBot(reject={"rich"})
    outcome = await send_text(bot, chat_id=42, text="**bold** text")  # ty: ignore[invalid-argument-type]

    assert outcome.tier == "markdownv2"
    assert outcome.degraded
    assert [failure.tier for failure in outcome.failures] == ["rich"]
    assert "can't parse rich_message" in outcome.error_chain
    assert bot.message_calls[0]["parse_mode"] == "MarkdownV2"


async def test_descent_switches_to_the_payload_fallback_form():
    """tg:// references are dead below Rich; the alt-text form must go out."""
    from tgkit.rich import RichPayload

    bot = LadderBot(reject={"rich"})
    payload = RichPayload(markdown="![x](tg://photo?id=m0)", fallback_markdown="the chart")
    outcome = await send_text(bot, chat_id=42, text=payload)  # ty: ignore[invalid-argument-type]

    assert outcome.tier == "markdownv2"
    assert "tg://" not in bot.message_calls[0]["text"]
    assert "the chart" in bot.message_calls[0]["text"]


async def test_both_upper_tiers_rejected_lands_on_plain():
    bot = LadderBot(reject={"rich", "markdownv2"})
    outcome = await send_text(bot, chat_id=42, text="**bold** text")  # ty: ignore[invalid-argument-type]

    assert outcome.tier == "plain"
    assert [failure.tier for failure in outcome.failures] == ["rich", "markdownv2"]
    assert bot.message_calls[-1]["parse_mode"] is None


async def test_plain_means_plain():
    """parse_mode=None must never be coerced back into MarkdownV2."""
    bot = LadderBot()
    outcome = await send_text(bot, chat_id=42, text="a_b_c *literal*", tier="plain")  # ty: ignore[invalid-argument-type]

    assert outcome.tier == "plain"
    assert bot.rich_calls == []
    assert bot.message_calls[0]["parse_mode"] is None
    assert bot.message_calls[0]["text"] == "a_b_c *literal*"


async def test_explicit_tier_skips_the_ones_above_it():
    bot = LadderBot()
    outcome = await send_text(bot, chat_id=42, text="hello", tier="markdownv2")  # ty: ignore[invalid-argument-type]

    assert outcome.tier == "markdownv2"
    assert bot.rich_calls == []


async def test_rate_limits_never_degrade_a_tier(monkeypatch: pytest.MonkeyPatch):
    """A 429 says nothing about the payload, so the ladder must not descend."""
    attempts = 0

    class RateLimitedOnce(LadderBot):
        async def send_rich_message(self, **kwargs: Any) -> FakeMessage:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise api_error("Too Many Requests: retry after 1")
            return await super().send_rich_message(**kwargs)

    import tgkit.retry as retry_mod

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(retry_mod.asyncio, "sleep", no_sleep)

    bot = RateLimitedOnce()
    outcome = await send_text(bot, chat_id=42, text="body")  # ty: ignore[invalid-argument-type]

    assert outcome.tier == "rich"
    assert outcome.failures == []
    assert attempts == 2
    assert bot.message_calls == []


# ---------------------------------------------------------------------------
# Length budgets across tiers
# ---------------------------------------------------------------------------


async def test_payload_over_the_rich_budget_skips_the_rich_tier():
    bot = LadderBot()
    outcome = await send_text(bot, chat_id=42, text="x" * 40000, fallback="split")  # ty: ignore[invalid-argument-type]

    assert outcome.tier == "markdownv2"
    assert bot.rich_calls == []
    assert outcome.failures[0].tier == "rich"
    assert "over the 32768 rich limit" in outcome.failures[0].reason


async def test_rich_sized_payload_is_rechunked_when_it_falls_to_tier_two():
    """A 32768-valid payload overflows 4096 and must be split, not sent whole."""
    bot = LadderBot(reject={"rich"})
    text = "\n".join(f"line {index}" for index in range(3000))

    outcome = await send_text(bot, chat_id=42, text=text, fallback="split")  # ty: ignore[invalid-argument-type]

    assert outcome.tier == "markdownv2"
    assert len(bot.message_calls) > 1
    assert all(utf16_len(call["text"]) <= MAX_MESSAGE_LENGTH for call in bot.message_calls)
    assert len(outcome.message_ids) == len(bot.message_calls)


async def test_document_fallback_attaches_the_whole_text():
    bot = LadderBot(reject={"rich"})
    text = "y" * 9000

    outcome = await send_text(bot, chat_id=42, text=text, fallback="document")  # ty: ignore[invalid-argument-type]

    assert bot.message_calls == []
    assert len(bot.document_calls) == 1
    assert bot.document_calls[0]["document"].data == text.encode()
    assert outcome.message_ids == [1]


async def test_no_fallback_is_an_error_not_a_truncation():
    bot = LadderBot(reject={"rich"})
    with pytest.raises(ValueError, match="no long-text fallback"):
        await send_text(bot, chat_id=42, text="z" * 9000, fallback=None)  # ty: ignore[invalid-argument-type]


async def test_only_the_first_chunk_carries_the_reply_parameters():
    bot = LadderBot(reject={"rich"})
    text = "\n".join(f"line {index}" for index in range(3000))

    await send_text(
        bot,  # ty: ignore[invalid-argument-type]
        chat_id=42,
        text=text,
        reply_to_message_id=7,
        fallback="split",
    )

    assert bot.message_calls[0]["reply_parameters"].message_id == 7
    assert all("reply_parameters" not in call for call in bot.message_calls[1:])


# ---------------------------------------------------------------------------
# Bot API 7.0 modernization
# ---------------------------------------------------------------------------


async def test_sends_use_link_preview_options_not_the_deprecated_flag():
    bot = LadderBot(reject={"rich"})
    await send_text(bot, chat_id=42, text="body", disable_link_preview=True)  # ty: ignore[invalid-argument-type]

    call = bot.message_calls[0]
    assert "disable_web_page_preview" not in call
    assert call["link_preview_options"].is_disabled is True


def test_link_preview_options_and_reply_parameters_builders():
    assert link_preview_options(None) is None
    assert link_preview_options(True).is_disabled is True
    assert reply_parameters(None) is None
    assert reply_parameters(11).message_id == 11


# ---------------------------------------------------------------------------
# Delivery Outcome
# ---------------------------------------------------------------------------


def test_outcome_reports_ids_and_degradation():
    outcome = DeliveryOutcome(
        tier="plain",
        messages=[FakeMessage(1), FakeMessage(2)],  # ty: ignore[invalid-argument-type]
        failures=[TierFailure(tier="rich", reason="boom")],
    )
    assert outcome.message_ids == [1, 2]
    assert outcome.message_id == 2
    assert outcome.degraded
    assert outcome.error_chain == "rich: boom"


def test_outcome_without_messages_has_no_id():
    assert DeliveryOutcome(tier="rich").message_id is None


# ---------------------------------------------------------------------------
# Caption boundary
# ---------------------------------------------------------------------------


def test_caption_stays_within_the_limit_after_escaping():
    """Escaping after truncating is what keeps the escaped form legal."""
    caption = "a." * MAX_CAPTION_LENGTH  # every '.' grows to '\\.' when escaped
    fitted = fit_caption(caption, parse_mode="MarkdownV2")

    assert fitted is not None
    assert utf16_len(fitted) <= MAX_CAPTION_LENGTH
    assert not fitted.endswith("\\")  # never a dangling escape


def test_plain_caption_is_only_truncated():
    caption = "b" * (MAX_CAPTION_LENGTH * 2)
    fitted = fit_caption(caption, parse_mode=None)

    assert fitted is not None
    assert utf16_len(fitted) <= MAX_CAPTION_LENGTH
    assert "\\" not in fitted


def test_short_caption_is_untouched_apart_from_escaping():
    assert fit_caption(None, parse_mode=None) is None
    assert fit_caption("plain text", parse_mode=None) == "plain text"
    assert fit_caption("a.b", parse_mode="MarkdownV2") == "a\\.b"
