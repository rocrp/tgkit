"""The observable result of one Telegram delivery.

Every send returns a :class:`DeliveryOutcome` saying which tier of the
degradation ladder actually delivered, which messages it produced, and what
each skipped tier failed with. Fallback is automatic but never silent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from aiogram.types import Message

if TYPE_CHECKING:
    from .media import MediaRejection


DeliveryTier = Literal["rich", "markdownv2", "plain"]

TIER_ORDER: tuple[DeliveryTier, ...] = ("rich", "markdownv2", "plain")


@dataclass(frozen=True, slots=True)
class TierFailure:
    """Why one tier of the ladder did not deliver."""

    tier: DeliveryTier
    reason: str

    def __str__(self) -> str:
        return f"{self.tier}: {self.reason}"


@dataclass(slots=True)
class DeliveryOutcome:
    """What one send actually did."""

    tier: DeliveryTier
    messages: list[Message] = field(default_factory=list)
    failures: list[TierFailure] = field(default_factory=list)
    rejected: list[MediaRejection] = field(default_factory=list)
    """Media that was physically undeliverable — never a formatting casualty."""

    @property
    def message_ids(self) -> list[int]:
        """Ids of every message produced; a split reply produces several."""
        return [message.message_id for message in self.messages]

    @property
    def message_id(self) -> int | None:
        """Id of the last message, which is what a caller edits or replies to."""
        ids = self.message_ids
        return ids[-1] if ids else None

    @property
    def message(self) -> Message | None:
        return self.messages[-1] if self.messages else None

    @property
    def degraded(self) -> bool:
        """Whether delivery fell below the Rich tier."""
        return self.tier != "rich"

    @property
    def error_chain(self) -> str:
        """One-line summary of every tier that was tried and failed."""
        return " -> ".join(str(failure) for failure in self.failures)


__all__ = [
    "TIER_ORDER",
    "DeliveryOutcome",
    "DeliveryTier",
    "TierFailure",
]
