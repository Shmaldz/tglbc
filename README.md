# Leboncoin PS5 alert bot

Sends a Telegram message when a new PS5 listing (200-350 EUR) appears on Leboncoin.

## Environment variables (set them in your hosting dashboard, never in the code)

| Name | Value |
|------|-------|
| `BOT_TOKEN` | token from @BotFather |
| `CHAT_ID` | your Telegram chat id |

## Run

Deploy as a **worker / background service** (not a web service).
Start command: `python leboncoin_ps5_bot.py`
