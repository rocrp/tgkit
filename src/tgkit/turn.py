"""The live controller that drives a Turn View and persists it.

One protocol, two transports. Private chats stream the view as a rich draft at
~1.5 s; groups edit a message in place under the flood-safe 5 s cadence. The
content is identical — only the transport differs — and either way the turn
ends by persisting the view as a durable Rich Message.

A view is a convenience, never a precondition: if the transport dies mid-turn
the reply is still delivered, unstreamed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal, Protocol, Self

from aiogram import Bot

from ._log import logger
from .delivery import LongTextFallback, send_text
from .media import DEFAULT_MEDIA_MAX_BYTES, send_photos
from .message_draft import DraftOutcome
from .message_edit import EditMessageOutcome, edit_message_safe, is_not_modified
from .outcome import DeliveryOutcome, TierFailure
from .pacing import (
    _ELAPSED_TICK_SECONDS,
    _GROUP_MIN_UPDATE_INTERVAL,
    _MAX_UPDATE_INTERVAL,
    _MIN_UPDATE_INTERVAL,
    Backoff,
    _now,
)
from .retry import call_absorbing_rate_limits
from .rich import (
    RichContractError,
    RichPayload,
    build_rich_payload,
    edit_rich,
    is_rich_rejection,
    send_rich_draft,
)
from .turn_view import (
    DEFAULT_ACTIVITY_LIMIT,
    DEFAULT_RECENT_ACTIVITIES,
    TurnView,
    normalize_activity,
)

EventKind = Literal["text", "activity", "ignored"]
"""What an agent event means to the view: answer text, an activity line, or nothing."""

EventClassifier = Callable[[object, int], tuple[EventKind, Any]]
"""The caller's Event Classifier: ``(event, activity_limit) -> (kind, payload)``.

The payload is the answer text for ``"text"``, an ``(activity_id, line)`` pair
for ``"activity"``, and ignored for ``"ignored"``. tgkit never interprets agent
events itself; :meth:`Turn.notify` only dispatches on what the classifier says.
"""


class TurnTransport(Protocol):
    """How one turn's view reaches the chat."""

    @property
    def name(self) -> str: ...

    @property
    def base_interval(self) -> float: ...

    @property
    def shows_thinking(self) -> bool:
        """Whether this transport may carry the draft-only thinking block."""
        ...

    async def show(self, markdown: str) -> bool:
        """Render one frame. False means the transport is finished."""
        ...

    async def persist(self, payload: RichPayload, *, fallback: LongTextFallback) -> DeliveryOutcome:
        """Turn the view into the durable final message."""
        ...


class DraftTransport:
    """Private chats: an ephemeral rich draft, committed to a fresh message.

    A draft is a 30-second preview that vanishes on its own, so persisting is
    mandatory even when the last frame looked complete.
    """

    name = "draft"
    shows_thinking = True

    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        *,
        draft_id: int,
        thread_id: int | None = None,
        reply_to: int | None = None,
        disable_notification: bool = True,
        disable_link_preview: bool | None = None,
    ) -> None:
        if chat_id <= 0:
            raise RichContractError("draft transport is private-chat only")
        self.bot = bot
        self.chat_id = chat_id
        self.draft_id = draft_id
        self._thread_id = thread_id
        self._reply_to = reply_to
        self._disable_notification = disable_notification
        self._disable_link_preview = disable_link_preview

    @property
    def base_interval(self) -> float:
        return _MIN_UPDATE_INTERVAL

    async def show(self, markdown: str) -> bool:
        return await send_rich_draft(
            self.bot,
            chat_id=self.chat_id,
            draft_id=self.draft_id,
            markdown=markdown,
            thread_id=self._thread_id,
        )

    async def persist(self, payload: RichPayload, *, fallback: LongTextFallback) -> DeliveryOutcome:
        return await send_text(
            self.bot,
            chat_id=self.chat_id,
            text=payload,
            reply_to_message_id=self._reply_to,
            message_thread_id=self._thread_id,
            disable_notification=self._disable_notification,
            disable_link_preview=self._disable_link_preview,
            fallback=fallback,
        )


class EditTransport:
    """Groups and draft-less chats: one message, edited in place."""

    name = "edit"
    shows_thinking = False

    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        message_id: int,
        *,
        thread_id: int | None = None,
        disable_link_preview: bool | None = None,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.message_id = message_id
        self._thread_id = thread_id
        self._disable_link_preview = disable_link_preview

    @property
    def base_interval(self) -> float:
        # Groups share a ~20 calls/min per-chat flood budget and edits count
        # toward it, so a whole turn must fit inside that.
        return _MIN_UPDATE_INTERVAL if self.chat_id >= 0 else _GROUP_MIN_UPDATE_INTERVAL

    async def show(self, markdown: str) -> bool:
        try:
            await self._edit_rich(markdown)
        except Exception as error:
            # The message already reads exactly like this frame. Nothing to do,
            # and certainly no reason to tear the view down.
            if is_not_modified(error):
                return True
            if is_rich_rejection(error):
                result = await self._edit_safe(markdown)
                return result is not EditMessageOutcome.FAILED
            raise
        return True

    async def _edit_rich(self, markdown: str | RichPayload) -> Any:
        return await edit_rich(
            self.bot,
            chat_id=self.chat_id,
            message_id=self.message_id,
            markdown=markdown,
            disable_link_preview=self._disable_link_preview,
        )

    async def _edit_safe(self, markdown: str) -> EditMessageOutcome:
        return await edit_message_safe(
            self.bot,
            self.chat_id,
            self.message_id,
            markdown,
            reraise_rate_limit=True,
            disable_link_preview=self._disable_link_preview,
        )

    async def persist(self, payload: RichPayload, *, fallback: LongTextFallback) -> DeliveryOutcome:
        """Edit the view message into its final form, rather than posting anew.

        The edit uploads the payload's inline media with it — Telegram permits
        direct upload of new files on editMessageText for everything except
        inline-mode messages.

        Rate limits are absorbed in place: letting one escape would fall
        through to a fresh send into the very chat that is throttled, leaving
        the stale view message behind — the flood-cascade anti-pattern.
        """
        try:
            edited = await call_absorbing_rate_limits(lambda: self._edit_rich(payload))
        except Exception as error:
            if is_not_modified(error):
                # The last frame already was the final message; letting this
                # raise would fall through to a fresh send and duplicate it.
                return DeliveryOutcome(tier="rich")
            if not is_rich_rejection(error):
                raise
            logger.warning("turn.persist_edit_degraded", reason=str(error))
            result = await self._edit_safe(payload.degraded_markdown)
            if result is EditMessageOutcome.FAILED:
                # The view message cannot hold the answer; send it as its own.
                # The full ladder runs again, so a fresh Rich send may still
                # carry the inline media the edit could not.
                return await send_text(
                    self.bot,
                    chat_id=self.chat_id,
                    text=payload,
                    message_thread_id=self._thread_id,
                    disable_link_preview=self._disable_link_preview,
                    fallback=fallback,
                )
            return DeliveryOutcome(tier="markdownv2")
        # An edit returns the edited Message (True only for inline messages);
        # carrying it lets callers read the server's parsed rich content.
        messages = [edited] if edited is not None and edited is not True else []
        return DeliveryOutcome(tier="rich", messages=messages)


class Turn:
    """Drives one Turn View over the transport that fits the chat."""

    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        *,
        reply_to: int | None = None,
        message_thread_id: int | None = None,
        recent_activities: int = DEFAULT_RECENT_ACTIVITIES,
        activity_limit: int = DEFAULT_ACTIVITY_LIMIT,
        disable_notification: bool = True,
        disable_link_preview: bool | None = None,
        prefer_draft: bool = True,
        media_max_bytes: int = DEFAULT_MEDIA_MAX_BYTES,
        header: str | None = None,
        footer: str | None = None,
        answer_prefix: str | None = None,
        status_provider: Callable[[], str | None] | None = None,
        classify_event: EventClassifier | None = None,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self._classify_event = classify_event
        self._reply_to = reply_to
        self._thread_id = message_thread_id
        self._disable_notification = disable_notification
        self._disable_link_preview = disable_link_preview
        self._prefer_draft = prefer_draft
        self._media_max_bytes = media_max_bytes
        self.view = TurnView(
            recent_activities=recent_activities,
            activity_limit=activity_limit,
            header=header,
            footer=footer,
            answer_prefix=answer_prefix,
            status_provider=status_provider,
        )
        self._transport: TurnTransport | None = None
        self._backoff = Backoff()
        self._last_shown = ""
        self._last_update = 0.0
        self._pending: str | None = None
        self._show_task: asyncio.Task[None] | None = None
        self._ticker_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._stopped = False
        self._persisted = False
        # Permanent latch: a transport that could not be established once is not
        # retried every frame for the rest of the turn.
        self._transport_failed = False

    # -- construction --------------------------------------------------------

    @classmethod
    async def create(cls, bot: Bot, chat_id: int, **kwargs: Any) -> Self:
        turn = cls(bot, chat_id, **kwargs)
        await turn._select_transport()
        return turn

    @classmethod
    async def attach(cls, bot: Bot, chat_id: int, message_id: int, **kwargs: Any) -> Self:
        """Adopt an existing message as the view, instead of posting a new one."""
        turn = cls(bot, chat_id, **kwargs)
        turn._install(
            EditTransport(
                bot,
                chat_id,
                message_id,
                thread_id=turn._thread_id,
                disable_link_preview=turn._disable_link_preview,
            )
        )
        turn._start_ticker()
        return turn

    def _install(self, transport: TurnTransport) -> None:
        self._transport = transport
        self._backoff = Backoff(base_interval=transport.base_interval, max_interval=_MAX_UPDATE_INTERVAL)
        logger.info("turn.transport_selected", transport=transport.name, chat_id=self.chat_id)

    async def _select_transport(self) -> bool:
        """Pick a transport once, probing for draft support in private chats."""
        if self._transport is not None:
            return True
        if self._transport_failed:
            return False

        if self._prefer_draft and self.chat_id > 0:
            draft_id = int(_now() * 1000) % (2**31 - 1) or 1
            transport = DraftTransport(
                self.bot,
                self.chat_id,
                draft_id=draft_id,
                thread_id=self._thread_id,
                reply_to=self._reply_to,
                disable_notification=self._disable_notification,
                disable_link_preview=self._disable_link_preview,
            )
            probe = self.view.compose(thinking=True)
            try:
                if await transport.show(probe):
                    self._install(transport)
                    self._last_shown = probe
                    self._start_ticker()
                    return True
            except Exception as error:
                logger.warning("turn.draft_probe_failed", chat_id=self.chat_id, error=str(error))

        initial = self.view.compose()
        try:
            outcome = await send_text(
                self.bot,
                chat_id=self.chat_id,
                text=initial,
                reply_to_message_id=self._reply_to,
                message_thread_id=self._thread_id,
                disable_notification=self._disable_notification,
                disable_link_preview=self._disable_link_preview,
            )
        except Exception as error:
            self._transport_failed = True
            logger.warning("turn.transport_unavailable", chat_id=self.chat_id, error=str(error))
            return False
        if (message_id := outcome.message_id) is None:
            self._transport_failed = True
            logger.warning("turn.transport_unavailable", chat_id=self.chat_id, reason="no message id")
            return False

        self._install(
            EditTransport(
                self.bot,
                self.chat_id,
                message_id,
                thread_id=self._thread_id,
                disable_link_preview=self._disable_link_preview,
            )
        )
        # The initial send already displayed this frame; without recording it the
        # next edit repeats identical content and Telegram rejects it.
        self._last_shown = initial
        self._last_update = _now()
        self._start_ticker()
        return True

    # -- input ---------------------------------------------------------------

    async def append_answer(self, text: str) -> None:
        """Stream answer text into the view."""
        if not text:
            return
        async with self._lock:
            self.view.append_answer(text)
        await self._schedule()

    async def add_activity(self, activity_id: str, line: str) -> None:
        if (normalized := normalize_activity(activity_id, line)) is None:
            return
        async with self._lock:
            self.view.upsert_activity(*normalized)
        await self._schedule()

    async def notify(self, event: object) -> None:
        """Fold one agent event into the view through the injected Event Classifier."""
        if self._classify_event is None:
            raise RuntimeError("Turn.notify requires classify_event")
        kind, payload = self._classify_event(event, self.view.activity_limit)
        match kind:
            case "text":
                await self.append_answer(payload)
            case "activity":
                await self.add_activity(*payload)
            case "ignored":
                return
            case _:
                raise ValueError(f"classify_event returned unknown kind {kind!r}")

    async def __call__(self, event: object) -> None:
        await self.notify(event)

    # -- rendering -----------------------------------------------------------

    def _frame(self) -> str:
        thinking = bool(self._transport and self._transport.shows_thinking)
        return self.view.compose(thinking=thinking)

    async def _schedule(self) -> None:
        """Queue a frame; the latest one wins, intermediate frames are dropped."""
        if self._stopped or self._persisted:
            return
        self._pending = self._frame()
        if not self._show_task or self._show_task.done():
            self._show_task = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        """Coalescing drain: never queue frames, always render the newest."""
        try:
            while not self._stopped and not self._persisted:
                pending = self._pending
                if not pending or pending == self._last_shown:
                    return
                if wait := self._backoff.remaining(last_update=self._last_update, now=_now()):
                    await asyncio.sleep(wait)
                if self._stopped or self._persisted:
                    return
                pending = self._pending
                if pending and pending != self._last_shown and not await self._show(pending):
                    return
        except asyncio.CancelledError:
            pass
        finally:
            self._show_task = None

    async def _show(self, markdown: str) -> bool:
        if not await self._select_transport() or self._transport is None:
            return False
        try:
            shown = await self._transport.show(markdown)
        except Exception as error:
            if self._backoff.penalize(error):
                self._last_update = _now()
                logger.warning("turn.rate_limited", retry_after=getattr(error, "retry_after", None))
                return True
            # A broken view must never take the reply down with it.
            logger.warning("turn.view_failed", error=str(error), transport=self._transport.name)
            self._transport_failed = True
            return False
        if not shown:
            return False
        self._last_shown = markdown
        self._last_update = _now()
        self._backoff.recover()
        return True

    def _start_ticker(self) -> None:
        if not self._ticker_task or self._ticker_task.done():
            self._ticker_task = asyncio.create_task(self._tick())

    async def _tick(self) -> None:
        """Refresh the elapsed time even when nothing else changes."""
        try:
            while not self._stopped and not self._persisted:
                await asyncio.sleep(_ELAPSED_TICK_SECONDS)
                await self._schedule()
        except asyncio.CancelledError:
            pass

    # -- ending --------------------------------------------------------------

    async def persist(
        self,
        answer: str | None = None,
        *,
        note: str = "done",
        failed: bool = False,
        fallback: LongTextFallback = "document",
    ) -> DeliveryOutcome:
        """End the turn: render the durable message with its Activity Log.

        Always sends. A draft is ephemeral, and a view that failed mid-turn is
        no reason to withhold the answer.

        Photo embeds in the answer become Inline Media at their markdown
        position; whatever the final message could not carry rides the Media
        Tail (ADR 20260727). ``outcome.rejected`` reports the files that could
        not be delivered at all.
        """
        if self._persisted:
            raise RuntimeError("turn already persisted")
        self.view.fail(note) if failed else self.view.complete(note)
        await self.stop()
        self._persisted = True

        markdown = self.view.persist_markdown(answer=answer)
        # Photos only: an inline video upload would block the final text, and
        # video/document delivery stays a separate concern by ADR.
        async with build_rich_payload(
            markdown, max_bytes=self._media_max_bytes, inline_kinds=frozenset({"photo"})
        ) as payload:
            outcome = await self._deliver_payload(payload, fallback)
            outcome.rejected.extend(payload.rejected)
            tail = payload.undelivered_media(outcome.tier)
            await self._send_media_tail(tail, outcome)
        logger.info(
            "turn.persisted",
            tier=outcome.tier,
            inline_media=len(payload.media),
            tail=len(tail),
            rejected=len(outcome.rejected),
            chat_id=self.chat_id,
        )
        self._log_server_echo(outcome)
        return outcome

    async def _deliver_payload(self, payload: RichPayload, fallback: LongTextFallback) -> DeliveryOutcome:
        if self._transport is not None:
            try:
                return await self._transport.persist(payload, fallback=fallback)
            except Exception as error:
                logger.warning(
                    "turn.persist_transport_failed",
                    transport=self._transport.name,
                    error=str(error),
                )
        # No transport, or the transport's own persist failed: deliver plainly.
        return await send_text(
            self.bot,
            chat_id=self.chat_id,
            text=payload,
            reply_to_message_id=self._reply_to,
            message_thread_id=self._thread_id,
            disable_notification=self._disable_notification,
            disable_link_preview=self._disable_link_preview,
            fallback=fallback,
        )

    async def _send_media_tail(self, tail: list[Path], outcome: DeliveryOutcome) -> None:
        """Send whatever the final message could not carry as a media group.

        Sends absorb rate limits in place; a throttled chat is waited out,
        never re-entered with a delete-and-resend.
        """
        if not tail:
            return
        logger.info("turn.media_tail", count=len(tail), tier=outcome.tier, chat_id=self.chat_id)
        try:
            result = await send_photos(self.bot, chat_id=self.chat_id, paths=tail, thread_id=self._thread_id)
        except Exception as error:
            # The answer is already delivered; a dead tail must not undo that.
            logger.warning("turn.media_tail_failed", error=str(error))
            return
        outcome.rejected.extend(result.rejected)

    def _log_server_echo(self, outcome: DeliveryOutcome) -> None:
        """DEBUG-log the block structure the server says the message now has.

        The returned Message of a rich send or edit carries the parsed
        ``rich_message.blocks`` — the authoritative answer to "did the media
        actually land", discovered while live-verifying edit-with-upload.
        """
        rich = getattr(outcome.message, "rich_message", None)
        blocks = getattr(rich, "blocks", None)
        if not blocks:
            return
        counts: dict[str, int] = {}
        for block in blocks:
            kind = getattr(block, "type", "?")
            counts[kind] = counts.get(kind, 0) + 1
        logger.debug("turn.persisted_blocks", blocks=counts, chat_id=self.chat_id)

    async def fail(self, note: str, *, answer: str | None = None) -> DeliveryOutcome:
        """End the turn as failed, still delivering whatever answer exists.

        A turn that never displayed anything and produced no answer has nothing
        to persist. Sending a bare failure line there would duplicate whatever
        the caller is about to say about the error, so the turn just closes.
        """
        has_content = bool(answer or self.view.answer.strip() or self.view.activities)
        if not has_content and self._transport is None:
            self.view.fail(note)
            await self.stop()
            self._persisted = True
            return DeliveryOutcome(tier="rich", failures=[TierFailure(tier="rich", reason=note)])
        return await self.persist(answer, note=note, failed=True)

    async def stop(self) -> None:
        """Stop refreshing the view without ending the turn."""
        if self._stopped:
            return
        self._stopped = True
        for task in (self._show_task, self._ticker_task):
            if task and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    async def discard(self) -> None:
        """Abandon the view. A draft simply lapses; an edited message is removed."""
        await self.stop()
        self._persisted = True
        if isinstance(self._transport, EditTransport):
            with suppress(Exception):
                await self.bot.delete_message(chat_id=self.chat_id, message_id=self._transport.message_id)

    async def wait_for_cooldown(self, *, padding: float = 0.0) -> None:
        """Sleep out the transport's pacing budget before a final edit."""
        if wait := self._backoff.remaining(last_update=self._last_update, now=_now(), padding=padding):
            await asyncio.sleep(wait)

    # -- introspection -------------------------------------------------------

    @property
    def transport(self) -> str:
        return self._transport.name if self._transport else "none"

    @property
    def persisted(self) -> bool:
        """Whether the turn has already ended; ending it again would raise."""
        return self._persisted

    @property
    def message_id(self) -> int | None:
        return self._transport.message_id if isinstance(self._transport, EditTransport) else None


__all__ = [
    "DraftOutcome",
    "DraftTransport",
    "EditTransport",
    "EventClassifier",
    "EventKind",
    "Turn",
    "TurnTransport",
]
