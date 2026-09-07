# tgkit

Delivery of bot replies to Telegram: formatting text so the API accepts it, sending it at the richest tier the chat allows, and streaming an agent's turn while it runs. Anything about *what* the reply says — agent events, speech, images — is the caller's domain and enters tgkit only through injected callables.

## Language

### Messages

**Rich Message**:
The structured message channel (Bot API 10.x): a document with native headings, tables, formulas, collapsible sections — separate from parse_mode text styling. The preferred final form of a bot reply.
_Avoid_: formatted message, MarkdownV2 message

**Delivery Outcome**:
The observable result of one send: which tier of the degradation ladder (Rich → legacy formatting → plain) actually delivered, the message ids, and the error chain. Fallback is automatic but never silent.

**Long-text fallback**:
The strategy for delivering a reply that exceeds the message limit: split into messages or attach as document.
_Avoid_: overflow handling, truncation, Telegraph

**Backoff**:
The adaptive wait policy after a Telegram rate limit: penalize on 429, recover gradually.
_Avoid_: retry logic, throttle

### Media

**Inline Media**:
Media rendered at its markdown position inside the final Rich Message, uploaded with the message itself. Photos only; video and audio are delivered separately.
_Avoid_: embedded media (ambiguous with the source-markdown embed), attachment

**Media Tail**:
The trailing media-group send carrying whatever the final message could not: every embed when the Rich tier was refused, the spill beyond the inline limit when it was not. A tail item arrives late but arrives; only a physically undeliverable file is rejected.
_Avoid_: fallback media, overflow group

### Turns

**Turn View**:
The single in-flight rendering of a reply: accumulated answer text first, then current activity and recent tools, then a status line. Identical content in private and group chats — private chats stream it as an ephemeral draft, groups edit a message at a paced cadence.
_Avoid_: progress display, draft progress, streaming progress

**Activity**:
One step of work inside a turn — a tool call, a phase. Ordered; the view shows the recent tail, the Activity Log shows all of them.

**Activity Log**:
The complete tool record of one turn, carried into the final message as a collapsed section. Expands on demand; never clutters the answer.

**Persist**:
Turning the Turn View into the durable final Rich Message at turn end. An unpersisted view vanishes; view failure never cancels the reply. Persisting is also when image embeds become Inline Media.

### Injected by the caller

**Event Classifier**:
The caller-supplied function that turns one agent event into what the Turn View shows: answer text, an Activity, or nothing. tgkit never interprets agent events itself.
_Avoid_: event handler, event adapter, event mapper

**Transcriber**:
The caller-supplied speech-to-text that turns downloaded voice bytes into text. Provider, language and caching belong to the caller.
_Avoid_: STT backend, whisper, speech provider
