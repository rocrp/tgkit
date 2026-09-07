# Rich-first Telegram delivery; MarkdownV2 demoted to fallback; Telegraph retired

Date: 2026-07-25
Status: Accepted

## Context

- Bot API 10.1/10.2 (2026-06/07) added Rich Messages: `sendRichMessage` / `sendRichMessageDraft` / `editMessageText(rich_message=)` with a GFM-compatible Rich Markdown dialect — native headings, tables, `$LaTeX$`, task lists, `<details>`, footnotes; 32768-char limit (vs 4096), 500 blocks, 50 inline media.
- tgkit's MarkdownV2 pipeline (13-step placeholder escaping, latex→unicode, auto-collapse, Telegraph overflow) exists to squeeze LLM output through `parse_mode=MarkdownV2` + 4096. Rich Messages remove the pressure that justified all of it.
- All consumers (rccc, codeagentg, rcmacbot, +2 bots) are personal, on this machine, editable installs — migration cost declared irrelevant by owner.

## Decision

- Primary send path is Rich Markdown. Degradation ladder: **Rich Markdown → simplified MarkdownV2 pipeline → plain**, automatic but observable via a unified Delivery Outcome (tier, message ids, error chain). Re-chunk against 4096 when falling below Rich.
- The existing MarkdownV2 pipeline survives only as the fallback tier, stripped of latex2unicode and auto-collapse.
- Telegraph is retired from delivery: the glue and the `telegraph`/`force_telegraph` fallback options are gone, and long-text fallback narrows to `split | document`. The client and its markdown→nodes converter survive only as the private backing for the standalone `telegraph` CLI, which is not part of the Telegram delivery surface.
- In-flight UX is one **Turn View** (answer text first, activity + status after) in all chat types; private chats stream it via rich draft (~1.5 s), groups edit-in-place under the shipped 5 s + Backoff pacing. Final message persists with the full tool record folded into a collapsed **Activity Log** section.
- Not adopted: HTML as the middle fallback tier, message effects, ephemeral messages, bot-sent reactions, checklists, auto-collapse of answers.

## Alternatives Considered

| Option | Result | Reason |
|---|---|---|
| Rich → MarkdownV2 → plain | Chosen | fallback tier already battle-tested in-repo; zero new converter code |
| Rich → HTML → plain (blog.cosine.ren rec) | Rejected | would add a new HTML converter to save a pipeline we already own |
| Keep Telegraph for long-form reading | Rejected for delivery | only real delivery users were rcmacbot `/article` `/research`; 32768 inline covers them. The standalone `telegraph` CLI is a separate tool and keeps its client (owner's call, 2026-07-25) |
| Rich as opt-in per caller | Rejected | five bots would migrate never; owner explicitly discards backward compatibility |

## Consequences

- + ~875 LOC deleted (`latex2unicode.py`, `markdown_collapse.py`, `telegraph_delivery.py`). The Telegraph client stays for the CLI, so the external service dependency is out of the bot path but not out of the repo.
- + Five coexisting error conventions collapse into Delivery Outcome; `disable_web_page_preview`/`reply_to_message_id` (deprecated since Bot API 7.0) replaced by `LinkPreviewOptions`/`ReplyParameters`.
- - Fallback-tier output degrades: raw `$x^2$` shown for formulas, no collapse. Accepted — fallback is a rare event.
- - Group in-flight cadence stays 5–30 s (edit budget is platform physics; drafts are private-only).
- ✓ Verified: aiogram 3.29.1 is Bot API 10.1 only. 3.30.0 (2026-07-17) carries 10.2 — `InputRichMessage.blocks`/`.media`, `InputRichMessageMedia`, `InputMediaVoiceNote`. All four repos now require `aiogram>=3.30` and vendor a `[tool.uv] exclude-newer` past that date, because a lower ambient `~/.config/uv/uv.toml` value hides the release and makes `uv sync --locked` fail.

## Notes

- Vocabulary: CONTEXT.md "Telegram Delivery" — Rich Message, Turn View, Activity Log, Persist, Delivery Outcome, Long-text fallback.
- The 2026-07-13 public-surface freeze is lifted; `tests/test_public_api.py` re-pins the new surface.
- Draft contract: `sendRichMessageDraft` is a 30 s ephemeral preview, private chats only, `draft_id` non-zero and stable per stream; the final `sendRichMessage` is mandatory even if the last frame looked complete. Draft failure never cancels the reply.
- Supersedes the premises of closed issues #108 (long-text fallback assumed Telegraph) and #109 (ProgressView deepening, replaced by Turn View).
