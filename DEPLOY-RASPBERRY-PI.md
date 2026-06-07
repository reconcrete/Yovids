# Deploying Yovids on a Raspberry Pi

This guide sets up the bot on a Raspberry Pi with the **local Bot API server**
(2 GB uploads) and **auto-start on boot**. It's written to be handed to a Claude
Code session running on the Pi over SSH — but it works fine by hand too.

## 0. Requirements

- **Raspberry Pi running a 64-bit OS** (Raspberry Pi OS Bookworm 64-bit, or
  Ubuntu for Pi). The local Bot API Docker image is `arm64` only, so a 32-bit OS
  won't work for the 2 GB feature.
  - Check: `uname -m` should print `aarch64`. If it prints `armv7l`, you're on a
    32-bit OS — either reflash with the 64-bit image, or run the bot against
    Telegram's cloud API instead (set `USE_LOCAL_BOT_API=0`, capped at 50 MB).
- Pi 4 / Pi 5 with a few GB free on the SD card (videos are downloaded to a temp
  dir, then deleted after upload).
- Your bot's **`TELEGRAM_BOT_TOKEN`**, **`TELEGRAM_API_ID`**, and
  **`TELEGRAM_API_HASH`** (the same ones already in use).

## 1. Install system dependencies

```bash
sudo apt update
sudo apt install -y git python3-venv ffmpeg curl

# Docker (official convenience script) + compose plugin
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"      # run docker without sudo
# Log out and back in (or run `newgrp docker`) for the group change to apply.
sudo systemctl enable --now docker   # start Docker now and on every boot
```

Verify: `docker run --rm hello-world` should succeed without `sudo`.

## 2. Clone the repo

```bash
cd ~
git clone https://github.com/reconcrete/Yovids.git
cd Yovids
```

## 3. Create the `.env`

```bash
cp .env.example .env
nano .env        # fill in the three values below
```

Set these (the URL and flag are already correct in the template):

```
TELEGRAM_BOT_TOKEN=<your bot token>
USE_LOCAL_BOT_API=1
LOCAL_BOT_API_URL=http://localhost:8081
TELEGRAM_API_ID=<your api_id>
TELEGRAM_API_HASH=<your api_hash>
```

> `.env` is gitignored — it never gets committed. Keep these values secret.

## 4. Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## 5. Start the local Bot API server

```bash
docker compose up -d
docker compose logs --tail 20      # should show it listening on :8081
```

## 6. Point the bot at the local server

A bot can only be served by one place at a time.

> ⚠️ **Stop any other instance first.** If the bot was running on another machine
> (e.g. your Mac), stop that `bot.py` and run `docker compose down` there before
> continuing — otherwise both will fight over `getUpdates` (Telegram returns a
> `409 Conflict`).

Then, on the Pi:

```bash
source .venv/bin/activate
python switch_to_local.py
```

You want it to end with `✅ Local server is serving the bot.`

## 7. Test it

```bash
python bot.py        # logs: "Using local Bot API server ... (2 GB limit)"
```

Open Telegram, send your bot a YouTube link, and confirm a download works.
Press `Ctrl+C` to stop once it works — the next step makes it run on its own.

## 8. Auto-start on boot (systemd)

This generates a systemd service filled in with your actual user and path, then
enables it:

```bash
sudo tee /etc/systemd/system/yovids.service >/dev/null <<EOF
[Unit]
Description=Yovids Telegram YouTube downloader bot
After=docker.service network-online.target
Requires=docker.service
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$PWD
ExecStart=$PWD/.venv/bin/python $PWD/bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now yovids.service
```

Check it:

```bash
systemctl status yovids.service        # should be "active (running)"
journalctl -u yovids.service -f        # live logs
```

The Docker server restarts on boot on its own (`restart: unless-stopped` +
Docker enabled), and `yovids.service` starts the bot after Docker. Reboot to
confirm: `sudo reboot`, then send the bot a link a minute after it's back.

## Updating later

```bash
cd ~/Yovids
git pull
source .venv/bin/activate
pip install -r requirements.txt        # in case deps changed
sudo systemctl restart yovids.service
```

Keep yt-dlp current (YouTube changes break old versions):

```bash
source .venv/bin/activate && pip install -U yt-dlp
sudo systemctl restart yovids.service
```

## Troubleshooting

- **`409 Conflict` / bot stops responding** — two instances are polling. Make
  sure only one `bot.py` runs anywhere. `systemctl stop yovids.service` before
  running it by hand.
- **`401 Unauthorized` from the local server** — the bot isn't logged into this
  server yet. Run `python switch_to_local.py` again.
- **Upload fails on large files** — confirm the bot logged
  `Using local Bot API server ... (2 GB limit)`. If it says `50 MB`, your `.env`
  doesn't have `USE_LOCAL_BOT_API=1`.
- **Video has audio but a frozen picture** — that's a non-H.264 codec; this bot
  already forces H.264, so update yt-dlp (`pip install -U yt-dlp`) if you ever
  see it.
- **`docker: permission denied`** — you haven't re-logged-in since `usermod -aG
  docker`. Run `newgrp docker` or start a new SSH session.
