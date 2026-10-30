"""The frozen public surface of tgkit."""

from __future__ import annotations

# ruff: noqa: E501
import importlib
import inspect
import re
from typing import get_args

import pytest
import tgkit
from tgkit.turn import DraftTransport, EditTransport, Turn


def test_delivery_public_signatures_are_frozen() -> None:
    """The delivery surface after the Rich-first rework (ADR 20260725)."""
    expected = {
        "send_text": "(bot: 'Bot', *, chat_id: 'int', text: 'str | RichPayload', tier: 'DeliveryTier | None' = None, reply_to_message_id: 'int | None' = None, message_thread_id: 'int | None' = None, disable_notification: 'bool | None' = None, disable_link_preview: 'bool | None' = None, fallback: 'LongTextFallback' = 'document') -> 'DeliveryOutcome'",
        "reply_text": "(message: 'Message', text: 'str', *, tier: 'DeliveryTier | None' = None, fallback: 'LongTextFallback' = 'document', disable_link_preview: 'bool | None' = None) -> 'DeliveryOutcome'",
        "send_rich": "(bot: 'Bot', *, chat_id: 'int', markdown: 'str | RichPayload', thread_id: 'int | None' = None, reply_params: 'Any' = None, disable_notification: 'bool | None' = None, protect_content: 'bool | None' = None) -> 'Message'",
        "send_rich_draft": "(bot: 'Bot', *, chat_id: 'int', draft_id: 'int', markdown: 'str | RichPayload', thread_id: 'int | None' = None) -> 'bool'",
        "edit_rich": "(bot: 'Bot', *, chat_id: 'int', message_id: 'int', markdown: 'str | RichPayload', disable_link_preview: 'bool | None' = None) -> 'Any'",
        "edit_message_safe": "(bot: 'Bot', chat_id: 'int', message_id: 'int', text: 'str', parse_mode: 'str | None' = 'MarkdownV2', *, reraise_rate_limit: 'bool' = False, disable_link_preview: 'bool | None' = None) -> 'EditMessageOutcome'",
        "send_photo": "(bot: 'Bot', *, chat_id: 'int', photo: 'Any', caption: 'str | None' = None, parse_mode: 'str | None' = 'MarkdownV2', reply_to_message_id: 'int | None' = None) -> 'DeliveryOutcome'",
        "reply_photo": "(message: 'Message', photo: 'Any', caption: 'str | None' = None, parse_mode: 'str | None' = 'MarkdownV2') -> 'DeliveryOutcome'",
        "delete_message_safe": "(bot: 'Bot', chat_id: 'int', message_id: 'int') -> 'bool'",
        "send_typing": "(bot: 'Bot', chat_id: 'int', *, message_thread_id: 'int | None' = None) -> 'None'",
        "split_message": "(text: 'str', max_len: 'int' = 4096) -> 'list[str]'",
        "reply_document": "(message: 'Message', content: 'str | bytes', filename: 'str', caption: 'str | None' = None) -> 'DeliveryOutcome'",
        "reply_file": "(message: 'Message', file_path: 'Path', caption: 'str | None' = None) -> 'DeliveryOutcome'",
        "with_retry": "(coro_fn: 'Callable[P, Awaitable[T]]', *args: 'P.args', **kwargs: 'P.kwargs') -> 'T'",
    }

    for name, signature in expected.items():
        assert str(inspect.signature(getattr(tgkit, name))) == signature, name


def test_long_text_fallback_narrowed_to_split_and_document() -> None:
    """Telegraph is retired; the fallback type must not readmit it."""
    from tgkit.delivery import LongTextFallback

    assert set(get_args(LongTextFallback)[0].__args__) == {"split", "document"}


def test_exports_are_eager() -> None:
    """No lazy ``__getattr__``: every name is bound at import time."""
    assert "__getattr__" not in vars(tgkit)
    missing = [name for name in tgkit.__all__ if name not in vars(tgkit)]
    assert missing == []


def test_new_surface_after_extraction() -> None:
    assert isinstance(tgkit.MEDIA_EMBED_RE, re.Pattern)
    assert tgkit.MEDIA_EMBED_RE.match("![alt](/tmp/a.png)")
    assert set(get_args(tgkit.EventKind)) == {"text", "activity", "ignored"}
    assert tgkit.EventClassifier is tgkit.turn.EventClassifier


def test_private_peers_are_not_exported() -> None:
    """Voice, photo and pacing stay submodule-only; nothing describes images any more."""
    for name in (
        "transcribe_voice_text",
        "download_voice_bytes",
        "download_photo_bytes",
        "describe_photo_text",
        "TelegramPhotoDescriptionError",
        "Backoff",
        "ensure_aiogram",
    ):
        assert name not in tgkit.__all__
        assert not hasattr(tgkit, name), name
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("tgkit._api")


def test_turn_view_public_signatures_are_frozen() -> None:
    turn_signature = "(self, bot: 'Bot', chat_id: 'int', *, reply_to: 'int | None' = None, message_thread_id: 'int | None' = None, recent_activities: 'int' = 6, activity_limit: 'int' = 200, disable_notification: 'bool' = True, disable_link_preview: 'bool | None' = None, prefer_draft: 'bool' = True, media_max_bytes: 'int' = 20971520, header: 'str | None' = None, footer: 'str | None' = None, answer_prefix: 'str | None' = None, status_provider: 'Callable[[], str | None] | None' = None, classify_event: 'EventClassifier | None' = None) -> 'None'"
    persist_signature = "(self, answer: 'str | None' = None, *, note: 'str' = 'done', failed: 'bool' = False, fallback: 'LongTextFallback' = 'document') -> 'DeliveryOutcome'"

    assert str(inspect.signature(Turn.__init__)) == turn_signature
    assert str(inspect.signature(Turn.persist)) == persist_signature
    assert str(inspect.signature(tgkit.keep_typing)) == (
        "(bot: 'Bot', chat_id: 'int', *, thread_id: 'int | None' = None) -> 'AsyncIterator[None]'"
    )


def test_voice_transcriber_is_injected() -> None:
    from tgkit.voice import transcribe_voice_text

    assert str(inspect.signature(transcribe_voice_text)) == (
        "(bot: '_Bot', voice: '_Voice', *, transcribe: 'Callable[[bytes], Awaitable[str]]') -> 'str'"
    )


def test_both_transports_satisfy_one_protocol() -> None:
    for transport in (DraftTransport, EditTransport):
        for member in ("name", "base_interval", "shows_thinking", "show", "persist"):
            assert hasattr(transport, member), (transport.__name__, member)
    assert DraftTransport.shows_thinking is True
    assert EditTransport.shows_thinking is False


def test_package_exports_and_submodule_surfaces_agree() -> None:
    """Anything a submodule declares public must be importable from the package."""
    modules = [
        "constants",
        "delivery",
        "markdown",
        "markdown_utils",
        "media",
        "message_draft",
        "message_edit",
        "message_misc",
        "outcome",
        "reply",
        "retry",
        "rich",
        "turn",
        "turn_view",
    ]
    exported = set(tgkit.__all__)
    gaps: dict[str, list[str]] = {}
    for name in modules:
        module = importlib.import_module(f"tgkit.{name}")
        missing = [
            symbol for symbol in getattr(module, "__all__", []) if not symbol.startswith("_") and symbol not in exported
        ]
        if missing:
            gaps[name] = sorted(missing)
    assert gaps == {}
