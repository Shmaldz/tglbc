"""
Leboncoin PS5 alert -> Telegram

Required environment variables:
    BOT_TOKEN  - token from @BotFather
    CHAT_ID    - your Telegram chat id
"""

import json
import os
import random
import re
import sys
import time
from pathlib import Path

import lbc
import requests

# ---------------------------------------------------------------- CONFIG ----
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

SEARCH_TEXT = "ps5"
PRICE_MIN = 200
PRICE_MAX = 350

CHECK_INTERVAL = (15, 30)    # random wait between checks, in seconds (lower = faster, but higher risk of being blocked)
ERROR_WAIT = (120, 300)      # wait after an error (e.g. 403 block) before trying again

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

# Optional: proxy if Leboncoin blocks your IP, e.g.
# PROXY = lbc.Proxy(host="127.0.0.1", port=8080, username="user", password="pass")
PROXY = None
# ---------------------------------------------------------------------------


def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except Exception:
            pass
    return set()


def save_seen(seen: set) -> None:
    SEEN_FILE.write_text(json.dumps(sorted(seen)))


def send_telegram(text: str) -> None:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={
                "chat_id": CHAT_ID,
                "text": text,
                "disable_web_page_preview": False,
            },
            timeout=15,
        )
        if not r.ok:
            print(f"Telegram error {r.status_code}: {r.text}", flush=True)
    except Exception as e:
        print(f"Telegram send failed: {e}", flush=True)


def fetch_ads(client):
    result = client.search(
        text=SEARCH_TEXT,
        price=[PRICE_MIN, PRICE_MAX],
        sort=lbc.Sort.NEWEST,
        limit=15,
    )
    return [a for a in result.ads if is_wanted_ad(a)]


def has_word(text: str, words) -> bool:
    """True if any of the words/phrases appears as a whole word in text."""
    return any(re.search(rf"(?<!\w){re.escape(w)}(?!\w)", text) for w in words)


def is_wanted_ad(ad) -> bool:
    title = ad.subject.lower()

    # 1. Must mention a PS5
    if not any(k in title for k in ("ps5", "ps 5", "playstation 5", "playstation5")):
        return False
    # 2. Skip "I'm looking for..." ads
    if has_word(title, WANTED_WORDS):
        return False
    # 3. Skip broken consoles / for parts
    if has_word(title, BROKEN_WORDS):
        return False
    # 4. Skip games / accessories, but keep console bundles.
    #    Rule: if a game/accessory word comes BEFORE "ps5" in the title
    #    ("Manette PS5", "Jeu FIFA PS5") it's the accessory itself -> skip.
    #    If it comes AFTER ("PS5 + FC 24", "PS5 avec manette", "PS5 SSD 1To")
    #    it's a console with extras -> keep.
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


def format_ad(ad) -> str:
    price = getattr(ad, "price", None)
    price_txt = f"{price:g} €" if isinstance(price, (int, float)) else "?"
    return f"🎮 {ad.subject}\n💶 {price_txt}\n🔗 {ad.url}"


def main() -> None:
    if not BOT_TOKEN or not CHAT_ID:
        print("ERROR: set the BOT_TOKEN and CHAT_ID environment variables.", flush=True)
        sys.exit(1)

    client = lbc.Client(proxy=PROXY) if PROXY else lbc.Client()
    seen = load_seen()
    first_run = not seen

    print("Bot started. Watching Leboncoin...", flush=True)
    while True:
        try:
            ads = fetch_ads(client)
            new_ads = [a for a in ads if str(a.id) not in seen]

            if first_run:
                # Don't spam you with existing listings on the first launch
                print(f"First run: marking {len(new_ads)} existing ads as seen.", flush=True)
                send_telegram(f"✅ Alert active: '{SEARCH_TEXT}' between {PRICE_MIN}-{PRICE_MAX} €.")
                first_run = False
            else:
                # Oldest first so messages arrive in chronological order
                for ad in reversed(new_ads):
                    send_telegram(format_ad(ad))
                    time.sleep(0.3)

            seen.update(str(a.id) for a in new_ads)
            save_seen(seen)
            time.sleep(random.uniform(*CHECK_INTERVAL))

        except Exception as e:
            wait = random.uniform(*ERROR_WAIT)
            print(f"Error: {e} -> waiting {wait:.0f}s before retrying", flush=True)
            time.sleep(wait)


if __name__ == "__main__":
    main()
