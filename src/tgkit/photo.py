"""Telegram photo helpers: download a photo and detect its MIME type.

Describing the image (vision models and the like) is the application's job;
tgkit hands back bytes plus a MIME type and stops there.
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.types import PhotoSize

_DEFAULT_MIME = "image/jpeg"

_IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
)
_AVIF_BRANDS = {b"avif", b"avis"}
_HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}


class TelegramPhotoDownloadError(RuntimeError):
    pass


def sniff_image_mime(data: bytes) -> str | None:
    """Detect an image MIME type from its magic bytes; None when unrecognised."""
    head = data[:32]
    for signature, mime in _IMAGE_SIGNATURES:
        if head.startswith(signature):
            return mime
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in _AVIF_BRANDS:
            return "image/avif"
        if brand in _HEIC_BRANDS:
            return "image/heic"
    return None


async def download_photo_bytes(bot: Bot, photo: PhotoSize) -> tuple[bytes, str]:
    """Download a Telegram photo to raw bytes plus its detected MIME type.

    Telegram re-encodes photos as JPEG, so an unrecognised signature falls
    back to ``image/jpeg`` rather than failing the download.

    Raises:
        TelegramPhotoDownloadError: on any download failure
    """
    file_id = getattr(photo, "file_id", None)
    if not file_id:
        raise TelegramPhotoDownloadError("photo missing file_id")

    try:
        file = await bot.get_file(file_id)
    except Exception as exc:
        raise TelegramPhotoDownloadError(f"bot.get_file failed: {exc}") from exc

    file_path = getattr(file, "file_path", None)
    if not file_path:
        raise TelegramPhotoDownloadError("photo file missing file_path")

    try:
        bio = await bot.download_file(file_path)
    except Exception as exc:
        raise TelegramPhotoDownloadError(f"bot.download_file failed: {exc}") from exc

    if bio is None:
        raise TelegramPhotoDownloadError("bot.download_file returned None")

    try:
        data = bytes(bio) if isinstance(bio, (bytes, bytearray)) else bio.read()
    except Exception as exc:
        raise TelegramPhotoDownloadError(f"downloaded photo unreadable: {exc}") from exc

    if not data:
        raise TelegramPhotoDownloadError("downloaded photo empty")

    return data, sniff_image_mime(data) or _DEFAULT_MIME


__all__ = [
    "TelegramPhotoDownloadError",
    "download_photo_bytes",
    "sniff_image_mime",
]
