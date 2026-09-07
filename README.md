# tgkit

Delivery toolkit on top of [aiogram](https://github.com/aiogram/aiogram) 3.30+.

- **Telegram-safe markdown**: escape/format LLM output for `parse_mode=MarkdownV2`, split at the 4096 UTF-16 limit, truncate captions after escaping.
- **Rate-limit-aware sends**: every send/edit absorbs `retry after` in place; formatting rejections degrade to plain text, never silently.
- **Degradation ladder**: Rich Message → MarkdownV2 → plain. `DeliveryOutcome` tells you which tier landed and why the others did not.
- **Rich Messages** (Bot API 10.x): 32k-char GFM documents, `![alt](/abs/path)` embeds lifted into inline media, spill and rejections reported.
- **Streamed agent turns**: `Turn` renders one in-flight reply (answer + activity + status), streams it as a draft in private chats or paced edits in groups, then persists it as a durable Rich Message with a collapsed Activity Log.

tgkit knows nothing about your agent framework, STT provider or vision model. Those are injected.

## Install

```sh
uv add tgkit
```

Python ≥ 3.12. Hard deps: `aiogram>=3.30`, `pillow>=11`.

## Send text

```python
from aiogram import Bot
from tgkit import send_text, with_retry

bot = Bot(token="...")

async def main() -> None:
    outcome = await with_retry(send_text, bot, chat_id=123, text="# Hello\n\n| a | b |\n|---|---|\n| 1 | 2 |")
    print(outcome.tier)          # "rich" | "markdownv2" | "plain"
    print(outcome.message_id)    # last message produced
    if outcome.degraded:
        print(outcome.error_chain)
```

`send_text` walks the ladder itself; `with_retry` adds the outer retry-after policy for the whole call.

## Stream an agent turn

Supply an **Event Classifier**: `(event, activity_limit) -> (kind, payload)` where kind is `"text"` (payload: str), `"activity"` (payload: `(activity_id, line)`) or `"ignored"`. tgkit never interprets your events.

```python
from tgkit import EventKind, Turn

def classify(event: object, activity_limit: int) -> tuple[EventKind, object]:
    match event:
        case {"type": "text", "delta": delta}:
            return "text", delta
        case {"type": "tool", "id": tool_id, "name": name}:
            return "activity", (tool_id, f"🔧 {name}")
    return "ignored", None

async def run(bot, chat_id: int, agent) -> None:
    turn = await Turn.create(bot, chat_id, classify_event=classify)
    async for event in agent.stream():
        await turn.notify(event)
    await turn.persist()          # final Rich Message + Activity Log
```

`Turn.notify` raises `RuntimeError` when no classifier was given; `append_answer` / `add_activity` work without one.

## Transcribe a voice note

Supply a **Transcriber**: `async (bytes) -> str`.

```python
from tgkit.voice import transcribe_voice_text

async def whisper(audio: bytes) -> str:
    ...  # your STT call

text = await transcribe_voice_text(bot, message.voice, transcribe=whisper)
```

Empty transcripts and transcriber failures raise `TelegramVoiceTranscriptionError`.

## More

- Glossary of the terms used across the API: [CONTEXT.md](CONTEXT.md)
- Design decisions: [docs/adr/](docs/adr/)

## Development

```sh
uv sync --all-groups
just check     # ruff format --check, ruff check, pytest
```

MIT © 2026 RoCry
