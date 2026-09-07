"""Rich Message assembly, draft contract, and inline media embedding."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tgkit.constants import (
    MAX_RICH_BLOCKS,
    MAX_RICH_MEDIA,
    MAX_RICH_MESSAGE_LENGTH,
    MAX_RICH_NESTING_DEPTH,
    MAX_RICH_TABLE_COLUMNS,
    rich_len,
)
from tgkit.media import MAX_IMAGE_DIMENSION, classify_media
from tgkit.rich import (
    RICH_MEDIA_ID_PATTERN,
    THINKING_OPEN,
    RichContractError,
    RichPayload,
    build_rich_payload,
    edit_rich,
    is_rich_rejection,
    send_rich,
    send_rich_draft,
)

MEGABYTE = 1024 * 1024


class RecordingBot:
    """Captures the aiogram call a send would have made."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def send_rich_message(self, **kwargs: Any) -> object:
        self.calls.append(("send_rich_message", kwargs))
        return object()

    async def send_rich_message_draft(self, **kwargs: Any) -> bool:
        self.calls.append(("send_rich_message_draft", kwargs))
        return True

    async def edit_message_text(self, **kwargs: Any) -> object:
        self.calls.append(("edit_message_text", kwargs))
        return object()


def write_png(path: Path, size: tuple[int, int]) -> Path:
    from PIL import Image

    Image.new("RGB", size, color=(10, 20, 30)).save(path, format="PNG")
    return path


def write_animated_gif(path: Path, size: tuple[int, int], frames: int = 3) -> Path:
    from PIL import Image

    # Each frame needs distinct pixels: Pillow's GIF writer collapses runs of
    # identical frames, so an all-one-colour animation saves as a single frame.
    images = [
        Image.new("RGB", size, color=(index * 60, 10, 20)).convert("P", palette=Image.Palette.ADAPTIVE)
        for index in range(frames)
    ]
    images[0].save(path, format="GIF", save_all=True, append_images=images[1:], duration=40, loop=0)
    return path


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------


def test_rich_limits_match_the_documented_values():
    assert MAX_RICH_MESSAGE_LENGTH == 32768
    assert MAX_RICH_BLOCKS == 500
    assert MAX_RICH_NESTING_DEPTH == 16
    assert MAX_RICH_MEDIA == 50
    assert MAX_RICH_TABLE_COLUMNS == 20


def test_rich_len_counts_characters_not_utf16_units():
    from tgkit.constants import utf16_len

    text = "a😀b"
    assert rich_len(text) == 3
    assert utf16_len(text) == 4


def test_payload_over_the_character_budget_is_rejected():
    payload = RichPayload(markdown="x" * (MAX_RICH_MESSAGE_LENGTH + 1))
    assert not payload.fits()
    with pytest.raises(RichContractError, match="over the 32768 limit"):
        payload.validate()


def test_payload_at_the_character_budget_is_accepted():
    payload = RichPayload(markdown="x" * MAX_RICH_MESSAGE_LENGTH)
    assert payload.fits()
    payload.validate()


def test_empty_payload_is_rejected():
    with pytest.raises(RichContractError, match="cannot be empty"):
        RichPayload(markdown="").validate()


def test_payload_over_the_media_budget_is_rejected():
    payload = RichPayload(markdown="hi", media=[object()] * (MAX_RICH_MEDIA + 1))
    with pytest.raises(RichContractError, match="over the 50 limit"):
        payload.validate()


# ---------------------------------------------------------------------------
# Draft contract
# ---------------------------------------------------------------------------


async def test_draft_rejects_non_private_chats():
    with pytest.raises(RichContractError, match="private chats only"):
        await send_rich_draft(
            RecordingBot(),  # ty: ignore[invalid-argument-type]
            chat_id=-1001234567890,
            draft_id=7,
            markdown="hi",
        )


async def test_draft_rejects_zero_draft_id():
    with pytest.raises(RichContractError, match="draft_id must be non-zero"):
        await send_rich_draft(
            RecordingBot(),  # ty: ignore[invalid-argument-type]
            chat_id=42,
            draft_id=0,
            markdown="hi",
        )


async def test_draft_rejects_file_uploads():
    payload = RichPayload(markdown="hi", media=[object()])
    with pytest.raises(RichContractError, match="drafts cannot upload files"):
        await send_rich_draft(
            RecordingBot(),  # ty: ignore[invalid-argument-type]
            chat_id=42,
            draft_id=7,
            markdown=payload,
        )


async def test_draft_returns_true_not_a_message():
    bot = RecordingBot()
    result = await send_rich_draft(
        bot,  # ty: ignore[invalid-argument-type]
        chat_id=42,
        draft_id=7,
        markdown="# Heading",
    )
    assert result is True
    method, kwargs = bot.calls[0]
    assert method == "send_rich_message_draft"
    assert kwargs["draft_id"] == 7
    assert kwargs["rich_message"].markdown == "# Heading"


async def test_thinking_block_never_reaches_a_persisted_send():
    bot = RecordingBot()
    thinking = f"{THINKING_OPEN}working</tg-thinking>\n\nAnswer"
    with pytest.raises(RichContractError, match="valid only in drafts"):
        await send_rich(bot, chat_id=42, markdown=thinking)  # ty: ignore[invalid-argument-type]
    with pytest.raises(RichContractError, match="valid only in drafts"):
        await edit_rich(bot, chat_id=42, message_id=1, markdown=thinking)  # ty: ignore[invalid-argument-type]
    assert bot.calls == []


async def test_thinking_block_is_allowed_in_a_draft():
    bot = RecordingBot()
    await send_rich_draft(
        bot,  # ty: ignore[invalid-argument-type]
        chat_id=42,
        draft_id=7,
        markdown=f"{THINKING_OPEN}searching</tg-thinking>",
    )
    assert bot.calls[0][0] == "send_rich_message_draft"


async def test_send_rich_passes_thread_and_reply_parameters():
    from aiogram.types import ReplyParameters

    bot = RecordingBot()
    reply = ReplyParameters(message_id=11)
    await send_rich(
        bot,  # ty: ignore[invalid-argument-type]
        chat_id=-100123,
        markdown="body",
        thread_id=5,
        reply_params=reply,
    )
    _, kwargs = bot.calls[0]
    assert kwargs["message_thread_id"] == 5
    assert kwargs["reply_parameters"] is reply
    assert kwargs["rich_message"].markdown == "body"


async def test_edit_rich_sends_the_rich_message_field():
    bot = RecordingBot()
    await edit_rich(bot, chat_id=42, message_id=9, markdown="## Updated")  # ty: ignore[invalid-argument-type]
    method, kwargs = bot.calls[0]
    assert method == "edit_message_text"
    assert kwargs["message_id"] == 9
    assert kwargs["rich_message"].markdown == "## Updated"
    assert "text" not in kwargs


# ---------------------------------------------------------------------------
# Error taxonomy
# ---------------------------------------------------------------------------


class FakeRetryAfter(Exception):
    def __init__(self, seconds: float) -> None:
        self.retry_after = seconds
        super().__init__(f"Too Many Requests: retry after {seconds}")


@pytest.mark.parametrize(
    "message",
    [
        "Bad Request: can't parse rich_message",
        "Bad Request: too many blocks in the rich message",
        "Bad Request: markdown is malformed",
        "Bad Request: rich messages are not supported here",
    ],
)
def test_rich_rejections_are_detected(message: str):
    assert is_rich_rejection(Exception(message))


def test_rate_limits_are_never_rich_rejections():
    assert not is_rich_rejection(FakeRetryAfter(12))
    assert not is_rich_rejection(Exception("Too Many Requests: retry after 30"))


def test_unrelated_failures_are_not_rich_rejections():
    assert not is_rich_rejection(Exception("Bad Request: chat not found"))


# ---------------------------------------------------------------------------
# Inline media embedding
# ---------------------------------------------------------------------------


async def test_local_image_becomes_a_tg_photo_reference(tmp_path: Path):
    image = write_png(tmp_path / "shot.png", (32, 32))
    source = f"Before\n\n![a shot]({image})\n\nAfter"

    async with build_rich_payload(source, max_bytes=20 * MEGABYTE) as payload:
        assert "![a shot](tg://photo?id=m0)" in payload.markdown
        assert str(image) not in payload.markdown
        assert payload.markdown.startswith("Before")
        assert payload.markdown.endswith("After")
        assert len(payload.media) == 1
        assert payload.media[0].id == "m0"
        assert RICH_MEDIA_ID_PATTERN.match(payload.media[0].id)
        assert payload.rejected == []


async def test_media_reference_is_lifted_into_its_own_block(tmp_path: Path):
    """Telegram parses media only as a standalone block, never mid-paragraph."""
    image = write_png(tmp_path / "inline.png", (16, 16))
    source = f"text before ![x]({image}) text after"

    async with build_rich_payload(source, max_bytes=20 * MEGABYTE) as payload:
        lines = payload.markdown.splitlines()
        reference_lines = [line for line in lines if "tg://photo" in line]
        assert reference_lines == ["![x](tg://photo?id=m0)"]
        index = lines.index(reference_lines[0])
        assert lines[index - 1] == ""
        assert lines[index + 1] == ""


async def test_embed_title_becomes_the_media_caption(tmp_path: Path):
    image = write_png(tmp_path / "cap.png", (16, 16))

    async with build_rich_payload(f'![alt]({image} "A caption")', max_bytes=20 * MEGABYTE) as payload:
        assert '![alt](tg://photo?id=m0 "A caption")' in payload.markdown


async def test_media_positions_are_preserved_in_order(tmp_path: Path):
    first = write_png(tmp_path / "one.png", (16, 16))
    second = write_png(tmp_path / "two.png", (16, 16))
    source = f"# Report\n\n![one]({first})\n\nMiddle\n\n![two]({second})\n"

    async with build_rich_payload(source, max_bytes=20 * MEGABYTE) as payload:
        assert [item.id for item in payload.media] == ["m0", "m1"]
        one = payload.markdown.index("tg://photo?id=m0")
        middle = payload.markdown.index("Middle")
        two = payload.markdown.index("tg://photo?id=m1")
        assert one < middle < two


async def test_remote_and_relative_links_are_left_alone(tmp_path: Path):
    source = "![web](https://example.com/a.png) and ![rel](./local.png)"
    async with build_rich_payload(source, max_bytes=20 * MEGABYTE) as payload:
        assert payload.markdown == source
        assert payload.media == []


async def test_documents_are_not_inlined(tmp_path: Path):
    document = tmp_path / "notes.pdf"
    document.write_bytes(b"%PDF-1.4\n")
    source = f"![doc]({document})"
    async with build_rich_payload(source, max_bytes=20 * MEGABYTE) as payload:
        assert payload.media == []
        assert payload.markdown == source


async def test_oversized_image_is_dropped_and_reported(tmp_path: Path):
    image = write_png(tmp_path / "huge.png", (64, 64))
    source = f"Start\n\n![huge]({image})\n\nEnd"

    async with build_rich_payload(source, max_bytes=1) as payload:
        assert payload.media == []
        assert "tg://photo" not in payload.markdown
        assert "huge" in payload.markdown  # alt text survives
        assert [rejection.reason for rejection in payload.rejected] == ["too_large"]
        assert payload.rejected[0].path == image


async def test_video_embeds_use_the_video_scheme(tmp_path: Path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00" * 32)

    async with build_rich_payload(f"![clip]({video})", max_bytes=20 * MEGABYTE) as payload:
        assert "tg://video?id=m0" in payload.markdown
        assert len(payload.media) == 1


async def test_markdown_without_media_passes_through_untouched():
    source = "# Title\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n$x^2$"
    async with build_rich_payload(source, max_bytes=20 * MEGABYTE) as payload:
        assert payload.markdown == source
        assert payload.media == []


async def test_fallback_form_carries_alt_text_instead_of_tg_references(tmp_path: Path):
    """Below the Rich tier a tg:// link is dead; the descent needs this form."""
    image = write_png(tmp_path / "shot.png", (32, 32))
    source = f"Before\n\n![a shot]({image})\n\nAfter"

    async with build_rich_payload(source, max_bytes=20 * MEGABYTE) as payload:
        assert "tg://" not in payload.degraded_markdown
        assert str(image) not in payload.degraded_markdown
        assert "a shot" in payload.degraded_markdown
        assert payload.degraded_markdown.startswith("Before")
        assert payload.degraded_markdown.endswith("After")


def test_degraded_markdown_of_a_plain_payload_is_the_markdown_itself():
    payload = RichPayload(markdown="just text")
    assert payload.degraded_markdown == "just text"


async def test_embeds_beyond_the_inline_limit_spill_instead_of_rejecting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The 51st image is deliverable — it rides the Media Tail, not `rejected`."""
    from tgkit import rich

    monkeypatch.setattr(rich, "MAX_RICH_MEDIA", 1)
    first = write_png(tmp_path / "one.png", (16, 16))
    second = write_png(tmp_path / "two.png", (16, 16))
    source = f"![one]({first})\n\n![two]({second})"

    async with build_rich_payload(source, max_bytes=20 * MEGABYTE) as payload:
        assert len(payload.media) == 1
        assert payload.inline_paths == [first]
        assert payload.spill == [second]
        assert payload.rejected == []
        assert "tg://photo?id=m0" in payload.markdown
        assert "tg://photo?id=m1" not in payload.markdown
        assert "two" in payload.markdown  # alt text survives at position


async def test_inline_kinds_narrows_what_gets_lifted(tmp_path: Path):
    """Turn persists photos only; a video embed must pass through untouched."""
    image = write_png(tmp_path / "pic.png", (16, 16))
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00" * 32)
    source = f"![pic]({image})\n\n![clip]({video})"

    async with build_rich_payload(
        source, max_bytes=20 * MEGABYTE, inline_kinds=frozenset({"photo"})
    ) as payload:
        assert len(payload.media) == 1
        assert "tg://photo?id=m0" in payload.markdown
        assert f"![clip]({video})" in payload.markdown
        assert f"![clip]({video})" in payload.degraded_markdown


def test_undelivered_media_is_the_spill_on_rich_and_everything_below_it(tmp_path: Path):
    inline = tmp_path / "a.png"
    spilled = tmp_path / "b.png"
    payload = RichPayload(markdown="x", inline_paths=[inline], spill=[spilled])

    assert payload.undelivered_media("rich") == [spilled]
    assert payload.undelivered_media("markdownv2") == [inline, spilled]
    assert payload.undelivered_media("plain") == [inline, spilled]


# ---------------------------------------------------------------------------
# Media preparation
# ---------------------------------------------------------------------------


def test_media_classification_by_suffix():
    assert classify_media(Path("a.png")) == "photo"
    assert classify_media(Path("a.JPG")) == "photo"
    assert classify_media(Path("a.mp4")) == "video"
    assert classify_media(Path("a.ogg")) == "audio"
    assert classify_media(Path("a.pdf")) == "document"


async def test_oversized_image_is_resized_below_the_dimension_cap(tmp_path: Path):
    from PIL import Image

    from tgkit.media import prepared_images

    original = write_png(tmp_path / "big.png", (MAX_IMAGE_DIMENSION + 500, 1000))

    async with prepared_images([original], max_bytes=50 * MEGABYTE) as prepared:
        assert prepared.rejected == []
        upload = prepared.ready[0].upload
        assert prepared.ready[0].source == original
        assert upload != original
        with Image.open(upload) as resized:
            assert max(resized.size) <= MAX_IMAGE_DIMENSION
            assert resized.size[0] / resized.size[1] == pytest.approx(
                (MAX_IMAGE_DIMENSION + 500) / 1000, rel=0.01
            )
        temp_dir = upload.parent
    assert not temp_dir.exists()  # cleaned up on context exit


async def test_small_image_is_passed_through_without_a_copy(tmp_path: Path):
    from tgkit.media import prepared_images

    original = write_png(tmp_path / "small.png", (100, 80))
    async with prepared_images([original], max_bytes=50 * MEGABYTE) as prepared:
        assert prepared.ready[0].upload == original


async def test_animated_gif_keeps_every_frame_when_resized(tmp_path: Path):
    from PIL import Image

    from tgkit.media import prepared_images

    original = write_animated_gif(tmp_path / "anim.gif", (MAX_IMAGE_DIMENSION + 100, 200), frames=4)

    async with prepared_images([original], max_bytes=50 * MEGABYTE) as prepared:
        assert prepared.rejected == []
        with Image.open(prepared.ready[0].upload) as resized:
            assert getattr(resized, "is_animated", False)
            assert getattr(resized, "n_frames", 1) == 4
            assert max(resized.size) <= MAX_IMAGE_DIMENSION


async def test_unreadable_image_is_reported_not_raised(tmp_path: Path):
    from tgkit.media import prepared_images

    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not an image")

    async with prepared_images([broken], max_bytes=50 * MEGABYTE) as prepared:
        assert prepared.ready == []
        assert prepared.rejected[0].reason == "prepare_failed"
        assert "broken.png" in prepared.rejected[0].describe()


async def test_filter_by_size_drops_only_the_oversized(tmp_path: Path):
    from tgkit.media import filter_by_size

    small = tmp_path / "small.bin"
    small.write_bytes(b"x" * 10)
    large = tmp_path / "large.bin"
    large.write_bytes(b"x" * 5000)

    prepared = await filter_by_size([small, large], max_bytes=1000)
    assert prepared.paths == [small]
    assert prepared.rejected[0].path == large
    assert prepared.rejected[0].reason == "too_large"
