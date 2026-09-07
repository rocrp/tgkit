"""Stable Telegram reply facade.

Implementation lives in focused delivery modules; this module is the single
import point bots use.
"""

from __future__ import annotations

from .delivery import (
    LongTextFallback,
    link_preview_options,
    reply_parameters,
    reply_text,
    send_text,
)
from .markdown_utils import split_message
from .message_draft import DraftOutcome, is_draft_unsupported_error, send_message_draft_safe
from .message_edit import (
    EditMessageOutcome,
    delete_message_safe,
    edit_message_safe,
    edit_message_text_raw,
    is_not_modified,
)
from .message_misc import (
    fit_caption,
    keep_typing,
    reply_document,
    reply_file,
    reply_photo,
    send_photo,
    send_typing,
)
from .outcome import TIER_ORDER, DeliveryOutcome, DeliveryTier, TierFailure
from .retry import RateLimitError, with_retry

__all__ = [
    "TIER_ORDER",
    "DeliveryOutcome",
    "DeliveryTier",
    "DraftOutcome",
    "EditMessageOutcome",
    "LongTextFallback",
    "RateLimitError",
    "TierFailure",
    "delete_message_safe",
    "edit_message_safe",
    "edit_message_text_raw",
    "fit_caption",
    "is_draft_unsupported_error",
    "is_not_modified",
    "keep_typing",
    "link_preview_options",
    "reply_document",
    "reply_file",
    "reply_parameters",
    "reply_photo",
    "reply_text",
    "send_message_draft_safe",
    "send_photo",
    "send_text",
    "send_typing",
    "split_message",
    "with_retry",
]
