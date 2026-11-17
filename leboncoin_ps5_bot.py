"""
Leboncoin PS5 alert -> Telegram   (fast version, one IP, no paid proxy)

What is different from the previous version:
  1. Lighter requests: only the 5 newest ads are fetched (new ads always appear first).
  2. Telegram messages are sent by a background thread, so searching never waits for them.
  3. Adaptive speed: starts fast, slows down automatically when Leboncoin starts
     refusing requests, then speeds up again when things are calm.
  4. Short, growing waits after errors (instead of always 2-5 minutes), so a small
     network hiccup doesn't cost you minutes of alerts.
  5. Logs the average request time, and adds "posted X s ago" to each alert
     (when the data is available) so you can SEE your real delay.

Install:   pip install lbc requests
Run:       python leboncoin_ps5_bot_fast.py
"""

import json
import os
import queue
import random
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import lbc
import requests

# ---------------------------------------------------------------- CONFIG ----
# Paste your values between the quotes (or set them as environment variables).
BOT_TOKEN = os.getenv("BOT_TOKEN") or "9209643639:AAF4c8whMj85OkzTInlBbTtBV_tDpeJv740"
CHAT_ID = os.getenv("CHAT_ID") or "1207417721"

SEARCH_TEXT = "ps5"
PRICE_MIN = 200
PRICE_MAX = 350

FETCH_LIMIT = 5                 # newest ads fetched per check (smaller = faster response)
BASE_INTERVAL = (3, 5)          # seconds between checks when everything is calm
MAX_SLOWDOWN = 4.0              # the interval can grow up to 4x when errors happen
MAX_ERROR_WAIT = 300            # longest pause after repeated errors (seconds)

# Ads whose title contains one of these are "wanted" ads, not sales -> ignored
WANTED_WORDS = ("je recherche", "je cherche", "recherche", "cherche", "achète", "achete", "je veux")

# Game/accessory words. If one appears BEFORE "ps5" in the title the ad is
# ignored (it's the game/accessory itself). After "ps5" it's fine (bundle).
NOT_CONSOLE_WORDS = (
    "jeu", "jeux", "disque", "cd", "blu-ray", "fifa", "fc 24", "fc 25", "fc 26",
    "manette", "dualsense", "casque", "pulse", "volant", "support", "housse", "coque",
    "câble", "cable", "chargeur", "station de charge", "télécommande", "telecommande",
    "caméra", "camera", "vr", "carte mémoire", "ssd", "skin", "sticker", "pièce", "piece",
)

# Broken / for-parts consoles -> always ignored, wherever the word appears
BROKEN_WORDS = (
    "hs", "pour pièces", "pour pieces", "défectueuse", "defectueuse",
    "en panne", "panne", "cassée", "cassee",
)
SEEN_FILE = Path("seen_ads.json")
# ---------------------------------------------------------------------------

outbox: "queue.Queue[str]" = queue.Queue()


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# ------------------------------------------------------------- persistence --
def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except Exception:
            pass
    return set()


def save_seen(seen: set) -> None:
    SEEN_FILE.write_text(json.dumps(sorted(seen)))


# ---------------------------------------------------------------- telegram --
def sender_loop() -> None:
    """Background thread: sends queued messages, keeps one connection open."""
    session = requests.Session()
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    while True:
        text = outbox.get()
        for _ in range(3):
            try:
                r = session.post(
                    url,
                    data={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": False},
                    timeout=10,
                )
                if r.ok:
                    break
                log(f"Telegram error {r.status_code}: {r.text}")
            except Exception as e:
                log(f"Telegram send failed: {e}")
            time.sleep(1)


def send_telegram(text: str) -> None:
    outbox.put(text)


# ------------------------------------------------------------------ filter --
def has_word(text: str, words) -> bool:
    return any(re.search(rf"(?<!\w){re.escape(w)}(?!\w)", text) for w in words)


def is_wanted_ad(ad) -> bool:
    title = ad.subject.lower()

    if not any(k in title for k in ("ps5", "ps 5", "playstation 5", "playstation5")):
        return False
    if has_word(title, WANTED_WORDS):
        return False
    if has_word(title, BROKEN_WORDS):
        return False
    if "console" in title:
        return True
    ps5_pos = min(
        (title.find(k) for k in ("ps5", "ps 5", "playstation 5", "playstation5") if k in title)
    )
    for w in NOT_CONSOLE_WORDS:
        m = re.search(rf"(?<!\w){re.escape(w)}(?!\w)", title)
        if m and m.start() < ps5_pos:
            return False
    return True


def age_seconds(ad):
    """How long ago the ad was posted, if the library gives us the date."""
    try:
        d = getattr(ad, "first_publication_date", None) or getattr(ad, "index_date", None)
        if isinstance(d, str):
            d = datetime.fromisoformat(d.replace(" ", "T"))
        if not isinstance(d, datetime):
            return None
        now = datetime.now(d.tzinfo) if d.tzinfo else datetime.now()
        s = (now - d).total_seconds()
        return s if 0 <= s < 3600 else None
    except Exception:
        return None


def format_ad(ad) -> str:
    price = getattr(ad, "price", None)
    price_txt = f"{price:g} €" if isinstance(price, (int, float)) else "?"
    text = f"🎮 {ad.subject}\n💶 {price_txt}\n🔗 {ad.url}"
    age = age_seconds(ad)
    if age is not None:
        text += f"\n⏱ posted {age:.0f}s ago"
    return text


def fetch_ads(client):
    result = client.search(
        text=SEARCH_TEXT,
        price=[PRICE_MIN, PRICE_MAX],
        sort=lbc.Sort.NEWEST,
        limit=FETCH_LIMIT,
    )
    return [a for a in result.ads if is_wanted_ad(a)]


# -------------------------------------------------------------------- main --
def main() -> None:
    if "PASTE_" in BOT_TOKEN or "PASTE_" in CHAT_ID:
        log("ERROR: fill in BOT_TOKEN and CHAT_ID at the top of the file.")
        sys.exit(1)

    threading.Thread(target=sender_loop, daemon=True).start()

    client = lbc.Client()
    seen = load_seen()
    first_run = not seen

    slowdown = 1.0       # 1.0 = full speed
    errors = 0           # consecutive errors
    ok_streak = 0        # consecutive good checks
    checks = 0
    total_time = 0.0

    log("Bot started. Watching Leboncoin...")
    while True:
        try:
            t0 = time.time()
            ads = fetch_ads(client)
            took = time.time() - t0

            new_ads = [a for a in ads if str(a.id) not in seen]

            if first_run:
                log(f"First run: marking {len(new_ads)} existing ads as seen.")
                send_telegram(f"✅ Alert active: '{SEARCH_TEXT}' between {PRICE_MIN}-{PRICE_MAX} €.")
                first_run = False
            else:
                for ad in reversed(new_ads):      # oldest first
                    send_telegram(format_ad(ad))
                    log(f"NEW: {ad.subject}")

            if new_ads:
                seen.update(str(a.id) for a in new_ads)
                save_seen(seen)

            # --- bookkeeping / adaptive speed ---
            checks += 1
            total_time += took
            if errors:
                log("Back online.")
            errors = 0
            ok_streak += 1
            if ok_streak >= 30 and slowdown > 1.0:
                slowdown = max(1.0, slowdown * 0.85)
                ok_streak = 0
            if checks % 100 == 0:
                log(f"{checks} checks, avg request {total_time / checks:.2f}s, "
                    f"interval x{slowdown:.2f}")

            wait = random.uniform(*BASE_INTERVAL) * slowdown
            time.sleep(max(1.0, wait - took))     # keep a steady rhythm

        except Exception as e:
            errors += 1
            ok_streak = 0
            slowdown = min(MAX_SLOWDOWN, slowdown * 1.5)
            wait = min(MAX_ERROR_WAIT, 10 * 2 ** (errors - 1)) * random.uniform(0.8, 1.2)
            log(f"Error #{errors}: {e} -> waiting {wait:.0f}s, interval now x{slowdown:.2f}")
            time.sleep(wait)
            client = lbc.Client()                 # fresh session


if __name__ == "__main__":
    main()
