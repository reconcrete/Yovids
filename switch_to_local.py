"""
One-time helper: log the bot out of Telegram's cloud API so a self-hosted
local Bot API server can take it over.

Telegram requires a bot to be logged out of the cloud (api.telegram.org)
before it can be used on a local server. This is reversible — to go back to
the cloud later, call logOut on the local server instead.

Usage:
    python switch_to_local.py
"""

import os
import sys
import time
import urllib.request

from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
LOCAL_API_BASE = os.environ.get("LOCAL_BOT_API_URL", "http://localhost:8081").rstrip("/")


def _get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read().decode()


def main() -> None:
    if not TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN is not set in .env")

    print("→ Logging the bot out of the Telegram cloud API…")
    try:
        body = _get(f"https://api.telegram.org/bot{TOKEN}/logOut")
        print(f"  cloud response: {body}")
    except Exception as exc:  # noqa: BLE001
        # 401 here usually means it's already logged out — that's fine.
        print(f"  (cloud logOut returned: {exc} — likely already logged out)")

    # The cloud needs a moment to release the bot.
    print("→ Waiting 5s for the cloud to release the bot…")
    time.sleep(5)

    print("→ Checking the local server…")
    try:
        body = _get(f"{LOCAL_API_BASE}/bot{TOKEN}/getMe")
        print(f"  local response: {body}")
        if '"ok":true' in body:
            print("\n✅ Local server is serving the bot. You can now run: python bot.py")
        else:
            print("\n⚠️ Local server reachable but getMe was not ok — check its logs.")
    except Exception as exc:  # noqa: BLE001
        print(f"  could not reach local server: {exc}")
        print(
            f"\n⚠️ Is the local server running? Start it with:\n"
            f"    docker compose up -d\n"
            f"  then re-run this script."
        )


if __name__ == "__main__":
    main()
