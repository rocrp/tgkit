---
status: accepted
date: 2026-09-07
---

# tgkit knows no private packages; agent events, speech and vision are injected

tgkit was extracted from a private monolith where the Telegram package had soft peers: `codeagentlayer` (agent event classification, image MIME sniffing), `rocpy.audiox` (speech-to-text) and `agenttools` (image description) were imported inside functions behind `ImportError` guards, so the package "worked" without them and silently lost features. As a standalone library tgkit imports none of them. Agent-event classification is an injected callable (`Turn(classify_event=)`, the **Event Classifier**), speech-to-text is an injected callable (`transcribe_voice_text(transcribe=)`, the **Transcriber**), image description moved to the consumer entirely, and MIME sniffing is a few lines of magic-byte matching inside tgkit.

## Considered Options

| Option | Verdict | Why |
|---|---|---|
| Keep the `ImportError` guards, document the peers | Rejected | private packages cannot be installed by anyone else; a guard that always fails is dead code, and one that silently degrades hides the failure (FAIL FAST) |
| Optional extras (`tgkit[agent]`, `tgkit[stt]`) wrapping public equivalents | Rejected | pins tgkit to one agent framework and one STT vendor; the actual consumers each use a different one |
| Injection: caller passes callables | **Chosen** | the seam is one function type per concern, testable with a lambda, and tgkit's dependency set is exactly aiogram + Pillow |

## Consequences

- `Turn.notify` raises `RuntimeError` when no Event Classifier was supplied; `append_answer` / `add_activity` remain usable without one.
- `transcribe_voice_text` and `Turn` gained one keyword argument each; `describe_photo_text` and `TelegramPhotoDescriptionError` are gone, `download_photo_bytes` stays.
- `rg "codeagentlayer|agenttools|rocpy" src tests` must stay empty; it is the cheapest regression check for this decision.
- Consumers own the mapping from their event model to `("text" | "activity" | "ignored", payload)`, and any logging of raw events.
