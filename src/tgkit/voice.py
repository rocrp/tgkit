"""Telegram voice helpers: download a voice note and hand it to the caller's STT.

tgkit never transcribes anything itself. :func:`transcribe_voice_text` takes a
Transcriber — any ``async (bytes) -> str`` — so the speech-to-text provider,
its language settings and its caching stay with the application.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol


class _Voice(Protocol):
    @property
    def file_id(self) -> str | None: ...


class _Bot(Protocol):
    async def get_file(self, file_id: str) -> object: ...

    async def download_file(self, file_path: str) -> object: ...


class TelegramVoiceDownloadError(RuntimeError):
    pass


class TelegramVoiceTranscriptionError(RuntimeError):
    pass


async def download_voice_bytes(bot: _Bot, voice: _Voice) -> bytes:
    file_id = voice.file_id
    if not file_id:
        raise TelegramVoiceDownloadError("voice missing file_id")

    try:
        file = await bot.get_file(file_id)
    except Exception as exc:
        raise TelegramVoiceDownloadError(f"bot.get_file failed: {exc}") from exc

    file_path = getattr(file, "file_path", None)
    if not isinstance(file_path, str) or not file_path:
        raise TelegramVoiceDownloadError("voice file missing file_path")

    try:
        bio = await bot.download_file(file_path)
    except Exception as exc:
        raise TelegramVoiceDownloadError(f"bot.download_file failed: {exc}") from exc

    if bio is None:
        raise TelegramVoiceDownloadError("bot.download_file returned None")

    try:
        if isinstance(bio, (bytes, bytearray)):
            data = bytes(bio)
        else:
            read = getattr(bio, "read", None)
            if not callable(read):
                raise TypeError(f"expected bytes or a readable object, got {type(bio).__name__}")
            data = read()
    except Exception as exc:
        raise TelegramVoiceDownloadError(f"downloaded voice unreadable: {exc}") from exc

    if not isinstance(data, bytes):
        raise TelegramVoiceDownloadError(f"downloaded voice returned {type(data).__name__}, expected bytes")
    if not data:
        raise TelegramVoiceDownloadError("downloaded voice empty")

    return data


async def transcribe_voice_text(
    bot: _Bot,
    voice: _Voice,
    *,
    transcribe: Callable[[bytes], Awaitable[str]],
) -> str:
    """Download a voice note and transcribe it with the caller-supplied ``transcribe``.

    The caller supplies the speech-to-text: ``transcribe`` receives the raw
    audio bytes and returns the text. Any failure inside it is re-raised as
    :class:`TelegramVoiceTranscriptionError`; so is an empty transcript.
    """
    data = await download_voice_bytes(bot, voice)
    try:
        result = await transcribe(data)
    except Exception as exc:
        raise TelegramVoiceTranscriptionError(str(exc)) from exc

    text = (result or "").strip()
    if not text:
        raise TelegramVoiceTranscriptionError("transcription empty")
    return text


__all__ = [
    "TelegramVoiceDownloadError",
    "TelegramVoiceTranscriptionError",
    "download_voice_bytes",
    "transcribe_voice_text",
]
