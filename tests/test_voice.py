from __future__ import annotations

from io import BytesIO

import pytest

from tgkit.voice import (
    TelegramVoiceDownloadError,
    TelegramVoiceTranscriptionError,
    download_voice_bytes,
    transcribe_voice_text,
)


class _Voice:
    def __init__(self, file_id: str | None) -> None:
        self.file_id = file_id


class _File:
    def __init__(self, file_path: str | None) -> None:
        self.file_path = file_path


class _Bot:
    def __init__(self, *, file_path: str | None, payload: object) -> None:
        self._file_path = file_path
        self._payload = payload

    async def get_file(self, file_id: str) -> _File:  # noqa: ARG002
        return _File(self._file_path)

    async def download_file(self, file_path: str) -> object:  # noqa: ARG002
        return self._payload


async def test_download_voice_bytes_bytes() -> None:
    bot = _Bot(file_path="/x", payload=b"abc")
    assert await download_voice_bytes(bot, _Voice("id")) == b"abc"


async def test_download_voice_bytes_bio() -> None:
    bot = _Bot(file_path="/x", payload=BytesIO(b"abc"))
    assert await download_voice_bytes(bot, _Voice("id")) == b"abc"


async def test_download_voice_bytes_missing_file_id_raises() -> None:
    bot = _Bot(file_path="/x", payload=b"abc")
    with pytest.raises(TelegramVoiceDownloadError, match="missing file_id"):
        await download_voice_bytes(bot, _Voice(None))


async def test_download_voice_bytes_missing_file_path_raises() -> None:
    bot = _Bot(file_path=None, payload=b"abc")
    with pytest.raises(TelegramVoiceDownloadError, match="missing file_path"):
        await download_voice_bytes(bot, _Voice("id"))


async def test_download_voice_bytes_empty_raises() -> None:
    bot = _Bot(file_path="/x", payload=b"")
    with pytest.raises(TelegramVoiceDownloadError, match="empty"):
        await download_voice_bytes(bot, _Voice("id"))


async def test_transcribe_voice_text_hands_bytes_to_the_transcriber() -> None:
    seen: list[bytes] = []

    async def transcribe(audio: bytes) -> str:
        seen.append(audio)
        return " hi "

    bot = _Bot(file_path="/x", payload=b"abc")
    text = await transcribe_voice_text(bot, _Voice("id"), transcribe=transcribe)

    assert text == "hi"
    assert seen == [b"abc"]


async def test_transcribe_voice_text_empty_raises() -> None:
    async def transcribe(_audio: bytes) -> str:
        return " "

    bot = _Bot(file_path="/x", payload=b"abc")
    with pytest.raises(TelegramVoiceTranscriptionError, match="empty"):
        await transcribe_voice_text(bot, _Voice("id"), transcribe=transcribe)


async def test_transcribe_voice_text_wraps_transcriber_errors() -> None:
    async def transcribe(_audio: bytes) -> str:
        raise RuntimeError("boom")

    bot = _Bot(file_path="/x", payload=b"abc")
    with pytest.raises(TelegramVoiceTranscriptionError, match="boom") as info:
        await transcribe_voice_text(bot, _Voice("id"), transcribe=transcribe)
    assert isinstance(info.value.__cause__, RuntimeError)


async def test_transcribe_voice_text_download_errors_are_not_transcription_errors() -> None:
    async def transcribe(_audio: bytes) -> str:
        raise AssertionError("must not be reached")

    bot = _Bot(file_path=None, payload=b"abc")
    with pytest.raises(TelegramVoiceDownloadError):
        await transcribe_voice_text(bot, _Voice("id"), transcribe=transcribe)
