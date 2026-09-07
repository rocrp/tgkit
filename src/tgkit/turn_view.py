"""The Turn View: one in-flight rendering of a reply.

The answer comes first. Tool activity and the status line sit underneath it,
because the reader is waiting for the answer, not for a list of the tools that
produced it. The same content renders in private and group chats; only the
transport and its cadence differ.

At turn end the view is *persisted* into the final Rich Message, where the
complete tool record folds into a collapsed **Activity Log** instead of being
dropped or left cluttering the answer.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from .constants import MAX_RICH_MESSAGE_LENGTH, prefix_within_utf16_limit, rich_len
from .rich import THINKING_CLOSE, THINKING_OPEN

TurnState = Literal["active", "done", "failed"]

DEFAULT_RECENT_ACTIVITIES = 6
DEFAULT_ACTIVITY_LIMIT = 200

_STATE_ICONS: dict[TurnState, str] = {"active": "⏳", "done": "✅", "failed": "❌"}


def _format_duration(seconds: float) -> str:
    """Compact duration: ``45s``, ``3m12s``, ``2h05m``."""
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


@dataclass(slots=True)
class Activity:
    """One step of work in a turn — a tool call, a phase, a status."""

    id: str
    line: str
    created_at: float
    duration: float | None = None

    def render(self) -> str:
        if self.duration is None:
            return f"- {self.line}"
        return f"- {self.line} — {_format_duration(self.duration)}"


def normalize_activity(
    activity_id: str,
    line: str,
    *,
    skip_ids: set[str] | None = None,
) -> tuple[str, str] | None:
    """Validate an activity, returning None when the id is deliberately skipped."""
    if not activity_id:
        raise ValueError("activity id required")
    if activity_id in (DEFAULT_SKIPPED_ACTIVITY_IDS if skip_ids is None else skip_ids):
        return None
    if not isinstance(line, str):
        raise ValueError("activity line must be a string")
    if not (cleaned := line.strip()):
        raise ValueError("activity line empty")
    if "\n" in cleaned or "\r" in cleaned:
        raise ValueError("activity line must be single-line")
    return activity_id, cleaned


DEFAULT_SKIPPED_ACTIVITY_IDS = frozenset({"output"})


@dataclass(slots=True)
class TurnView:
    """Bot-free state and rendering for one turn."""

    recent_activities: int = DEFAULT_RECENT_ACTIVITIES
    activity_limit: int = DEFAULT_ACTIVITY_LIMIT
    header: str | None = None
    footer: str | None = None
    answer_prefix: str | None = None
    status_provider: Callable[[], str | None] | None = None
    started_at: float = field(default_factory=lambda: time.monotonic())

    answer: str = ""
    state: TurnState = "active"
    state_note: str | None = None
    # Every activity of the turn, in order. The in-flight view shows the tail;
    # the Activity Log in the persisted message shows all of them.
    activities: list[Activity] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.recent_activities < 0:
            raise ValueError("recent_activities must be >= 0")
        self.header = self.header.strip() if self.header else None
        self.footer = self.footer.strip() if self.footer else None

    # -- state ---------------------------------------------------------------

    def append_answer(self, text: str) -> None:
        """Add streamed answer text."""
        self.answer += text

    def set_answer(self, text: str) -> None:
        self.answer = text

    def upsert_activity(self, activity_id: str, line: str, *, duration: float | None = None) -> None:
        """Record or update one activity, keeping its original position."""
        for activity in self.activities:
            if activity.id == activity_id:
                activity.line = line
                activity.duration = duration
                return
        self.activities.append(
            Activity(
                id=activity_id,
                line=line,
                created_at=time.monotonic(),
                duration=duration,
            )
        )

    def complete(self, note: str) -> None:
        self.state = "done"
        self.state_note = note

    def fail(self, note: str) -> None:
        self.state = "failed"
        self.state_note = note

    # -- rendering -----------------------------------------------------------

    def status_line(self, *, now: float | None = None) -> str:
        elapsed = _format_duration((time.monotonic() if now is None else now) - self.started_at)
        parts = [f"{_STATE_ICONS[self.state]} {elapsed}"]
        if self.state_note:
            parts.append(self.state_note)
        if self.status_provider and (line := self.status_provider()) and (line := line.strip()):
            parts.append(line)
        return " • ".join(parts)

    def _answer_block(self) -> str:
        if not self.answer.strip():
            return ""
        if not self.answer_prefix:
            return self.answer.strip()
        return "\n".join(f"{self.answer_prefix}{line}" for line in self.answer.strip().splitlines())

    def compose(self, *, thinking: bool = False, now: float | None = None) -> str:
        """Render the in-flight view.

        ``thinking`` wraps the activity section in the draft-only thinking
        block; it must stay False for anything that will be persisted.
        """
        blocks: list[str] = []
        if self.header:
            blocks.append(self.header)
        if answer := self._answer_block():
            blocks.append(answer)

        recent = self.activities[-self.recent_activities :] if self.recent_activities else []
        activity_block = "\n".join(activity.render() for activity in recent)
        status = self.status_line(now=now)

        if thinking:
            inner = f"{activity_block}\n{status}" if activity_block else status
            blocks.append(f"{THINKING_OPEN}\n{inner}\n{THINKING_CLOSE}")
        else:
            if activity_block:
                blocks.append(activity_block)
            blocks.append(status)

        if self.footer:
            blocks.append(self.footer)
        return _fit("\n\n".join(block for block in blocks if block))

    def activity_log(self) -> str:
        """The full tool record, folded into a collapsed section."""
        if not self.activities:
            return ""
        summary = f"Activity Log ({len(self.activities)} steps)"
        body = "\n".join(activity.render() for activity in self.activities)
        return f"<details><summary>{summary}</summary>\n\n{body}\n\n</details>"

    def persist_markdown(self, *, answer: str | None = None) -> str:
        """Render the durable final message: the answer plus the Activity Log.

        Never carries the thinking block — that tag is valid only in drafts.
        """
        if answer is not None:
            self.set_answer(answer)
        blocks: list[str] = []
        if self.header:
            blocks.append(self.header)
        if body := self._answer_block():
            blocks.append(body)
        if log := self.activity_log():
            blocks.append(log)
        # A finished turn does not need a stopwatch, but a failed one must say
        # so: without it a crash reads as a complete answer.
        if self.state == "failed" or not blocks:
            blocks.append(self.status_line())
        if self.footer:
            blocks.append(self.footer)
        return _fit("\n\n".join(blocks))


def _fit(markdown: str) -> str:
    """Keep a rendering inside the Rich budget without ever collapsing content."""
    if rich_len(markdown) <= MAX_RICH_MESSAGE_LENGTH:
        return markdown
    return prefix_within_utf16_limit(markdown, MAX_RICH_MESSAGE_LENGTH - 1) + "…"


__all__ = [
    "Activity",
    "TurnState",
    "TurnView",
    "normalize_activity",
]
