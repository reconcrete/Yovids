# Telegram YouTube Downloader Bot

A Telegram bot that takes a YouTube URL and lets you download it as **video**
(at a chosen quality) or **audio** (MP3), then sends the file back into the chat.

## How it works

1. Send the bot a YouTube link.
2. It checks the video and replies with its title + **🎬 Download Video** /
   **🎵 Download Audio**.
3. Pick **Video** → it shows the resolutions *actually available for that video*
   (read live from YouTube), plus a **🎵 Convert to Audio** shortcut.
4. The bot downloads with `yt-dlp` and uploads the file to the chat.

Only **H.264** resolutions are offered, because that's the codec Telegram can
play inline. YouTube's 1440p/4K are VP9/AV1-only — they wouldn't play in
Telegram (frozen frame), so they're intentionally not listed.

## Requirements

- Python 3.10+
- [`ffmpeg`](https://ffmpeg.org/) on your `PATH` (used to merge video/audio and
  extract MP3). On macOS: `brew install ffmpeg`.

## Setup

```bash
cd telegram-youtube-videos
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

The bot token lives in `.env`:

```
TELEGRAM_BOT_TOKEN=your-token-here
```

## Run

```bash
source .venv/bin/activate
python bot.py
```

Then open Telegram, find your bot, send `/start`, and paste a YouTube link.

## Run it 24/7 on a Raspberry Pi

See **[DEPLOY-RASPBERRY-PI.md](DEPLOY-RASPBERRY-PI.md)** for a full walkthrough:
system deps, the local Bot API server in Docker, and a systemd service that
auto-starts the bot on boot.

## Big files: 2 GB uploads via a local Bot API server

Telegram's **cloud** Bot API caps bot uploads at **50 MB** — far too small for a
full-length 1080p video. Running your own [local Bot API
server](https://github.com/tdlib/telegram-bot-api) raises that to **2 GB**. This
repo ships a `docker-compose.yml` that runs it for you.

### 1. Get an api_id + api_hash

Go to https://my.telegram.org → log in with your phone → **API development
tools** → create an app → copy the **api_id** and **api_hash**.

Put them in `.env`:

```
USE_LOCAL_BOT_API=1
TELEGRAM_API_ID=1234567
TELEGRAM_API_HASH=your_api_hash_here
```

### 2. Start the local server

```bash
docker compose up -d
docker compose logs -f      # optional: confirm it started cleanly
```

### 3. Switch the bot off the cloud (one time)

A bot must be logged out of Telegram's cloud before a local server can use it
(reversible later):

```bash
source .venv/bin/activate
python switch_to_local.py
```

### 4. Run the bot

```bash
python bot.py     # logs: "Using local Bot API server ... (2 GB limit)"
```

To go back to the cloud (50 MB), set `USE_LOCAL_BOT_API=0` in `.env` and call
`logOut` on the local server (`curl http://localhost:8081/bot<TOKEN>/logOut`).

## Notes & limitations

- The bot checks file size after downloading and, if it exceeds the active limit
  (2 GB local / 50 MB cloud), asks you to pick a lower quality or grab the audio.
- Downloads run in a background thread so the bot stays responsive.
- Temporary files are cleaned up after each download.
- Playlists are ignored — only the single linked video is downloaded.
