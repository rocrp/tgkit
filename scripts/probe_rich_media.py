#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["aiogram>=3.30", "pillow"]
# ///
"""Live probe: do Rich Message media uploads actually render?

TelegramTurnDelivery has zero automated coverage — the real API is its only
oracle. This script is that oracle, canned. It runs the two upload paths
against a real chat and prints the block structure the server says each
message ended up with (`rich_message.blocks` on the returned Message —
a `photo` block with file_ids means the media landed):

1. sendRichMessage with an uploaded photo referenced from the markdown
2. sendMessage, then editMessageText(rich_message=) uploading a NEW photo —
   the path the Bot API docs only prohibit for inline-mode messages,
   live-verified 2026-07-27

Usage:
    TELEGRAM_BOT_TOKEN=... uv run scripts/probe_rich_media.py --chat-id <your user id>

The probe posts two visible messages into the chat; use your own DM.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def make_png(directory: Path) -> Path:
    from PIL import Image, ImageDraw

    path = directory / "probe-grid.png"
    image = Image.new("RGB", (240, 240), (240, 240, 240))
    draw = ImageDraw.Draw(image)
    colors = [(196, 30, 58), (0, 158, 96), (0, 81, 186), (255, 213, 0), (255, 255, 255), (255, 88, 0)]
    for row in range(3):
        for col in range(3):
            origin = (12 + col * 74, 12 + row * 74)
            draw.rectangle(
                [*origin, origin[0] + 68, origin[1] + 68],
                fill=colors[(row * 3 + col) % 6],
                outline=(30, 30, 30),
                width=3,
            )
    image.save(path)
    return path


def block_summary(message: object) -> str:
    rich = getattr(message, "rich_message", None)
    blocks = getattr(rich, "blocks", None)
    if not blocks:
        return "NO rich_message.blocks — media did NOT land as rich content"
    parts = []
    for block in blocks:
        kind = getattr(block, "type", "?")
        detail = ""
        if kind == "photo" and (sizes := getattr(block, "photo", None)):
            largest = sizes[-1]
            detail = f"({largest.width}x{largest.height}, file_id={largest.file_id[:16]}…)"
        parts.append(f"{kind}{detail}")
    return " | ".join(parts)


async def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "Live rich-media probe").splitlines()[0])
    parser.add_argument("--chat-id", type=int, required=True, help="target chat (use your own DM)")
    parser.add_argument(
        "--token", default=os.environ.get("TELEGRAM_BOT_TOKEN"), help="bot token (or TELEGRAM_BOT_TOKEN)"
    )
    args = parser.parse_args()
    if not args.token:
        parser.error("--token or TELEGRAM_BOT_TOKEN is required")

    from aiogram import Bot
    from tgkit.rich import build_rich_payload, edit_rich, send_rich

    failures = 0
    with tempfile.TemporaryDirectory(prefix="rich_probe_") as tmp:
        png = make_png(Path(tmp))
        markdown = f"probe: BEFORE\n\n![probe grid]({png})\n\nprobe: AFTER"

        bot = Bot(token=args.token)
        try:
            print("=== 1. sendRichMessage with uploaded photo ===")
            async with build_rich_payload(markdown, max_bytes=20 * 1024 * 1024) as payload:
                assert payload.media, "payload built no media — probe is broken"
                sent = await send_rich(bot, chat_id=args.chat_id, markdown=payload)
            summary = block_summary(sent)
            print(summary)
            failures += "photo" not in summary

            print("=== 2. editMessageText(rich_message=) with NEW upload ===")
            placeholder = await bot.send_message(
                chat_id=args.chat_id, text="probe: about to become a rich message with an uploaded photo"
            )
            async with build_rich_payload(markdown, max_bytes=20 * 1024 * 1024) as payload:
                edited = await edit_rich(bot, chat_id=args.chat_id, message_id=placeholder.message_id, markdown=payload)
            summary = block_summary(edited)
            print(summary)
            failures += "photo" not in summary
        finally:
            await bot.session.close()

    verdict = "PASS" if not failures else f"FAIL ({failures})"
    print(f"=== {verdict} — confirm visually that both messages show text-image-text ===")
    sys.exit(1 if failures else 0)


asyncio.run(main())
