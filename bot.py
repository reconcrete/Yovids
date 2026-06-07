"""
Telegram YouTube downloader bot.

Flow:
  1. User sends a message containing a YouTube URL.
  2. Bot probes the video and replies with its title + "Download Video" /
     "Download Audio" buttons.
  3. If "Download Video": bot shows the resolutions actually available for that
     video (H.264 only, so they play in Telegram) plus a "Convert to Audio"
     shortcut.
  4. Bot downloads with yt-dlp and uploads the file back to the chat.

Sending files through the standard Telegram Bot API is capped at 50 MB, so
downloads larger than that are rejected with a helpful message instead of a
silent failure.
"""

import asyncio
import logging
import os
import re
import secrets
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# Quiet down the very chatty network libraries.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

# When pointed at a self-hosted local Bot API server, uploads can be up to 2 GB
# instead of the cloud server's 50 MB. Driven by .env so the bot still runs
# against the cloud API if the local server isn't configured.
USE_LOCAL_API = os.environ.get("USE_LOCAL_BOT_API", "").strip().lower() in (
    "1",
    "true",
    "yes",
)
LOCAL_API_BASE = os.environ.get("LOCAL_BOT_API_URL", "http://localhost:8081").rstrip("/")

# Upload limit: 2 GB on a local server, 50 MB on Telegram's cloud API.
MAX_UPLOAD_BYTES = (2000 if USE_LOCAL_API else 50) * 1024 * 1024

# Generous timeouts: a multi-hundred-MB upload to the local server (which then
# relays to Telegram) can take a while.
UPLOAD_TIMEOUT = 3600  # seconds

# Matches typical YouTube URLs (youtube.com/watch, youtu.be, shorts, etc.).
YOUTUBE_RE = re.compile(
    r"(https?://)?(www\.)?"
    r"(youtube\.com/(watch\?v=|shorts/|live/|embed/)|youtu\.be/)"
    r"[\w\-]+\S*",
    re.IGNORECASE,
)

# Video quality presets -> yt-dlp format selector.
#
# We MUST prefer H.264 video (avc1) + AAC audio (m4a). YouTube also serves 1080p
# as VP9 and AV1, but Telegram's inline player can't decode those — playback
# shows a frozen first frame even though the audio plays. Forcing avc1+m4a (when
# available, which is the case up to 1080p for almost all videos) yields an MP4
# that plays everywhere, with no re-encoding. We only fall back to other codecs
# when no H.264 stream exists at the requested resolution.
def _video_format(max_height) -> str:
    h = f"[height<={max_height}]" if str(max_height).isdigit() else ""
    return (
        # 1. H.264 video + AAC audio  (ideal: clean, universally playable MP4)
        f"bestvideo{h}[vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
        # 2. H.264 video + any audio  (audio gets transcoded into the MP4)
        f"bestvideo{h}[vcodec^=avc1]+bestaudio/"
        # 3. a progressive H.264 MP4 (already muxed)
        f"best{h}[vcodec^=avc1][ext=mp4]/"
        # 4. last resort: best at this resolution (may be VP9/AV1)
        f"best{h}"
    )


def _probe(url: str) -> tuple[str, list[int]]:
    """Inspect a video without downloading it.

    Returns (title, heights) where `heights` are the resolutions actually
    available *in H.264* for this specific video, highest first. We restrict to
    H.264 because that's what plays inline in Telegram, so the quality menu only
    offers resolutions that will actually be playable.
    """
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    title = info.get("title", "video")
    heights = sorted(
        {
            f["height"]
            for f in info.get("formats", [])
            if f.get("height") and str(f.get("vcodec", "")).startswith("avc1")
        },
        reverse=True,
    )
    return title, heights


def store_entry(
    context: ContextTypes.DEFAULT_TYPE, url: str, title: str, heights: list[int]
) -> str:
    """Stash video info behind a short token so it fits in callback_data (<=64 bytes)."""
    token = secrets.token_urlsafe(6)
    context.bot_data.setdefault("entries", {})[token] = {
        "url": url,
        "title": title,
        "heights": heights,
    }
    return token


def get_entry(context: ContextTypes.DEFAULT_TYPE, token: str) -> dict | None:
    return context.bot_data.get("entries", {}).get(token)


def quality_keyboard(token: str, heights: list[int]) -> InlineKeyboardMarkup:
    """Build a quality menu from the video's actual H.264 resolutions (2 per row),
    with a 'Convert to Audio' shortcut at the bottom."""
    btns = [
        InlineKeyboardButton(f"{h}p", callback_data=f"q|{h}|{token}") for h in heights
    ]
    rows = [btns[i : i + 2] for i in range(0, len(btns), 2)]
    rows.append(
        [InlineKeyboardButton("🎵 Audio instead", callback_data=f"audio|{token}")]
    )
    return InlineKeyboardMarkup(rows)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    limit_label = "2 GB" if USE_LOCAL_API else "50 MB"
    await update.message.reply_text(
        "👋 Send me a YouTube link and I'll download the video or audio for you.\n\n"
        f"Upload limit is currently {limit_label} — pick a lower quality if a "
        "download is rejected as too large."
    )


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """React to any message that contains a YouTube URL."""
    match = YOUTUBE_RE.search(update.message.text)
    if not match:
        await update.message.reply_text(
            "That doesn't look like a YouTube link. Send me a YouTube URL."
        )
        return

    url = match.group(0)
    status = await update.message.reply_text("🔍 Checking video…")

    try:
        title, heights = await asyncio.to_thread(_probe, url)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Probe failed for %s: %s", url, exc)
        await status.edit_text(
            "❌ Couldn't read that video — it may be private, age-restricted, "
            "or region-locked.\n\nTry another link."
        )
        return

    token = store_entry(context, url, title, heights)

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🎬 Download Video", callback_data=f"video|{token}"),
                InlineKeyboardButton("🎵 Download Audio", callback_data=f"audio|{token}"),
            ]
        ]
    )
    await status.edit_text(
        f"🎬 {title}\n\nWhat would you like to do?",
        reply_markup=keyboard,
    )


async def on_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the first-level choice: Video vs Audio."""
    query = update.callback_query
    await query.answer()

    action, token = query.data.split("|", 1)
    entry = get_entry(context, token)
    if entry is None:
        await query.edit_message_text("⌛ That link expired. Please send it again.")
        return

    if action == "audio":
        await download_and_send(update, context, entry["url"], kind="audio", token=token)
        return

    # action == "video": offer the resolutions this video actually has in H.264.
    heights = entry["heights"]
    if not heights:
        await query.edit_message_text(
            "⚠️ This video has no Telegram-playable (H.264) video stream.\n\n"
            "You can still download the audio:",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🎵 Download Audio", callback_data=f"audio|{token}")]]
            ),
        )
        return

    await query.edit_message_text(
        "Choose a video quality:", reply_markup=quality_keyboard(token, heights)
    )


async def on_quality(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the quality selection for a video download."""
    query = update.callback_query
    await query.answer()

    _, quality, token = query.data.split("|", 2)
    entry = get_entry(context, token)
    if entry is None:
        await query.edit_message_text("⌛ That link expired. Please send it again.")
        return

    await download_and_send(
        update, context, entry["url"], kind="video", quality=quality, token=token
    )


def _ytdl_download(url: str, kind: str, quality: str, workdir: str) -> dict:
    """Blocking yt-dlp download. Returns info dict with the resulting file path."""
    import yt_dlp

    outtmpl = os.path.join(workdir, "%(title).80s.%(ext)s")

    if kind == "audio":
        opts = {
            "format": "bestaudio/best",
            "outtmpl": outtmpl,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,
        }
    else:
        opts = {
            "format": _video_format(quality),
            "outtmpl": outtmpl,
            "merge_output_format": "mp4",
            # Move the moov atom to the front so Telegram can stream/play
            # immediately instead of waiting for the whole file.
            "postprocessor_args": {"merger": ["-movflags", "+faststart"]},
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,
        }

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    # Find the produced file (extension may change after post-processing/merge).
    files = sorted(Path(workdir).iterdir(), key=lambda p: p.stat().st_size, reverse=True)
    if not files:
        raise RuntimeError("yt-dlp produced no output file")
    info["_filepath"] = str(files[0])
    return info


async def download_and_send(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    kind: str,
    quality: str = "best",
    token: str | None = None,
) -> None:
    query = update.callback_query
    chat_id = query.message.chat_id

    # The button can be tapped from a text menu (the quality screen) or from
    # under a delivered video (a media message). Media-message text can't be
    # edited, so in that case we post a fresh status message and edit that.
    if query.message.text is not None:
        status_msg = query.message
    else:
        status_msg = await query.message.reply_text("⏳ Working…")

    async def set_status(text: str) -> None:
        try:
            await status_msg.edit_text(text)
        except Exception:  # noqa: BLE001 - status updates are best-effort
            pass

    label = "audio" if kind == "audio" else f"video ({quality}p)"
    await set_status(f"⏳ Downloading {label}… this may take a moment.")

    action = ChatAction.UPLOAD_VOICE if kind == "audio" else ChatAction.UPLOAD_VIDEO
    await context.bot.send_chat_action(chat_id=chat_id, action=action)

    logger.info("Request: kind=%s quality=%s url=%s", kind, quality, url)

    workdir = tempfile.mkdtemp(prefix="ytbot_")
    try:
        info = await asyncio.to_thread(_ytdl_download, url, kind, quality, workdir)
        filepath = info["_filepath"]
        size = os.path.getsize(filepath)
        logger.info(
            "Downloaded %r -> %.1f MB", info.get("title", "?"), size / (1024 * 1024)
        )

        if size > MAX_UPLOAD_BYTES:
            mb = size / (1024 * 1024)
            limit_label = "2 GB" if USE_LOCAL_API else "50 MB"
            logger.warning("File too large: %.1f MB > %s", mb, limit_label)
            if kind == "video":
                await set_status(
                    f"⚠️ The file is {mb:.1f} MB, which exceeds the current "
                    f"{limit_label} upload limit.\n\nTry a lower quality, or "
                    "download the audio instead."
                )
            else:
                await set_status(
                    f"⚠️ The audio is {mb:.1f} MB, which exceeds the current "
                    f"{limit_label} upload limit. Try a shorter video."
                )
            return

        title = info.get("title", "download")
        await set_status(f"📤 Uploading {label}…")

        with open(filepath, "rb") as fh:
            if kind == "audio":
                await context.bot.send_audio(
                    chat_id=chat_id,
                    audio=fh,
                    title=title,
                    performer=info.get("uploader"),
                    duration=int(info.get("duration") or 0) or None,
                    caption=title,
                    write_timeout=UPLOAD_TIMEOUT,
                    read_timeout=UPLOAD_TIMEOUT,
                )
            else:
                # Attach a "Convert to Audio" button directly under the video so
                # the user can grab the MP3 of this same video after watching.
                convert_markup = (
                    InlineKeyboardMarkup(
                        [[InlineKeyboardButton(
                            "🎵 Convert to Audio", callback_data=f"audio|{token}"
                        )]]
                    )
                    if token
                    else None
                )
                await context.bot.send_video(
                    chat_id=chat_id,
                    video=fh,
                    caption=title,
                    duration=int(info.get("duration") or 0) or None,
                    width=info.get("width"),
                    height=info.get("height"),
                    supports_streaming=True,
                    reply_markup=convert_markup,
                    write_timeout=UPLOAD_TIMEOUT,
                    read_timeout=UPLOAD_TIMEOUT,
                )

        logger.info("Uploaded %r (%.1f MB) to chat %s", title, size / (1024 * 1024), chat_id)
        await set_status(f"✅ Done: {title}")

    except Exception as exc:  # noqa: BLE001 - surface any failure to the user
        logger.exception("Download/upload failed")
        await set_status(
            f"❌ Something went wrong:\n{exc}\n\nTry again or pick a different option."
        )
    finally:
        # Clean up the temp directory and its contents.
        for p in Path(workdir).glob("*"):
            try:
                p.unlink()
            except OSError:
                pass
        try:
            os.rmdir(workdir)
        except OSError:
            pass


def main() -> None:
    if not TOKEN:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is not set. Put it in a .env file or export it."
        )

    builder = Application.builder().token(TOKEN)
    if USE_LOCAL_API:
        builder = (
            builder.base_url(f"{LOCAL_API_BASE}/bot")
            .base_file_url(f"{LOCAL_API_BASE}/file/bot")
            .read_timeout(UPLOAD_TIMEOUT)
            .write_timeout(UPLOAD_TIMEOUT)
            .connect_timeout(60)
            .pool_timeout(60)
        )
        logger.info("Using local Bot API server at %s (2 GB limit)", LOCAL_API_BASE)
    else:
        logger.info("Using Telegram cloud Bot API (50 MB limit)")

    app = builder.build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CallbackQueryHandler(on_choice, pattern=r"^(video|audio)\|"))
    app.add_handler(CallbackQueryHandler(on_quality, pattern=r"^q\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))

    logger.info("Bot started. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
