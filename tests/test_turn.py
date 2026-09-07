"""Turn View composition, transports, and the Persist contract."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fakes import api_error

from tgkit.message_misc import keep_typing
from tgkit.pacing import (
    _GROUP_MIN_UPDATE_INTERVAL,
    _MAX_UPDATE_INTERVAL,
    _MIN_UPDATE_INTERVAL,
)
from tgkit.rich import THINKING_CLOSE, THINKING_OPEN, RichContractError
from tgkit.turn import DraftTransport, EditTransport, Turn
from tgkit.turn_view import TurnView, normalize_activity

PRIVATE_CHAT = 4242
GROUP_CHAT = -1001234567890


class FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


class TurnBot:
    def __init__(self, *, drafts: bool = True) -> None:
        self.drafts = drafts
        self.draft_calls: list[dict[str, Any]] = []
        self.rich_calls: list[dict[str, Any]] = []
        self.edit_calls: list[dict[str, Any]] = []
        self.message_calls: list[dict[str, Any]] = []
        self.photo_calls: list[dict[str, Any]] = []
        self.media_group_calls: list[dict[str, Any]] = []
        self.deleted: list[int] = []
        self._next_id = 0

    def _message(self) -> FakeMessage:
        self._next_id += 1
        return FakeMessage(self._next_id)

    async def send_rich_message_draft(self, **kwargs: Any) -> bool:
        self.draft_calls.append(kwargs)
        if not self.drafts:
            raise api_error("Bad Request: TEXTDRAFT_PEER_INVALID")
        return True

    async def send_rich_message(self, **kwargs: Any) -> FakeMessage:
        self.rich_calls.append(kwargs)
        return self._message()

    async def edit_message_text(self, **kwargs: Any) -> FakeMessage:
        self.edit_calls.append(kwargs)
        return self._message()

    async def send_message(self, **kwargs: Any) -> FakeMessage:
        self.message_calls.append(kwargs)
        return self._message()

    async def send_document(self, **kwargs: Any) -> FakeMessage:
        return self._message()

    async def send_photo(self, **kwargs: Any) -> FakeMessage:
        self.photo_calls.append(kwargs)
        return self._message()

    async def send_media_group(self, **kwargs: Any) -> list[FakeMessage]:
        self.media_group_calls.append(kwargs)
        return [self._message() for _ in kwargs["media"]]

    async def delete_message(self, **kwargs: Any) -> bool:
        self.deleted.append(kwargs["message_id"])
        return True

    async def send_chat_action(self, **_kwargs: Any) -> bool:
        return True


def view_with(answer: str = "", activities: int = 0) -> TurnView:
    view = TurnView(started_at=0.0)
    if answer:
        view.set_answer(answer)
    for index in range(activities):
        view.upsert_activity(f"a{index}", f"🔧 step {index}")
    return view


# ---------------------------------------------------------------------------
# Composition order
# ---------------------------------------------------------------------------


def test_answer_comes_before_activity_and_status():
    view = view_with(answer="The answer body.", activities=2)
    composed = view.compose(now=5.0)

    answer_at = composed.index("The answer body.")
    activity_at = composed.index("🔧 step 0")
    status_at = composed.index("⏳")
    assert answer_at < activity_at < status_at


def test_only_recent_activities_appear_in_flight():
    view = TurnView(recent_activities=3, started_at=0.0)
    for index in range(10):
        view.upsert_activity(f"a{index}", f"step {index}")

    composed = view.compose(now=1.0)
    assert "step 9" in composed
    assert "step 7" in composed
    assert "step 6" not in composed


def test_activity_log_carries_every_step():
    view = TurnView(recent_activities=2, started_at=0.0)
    for index in range(10):
        view.upsert_activity(f"a{index}", f"step {index}")

    log = view.activity_log()
    assert log.startswith("<details><summary>Activity Log (10 steps)</summary>")
    assert log.rstrip().endswith("</details>")
    for index in range(10):
        assert f"step {index}" in log


def test_persist_folds_the_activity_log_under_the_answer():
    view = view_with(answer="Final answer.", activities=3)
    persisted = view.persist_markdown()

    assert persisted.index("Final answer.") < persisted.index("<details>")
    assert "Activity Log (3 steps)" in persisted


def test_upsert_keeps_an_activity_in_its_original_position():
    view = view_with(activities=3)
    view.upsert_activity("a0", "🔧 step 0 done", duration=1.5)

    lines = view.activity_log().splitlines()
    steps = [line for line in lines if line.startswith("- ")]
    assert steps[0] == "- 🔧 step 0 done — 1s"
    assert len(steps) == 3


def test_answer_prefix_applies_to_every_line():
    view = TurnView(answer_prefix="> ", started_at=0.0)
    view.set_answer("one\ntwo")
    assert "> one\n> two" in view.compose(now=1.0)


def test_status_provider_joins_the_status_line():
    view = TurnView(started_at=0.0, status_provider=lambda: "main +2 -1")
    view.complete("done")
    status = view.status_line(now=3.0)
    assert status.startswith("✅")
    assert status.endswith("main +2 -1")


def test_activity_normalization_rejects_multiline_and_empty():
    assert normalize_activity("id", " step ") == ("id", "step")
    assert normalize_activity("output", "x") is None
    with pytest.raises(ValueError, match="single-line"):
        normalize_activity("id", "a\nb")
    with pytest.raises(ValueError, match="empty"):
        normalize_activity("id", "   ")


# ---------------------------------------------------------------------------
# The thinking block
# ---------------------------------------------------------------------------


def test_thinking_block_wraps_activity_only_when_asked():
    view = view_with(answer="Body", activities=1)

    plain = view.compose(thinking=False, now=1.0)
    assert THINKING_OPEN not in plain

    drafted = view.compose(thinking=True, now=1.0)
    assert THINKING_OPEN in drafted and THINKING_CLOSE in drafted
    # The answer stays outside the thinking block — it is the point of the reply.
    assert drafted.index("Body") < drafted.index(THINKING_OPEN)


def test_persisted_markdown_never_carries_the_thinking_block():
    view = view_with(answer="Body", activities=2)
    view.complete("done")
    assert THINKING_OPEN not in view.persist_markdown()
    assert THINKING_CLOSE not in view.persist_markdown()


async def test_a_thinking_frame_cannot_be_persisted():
    """Belt and braces: the send itself refuses the draft-only tag."""
    from tgkit.rich import send_rich

    view = view_with(answer="Body", activities=1)
    with pytest.raises(RichContractError, match="valid only in drafts"):
        await send_rich(
            TurnBot(),  # ty: ignore[invalid-argument-type]
            chat_id=PRIVATE_CHAT,
            markdown=view.compose(thinking=True, now=1.0),
        )


# ---------------------------------------------------------------------------
# Transport selection and cadence
# ---------------------------------------------------------------------------


async def test_private_chat_streams_over_a_draft():
    bot = TurnBot()
    turn = await Turn.create(bot, PRIVATE_CHAT)  # ty: ignore[invalid-argument-type]
    try:
        assert turn.transport == "draft"
        assert bot.draft_calls
        assert bot.draft_calls[0]["draft_id"] != 0
    finally:
        await turn.stop()


async def test_group_chat_edits_a_message():
    bot = TurnBot()
    turn = await Turn.create(bot, GROUP_CHAT)  # ty: ignore[invalid-argument-type]
    try:
        assert turn.transport == "edit"
        assert bot.draft_calls == []  # never probed: drafts are private-only
        assert turn.message_id is not None
    finally:
        await turn.stop()


async def test_draft_unsupported_falls_back_to_editing():
    bot = TurnBot(drafts=False)
    turn = await Turn.create(bot, PRIVATE_CHAT)  # ty: ignore[invalid-argument-type]
    try:
        assert turn.transport == "edit"
        assert bot.draft_calls  # probed once
    finally:
        await turn.stop()


def test_group_cadence_matches_the_shipped_flood_budget():
    """The 2026-07-13 flood-cascade fix must survive the recast."""
    bot = TurnBot()
    group = EditTransport(bot, GROUP_CHAT, 1)  # ty: ignore[invalid-argument-type]
    private_edit = EditTransport(bot, PRIVATE_CHAT, 1)  # ty: ignore[invalid-argument-type]
    draft = DraftTransport(bot, PRIVATE_CHAT, draft_id=7)  # ty: ignore[invalid-argument-type]

    assert group.base_interval == _GROUP_MIN_UPDATE_INTERVAL == 5.0
    assert private_edit.base_interval == _MIN_UPDATE_INTERVAL == 1.5
    assert draft.base_interval == _MIN_UPDATE_INTERVAL == 1.5
    assert _MAX_UPDATE_INTERVAL == 30.0


def test_draft_transport_refuses_group_chats():
    with pytest.raises(RichContractError, match="private-chat only"):
        DraftTransport(TurnBot(), GROUP_CHAT, draft_id=7)  # ty: ignore[invalid-argument-type]


async def test_attach_adopts_an_existing_message():
    bot = TurnBot()
    turn = await Turn.attach(bot, GROUP_CHAT, 99)  # ty: ignore[invalid-argument-type]
    try:
        assert turn.transport == "edit"
        assert turn.message_id == 99
        assert bot.message_calls == []  # nothing new was posted
    finally:
        await turn.stop()


# ---------------------------------------------------------------------------
# Persist
# ---------------------------------------------------------------------------


async def test_persist_is_mandatory_after_a_draft():
    """A draft is a 30-second preview; the turn must commit a real message."""
    bot = TurnBot()
    turn = await Turn.create(bot, PRIVATE_CHAT)  # ty: ignore[invalid-argument-type]
    await turn.append_answer("Here is the answer.")

    outcome = await turn.persist()

    assert outcome.tier == "rich"
    assert len(bot.rich_calls) == 1
    assert "Here is the answer." in bot.rich_calls[0]["rich_message"].markdown
    assert THINKING_OPEN not in bot.rich_calls[0]["rich_message"].markdown


async def test_persist_in_a_group_edits_the_view_message():
    bot = TurnBot()
    turn = await Turn.create(bot, GROUP_CHAT)  # ty: ignore[invalid-argument-type]
    message_id = turn.message_id
    await turn.append_answer("Group answer.")

    outcome = await turn.persist()

    assert outcome.tier == "rich"
    assert bot.edit_calls[-1]["message_id"] == message_id
    assert "Group answer." in bot.edit_calls[-1]["rich_message"].markdown


async def test_link_preview_suppression_reaches_the_wire_where_the_api_allows():
    """sendRichMessage carries no preview field; every editMessageText must."""
    bot = TurnBot()
    turn = await Turn.create(bot, GROUP_CHAT, disable_link_preview=True)  # ty: ignore[invalid-argument-type]
    await turn.append_answer("see https://example.com")

    outcome = await turn.persist()

    assert outcome.tier == "rich"
    assert "link_preview_options" not in bot.rich_calls[0]  # the API has no such field
    assert bot.edit_calls
    for call in bot.edit_calls:
        assert call["link_preview_options"].is_disabled is True


async def test_view_failure_never_cancels_the_reply():
    """The whole point of Persist: a dead view must not swallow the answer."""

    class NoViewBot(TurnBot):
        """Refuses every way of putting a view on screen, but can still send."""

        def __init__(self) -> None:
            super().__init__()
            self.allow_rich = False

        async def send_rich_message_draft(self, **kwargs: Any) -> bool:
            raise api_error("Bad Request: TEXTDRAFT_PEER_INVALID")

        async def send_rich_message(self, **kwargs: Any) -> FakeMessage:
            if not self.allow_rich:
                raise api_error("Bad Request: chat not found")
            return await super().send_rich_message(**kwargs)

        async def send_message(self, **kwargs: Any) -> FakeMessage:
            raise api_error("Bad Request: chat not found")

    bot = NoViewBot()
    turn = await Turn.create(bot, PRIVATE_CHAT)  # ty: ignore[invalid-argument-type]
    assert turn.transport == "none"  # no view could be established

    await turn.append_answer("The answer survives.")
    bot.allow_rich = True  # the chat recovers by the time the answer is ready
    outcome = await turn.persist()

    assert outcome.tier == "rich"
    assert "The answer survives." in bot.rich_calls[0]["rich_message"].markdown


async def test_persist_carries_the_activity_log():
    bot = TurnBot()
    turn = await Turn.create(bot, PRIVATE_CHAT)  # ty: ignore[invalid-argument-type]
    await turn.add_activity("read", "🔧 Read config.py")
    await turn.add_activity("edit", "✏️ Edit config.py")
    await turn.append_answer("Done.")

    await turn.persist()

    markdown = bot.rich_calls[0]["rich_message"].markdown
    assert "<details><summary>Activity Log (2 steps)</summary>" in markdown
    assert "Read config.py" in markdown
    assert markdown.index("Done.") < markdown.index("<details>")


def _write_png(path: Any, size: tuple[int, int] = (16, 16)) -> Any:
    from PIL import Image

    Image.new("RGB", size, color=(10, 20, 30)).save(path, format="PNG")
    return path


async def test_persist_inlines_a_photo_embed_at_its_markdown_position(tmp_path: Any):
    """The whole point of ADR 20260727: the image rides the final message."""
    image = _write_png(tmp_path / "shot.png")
    bot = TurnBot()
    turn = await Turn.create(bot, PRIVATE_CHAT)  # ty: ignore[invalid-argument-type]

    outcome = await turn.persist(f"Before\n\n![shot]({image})\n\nAfter")

    assert outcome.tier == "rich"
    rich_message = bot.rich_calls[0]["rich_message"]
    assert "tg://photo?id=m0" in rich_message.markdown
    assert str(image) not in rich_message.markdown
    assert len(rich_message.media) == 1
    assert outcome.rejected == []
    assert bot.photo_calls == []  # delivered inline: no Media Tail
    assert bot.media_group_calls == []


async def test_degraded_persist_diverts_inline_media_to_the_tail(tmp_path: Any):
    """When Rich is refused, the alt-text form goes out and the photo follows."""
    image = _write_png(tmp_path / "graph.png")

    class RichlessEditBot(TurnBot):
        async def edit_message_text(self, **kwargs: Any) -> FakeMessage:
            if "rich_message" in kwargs:
                raise api_error("Bad Request: can't parse rich_message")
            return await super().edit_message_text(**kwargs)

    bot = RichlessEditBot()
    turn = await Turn.attach(bot, GROUP_CHAT, 99)  # ty: ignore[invalid-argument-type]

    outcome = await turn.persist(f"The graph:\n\n![graph]({image})")

    assert outcome.tier == "markdownv2"
    safe_edit = next(call for call in bot.edit_calls if "rich_message" not in call)
    assert "tg://" not in safe_edit["text"]
    assert str(image) not in safe_edit["text"]
    assert len(bot.photo_calls) == 1  # the Media Tail carried the photo
    assert outcome.rejected == []


async def test_rate_limited_persist_edit_sleeps_in_place_instead_of_resending():
    """A 429 on the final edit must wait it out, never fall to a fresh send —
    that would spend more of the throttled chat's flood budget and leave the
    stale view message behind (the 2026-07-13 cascade)."""

    class ThrottledOnceBot(TurnBot):
        def __init__(self) -> None:
            super().__init__()
            self.throttles_left = 1

        async def edit_message_text(self, **kwargs: Any) -> FakeMessage:
            if "rich_message" in kwargs and self.throttles_left:
                self.throttles_left -= 1
                error = api_error("Too Many Requests: retry after 0")
                error.retry_after = 0  # ty: ignore[unresolved-attribute]
                raise error
            return await super().edit_message_text(**kwargs)

    bot = ThrottledOnceBot()
    turn = await Turn.attach(bot, GROUP_CHAT, 99)  # ty: ignore[invalid-argument-type]

    outcome = await turn.persist("The answer.")

    assert outcome.tier == "rich"
    assert len(bot.edit_calls) == 1  # the retry landed as the same edit
    assert bot.rich_calls == []  # no fresh send into the throttled chat
    assert bot.message_calls == []


async def test_over_budget_media_is_reported_through_the_outcome(tmp_path: Any):
    image = _write_png(tmp_path / "big.png")
    bot = TurnBot()
    turn = await Turn.create(bot, PRIVATE_CHAT, media_max_bytes=1)  # ty: ignore[invalid-argument-type]

    outcome = await turn.persist(f"![big]({image})")

    assert outcome.tier == "rich"
    assert [rejection.reason for rejection in outcome.rejected] == ["too_large"]
    assert bot.rich_calls[0]["rich_message"].media is None  # nothing could be attached
    assert bot.photo_calls == []  # undeliverable is not the same as tail-worthy


async def test_persist_twice_is_an_error():
    bot = TurnBot()
    turn = await Turn.create(bot, PRIVATE_CHAT)  # ty: ignore[invalid-argument-type]
    await turn.persist()
    with pytest.raises(RuntimeError, match="already persisted"):
        await turn.persist()


async def test_failed_turn_still_delivers_what_it_has():
    bot = TurnBot()
    turn = await Turn.create(bot, PRIVATE_CHAT)  # ty: ignore[invalid-argument-type]
    await turn.append_answer("Partial work.")

    await turn.fail("crashed")

    markdown = bot.rich_calls[0]["rich_message"].markdown
    assert "Partial work." in markdown
    assert "❌" in markdown
    assert "crashed" in markdown


async def test_discard_removes_an_edited_view_but_not_a_draft():
    bot = TurnBot()
    group_turn = await Turn.create(bot, GROUP_CHAT)  # ty: ignore[invalid-argument-type]
    message_id = group_turn.message_id
    await group_turn.discard()
    assert bot.deleted == [message_id]

    private_turn = await Turn.create(bot, PRIVATE_CHAT)  # ty: ignore[invalid-argument-type]
    await private_turn.discard()
    assert bot.deleted == [message_id]  # a draft lapses on its own


# ---------------------------------------------------------------------------
# Frame coalescing
# ---------------------------------------------------------------------------


async def test_intermediate_frames_are_dropped_not_queued():
    """Latest frame wins; a burst must not become a burst of API calls."""
    bot = TurnBot()
    turn = await Turn.create(bot, PRIVATE_CHAT)  # ty: ignore[invalid-argument-type]
    before = len(bot.draft_calls)
    try:
        for index in range(50):
            await turn.append_answer(f"chunk {index} ")
        await asyncio.sleep(0)
        assert len(bot.draft_calls) - before <= 1
    finally:
        await turn.stop()


# ---------------------------------------------------------------------------
# Typing keepalive
# ---------------------------------------------------------------------------


async def test_keep_typing_starts_and_stops_cleanly():
    calls = 0

    class TypingBot(TurnBot):
        async def send_chat_action(self, **_kwargs: Any) -> bool:
            nonlocal calls
            calls += 1
            return True

    bot = TypingBot()
    async with keep_typing(bot, PRIVATE_CHAT):  # ty: ignore[invalid-argument-type]
        await asyncio.sleep(0)
    assert calls >= 1
    # The refresher is cancelled on exit; nothing keeps running.
    await asyncio.sleep(0)
    assert calls >= 1


async def test_keep_typing_propagates_the_body_error():
    bot = TurnBot()
    with pytest.raises(ValueError, match="boom"):
        async with keep_typing(bot, PRIVATE_CHAT):  # ty: ignore[invalid-argument-type]
            raise ValueError("boom")


async def test_failing_an_unstarted_turn_sends_nothing():
    """The caller reports the error itself; a bare failure line would duplicate it."""
    bot = TurnBot()
    turn = Turn(bot, PRIVATE_CHAT)  # ty: ignore[invalid-argument-type]

    outcome = await turn.fail("agent error")

    assert bot.rich_calls == []
    assert bot.message_calls == []
    assert outcome.failures[0].reason == "agent error"


async def test_failing_a_started_turn_still_persists():
    bot = TurnBot()
    turn = await Turn.create(bot, PRIVATE_CHAT)  # ty: ignore[invalid-argument-type]
    await turn.add_activity("read", "🔧 Read config.py")

    await turn.fail("agent error")

    assert len(bot.rich_calls) == 1
    assert "❌" in bot.rich_calls[0]["rich_message"].markdown


class NotModifiedBot(TurnBot):
    """Models Telegram: a message holds content, and an edit repeating it fails.

    Crucially the *initial send* sets that content too — which is what made the
    very first group frame a guaranteed no-op edit in production.
    """

    def __init__(self) -> None:
        super().__init__()
        self.last_markdown: str | None = None

    async def send_message(self, **kwargs: Any) -> FakeMessage:
        self.last_markdown = kwargs.get("text")
        return await super().send_message(**kwargs)

    async def send_rich_message(self, **kwargs: Any) -> FakeMessage:
        self.last_markdown = kwargs["rich_message"].markdown
        return await super().send_rich_message(**kwargs)

    async def edit_message_text(self, **kwargs: Any) -> Any:
        markdown = kwargs["rich_message"].markdown
        if markdown == self.last_markdown:
            raise api_error(
                "Telegram server says - Bad Request: message is not modified: "
                "specified new message content and reply markup are exactly the same"
            )
        self.last_markdown = markdown
        return await super().edit_message_text(**kwargs)


async def test_first_group_frame_is_not_a_repeat_of_the_initial_send():
    """The initial send already displays a frame; repeating it is rejected.

    Reproduces the production trace: transport_selected immediately followed by
    view_failed "message is not modified", which then froze the whole turn.
    """
    bot = NotModifiedBot()
    turn = Turn(bot, GROUP_CHAT)  # ty: ignore[invalid-argument-type]
    turn._backoff.base_interval = 0.0
    turn._backoff.interval = 0.0
    try:
        # A frame whose content matches what the initial send already displayed.
        await turn._schedule()
        await asyncio.sleep(0)
        assert turn.transport == "edit"
        assert turn._transport_failed is False, "one benign rejection killed the view"
    finally:
        await turn.stop()


async def test_unchanged_view_never_kills_the_transport():
    """ "message is not modified" is benign; latching on it froze whole turns."""
    bot = NotModifiedBot()
    turn = await Turn.attach(bot, GROUP_CHAT, 99)  # ty: ignore[invalid-argument-type]
    try:
        transport = turn._transport
        assert transport is not None
        frame = "same content"
        assert await transport.show(frame) is True
        assert await transport.show(frame) is True  # repeat -> still fine
    finally:
        await turn.stop()


async def test_persisting_an_unchanged_view_does_not_duplicate_the_message():
    """Falling through to a fresh send here posted the answer twice."""
    bot = NotModifiedBot()
    turn = await Turn.attach(bot, GROUP_CHAT, 99)  # ty: ignore[invalid-argument-type]
    await turn.append_answer("The answer.")
    await asyncio.sleep(0)

    markdown = turn.view.persist_markdown()
    bot.last_markdown = markdown  # the final frame already showed this exactly

    outcome = await turn.persist()

    assert outcome.tier == "rich"
    assert bot.message_calls == []  # no duplicate fresh send
    assert bot.rich_calls == []


async def test_activity_updates_keep_flowing_after_a_no_op_frame():
    """The log showed a whole turn's activity lost after one benign rejection."""
    bot = NotModifiedBot()
    turn = await Turn.attach(bot, GROUP_CHAT, 99)  # ty: ignore[invalid-argument-type]
    turn._backoff.base_interval = 0.0
    turn._backoff.interval = 0.0
    try:
        for index in range(4):
            await turn.add_activity(f"step{index}", f"🔧 step {index}")
            await asyncio.sleep(0)
        assert turn._transport_failed is False
        assert bot.edit_calls, "no frame ever reached the chat"
    finally:
        await turn.stop()


# ---------------------------------------------------------------------------
# Event Classifier injection
# ---------------------------------------------------------------------------


def _classify(event: object, activity_limit: int) -> tuple[str, Any]:
    assert activity_limit == 200
    match event:
        case {"text": text}:
            return "text", text
        case {"tool": tool_id, "line": line}:
            return "activity", (tool_id, line)
    return "ignored", None


async def test_notify_dispatches_through_the_injected_classifier():
    bot = TurnBot()
    turn = await Turn.create(bot, GROUP_CHAT, classify_event=_classify)  # ty: ignore[invalid-argument-type]

    await turn.notify({"tool": "read", "line": "🔧 Read config.py"})
    await turn.notify({"text": "Done."})
    await turn.notify({"unrelated": True})
    await turn.notify({"tool": "read", "line": "🔧 Read config.py — 1s"})

    assert turn.view.answer == "Done."
    assert [activity.line for activity in turn.view.activities] == ["🔧 Read config.py — 1s"]
    await turn.discard()


async def test_notify_without_a_classifier_fails_fast():
    bot = TurnBot()
    turn = Turn(bot, GROUP_CHAT)  # ty: ignore[invalid-argument-type]

    with pytest.raises(RuntimeError, match="classify_event"):
        await turn.notify({"text": "never shown"})
    assert turn.view.answer == ""


async def test_notify_rejects_an_unknown_kind():
    def bogus(_event: object, _limit: int) -> tuple[str, Any]:
        return "thinking", None

    turn = Turn(TurnBot(), GROUP_CHAT, classify_event=bogus)  # ty: ignore[invalid-argument-type]

    with pytest.raises(ValueError, match="unknown kind"):
        await turn.notify(object())


async def test_turn_is_callable_as_an_event_sink():
    bot = TurnBot()
    turn = await Turn.create(bot, GROUP_CHAT, classify_event=_classify)  # ty: ignore[invalid-argument-type]

    await turn({"text": "via __call__"})

    assert turn.view.answer == "via __call__"
    await turn.discard()
