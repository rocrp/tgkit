# Pitfall: Telegram MarkdownV2 fence close must separate next segment

Date: 2026-01-13 (UTC)

## Status

Accepted

## Context

- Symptom: Telegram parse error "Character '>' reserved..." on auto-collapsed replies
- Trigger: code-fence close glued to following text (```next, >```>) esp. blockquote segments
- Constraint: MarkdownV2 requires '>' only at line start

## Decision

- Always add newline after closing fence when another segment follows
- Keep fence language on opening line; never inject '>' mid-line
- Sanitize auto-collapsed headings via strip_markdown to avoid nested bold

## Consequences

- + Prevents MarkdownV2 parse errors, stable auto-collapsed previews
- - Adds newline between segments (format change)
- ? Regression tests for fence separation + heading sanitization
