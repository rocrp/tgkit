# Persist inlines image embeds; whatever the final message cannot carry rides a Media Tail

Date: 2026-07-27
Status: Accepted

## Context

- The Rich-first ADR (`20260725-rich-first-telegram-delivery.md`) promised "images inline at markdown position" but shipped without it: consumers (rccc et al.) still strip `![alt](/abs/path)` embeds and send a separate media group after the answer. `rich.py:build_rich_payload` — the full inline mechanism (find embeds → resize/budget-gate → rewrite to `tg://photo?id=` → `RichPayload`) — existed but had zero callers.
- Load-bearing API fact, verified against core.telegram.org 2026-07-27: `editMessageText(rich_message=)` **supports direct upload of new files**; the docs prohibit it only "when an inline message is edited". So the group-chat EditTransport can inline media on persist — no delete+resend needed. This unblocked the whole design.
- Constraint: the fallback tiers (MarkdownV2/plain) cannot render `tg://` references or carry inline media, and drafts cannot carry media at all.

## Decision

- **Persist owns inlining.** `Turn.persist` runs the view markdown through `build_rich_payload`; photo embeds upload inline at their markdown position on the Rich tier — both transports (fresh `sendRichMessage` and `editMessageText(rich_message=)`).
- **Default-on for every consumer.** `Turn` takes `media_max_bytes` (default 20 MB); callers pass their own policy value (rccc: `RCCC_MEDIA_MAX_MB`). No opt-in flag — same judgement as Rich-as-default.
- **Photos only.** Video/audio embeds are not inlined even though `tg://video`/`tg://audio` exist: a multi-MB upload would block the final text (answer-first), and real traffic is ~zero. One-line filter to revisit.
- **Leftovers ride the Media Tail.** One rule covers degradation and overflow: any embed the final message could not carry is sent as a trailing media group after the text. Rich rejected → all embeds go to the tail (alt text stays at position — the pre-ADR behavior *is* the degraded story). Rich accepted with >50 embeds → the 51st onward spill to the tail. `MediaRejection` narrows to one meaning: physically undeliverable (over budget, unreadable) — reported via `DeliveryOutcome`, never silently dropped.
- **GIF stays in the photo bucket** (static). The media-group path already flattened GIFs to photos, so this loses nothing; fixing animation is a separate concern (`InputMediaAnimation` exists in the union but has no documented `tg://` scheme).
- Tail sends absorb rate limits in place (sleep-and-retry); never delete+resend into a throttled chat.

## Alternatives Considered

| Option | Result | Reason |
|---|---|---|
| Persist inlines; leftovers → Media Tail | Chosen | degraded UX = today's proven UX; one leftover rule; `rejected` keeps one meaning |
| Caller builds `RichPayload`, passes to persist | Rejected | mechanism split across every consumer; degradation still lands in tgkit anyway |
| Drop leftovers as rejections | Rejected | promotes a formatting limit into content loss; ladder exists to guarantee delivery |
| All-or-nothing: Rich refused → media-group-with-caption | Rejected | second full delivery pipeline; 1024-char caption re-splits long answers |
| Inline video/audio too | Deferred | revisit when a consumer actually emits them; needs answer-first latency check |
| GIF → `InputMediaAnimation` via `tg://video` | Rejected | undocumented API behavior; pathological frequency |

## Consequences

- + Every `Turn` consumer (rccc, codeagentg, rcmacbot, siu bots) gets positional images with no wiring.
- + Degradation and overflow share one code path (the tail) and one vocabulary.
- - A rate-limited persist retried by the caller re-uploads media bytes. Accepted: rare, correctness over bandwidth.
- - Tail media group loses its caption (text already delivered); order flips from media-first to text-first. Accepted: answer-first.
- ? Group-chat edit-with-upload is doc-verified but not yet live-exercised; `TelegramTurnDelivery` has zero automated coverage — first live group turn with an image is the oracle.

## Notes

- Revisit photo-only inlining if a consumer starts emitting video embeds; revisit GIF if animation loss ever hurts.
- Consumer wiring (rccc): RoCry/rccc#43. Vocabulary: CONTEXT.md "Telegram Delivery" — Inline Media, Media Tail.
