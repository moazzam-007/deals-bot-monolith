# Affiliate Deals Monolith Bot

A highly resilient Pyrogram-based Telegram User-Bot designed to forward deals from source channels, convert their affiliate links (Lehlah/Wishlink), and seamlessly post them to your destination channel while bypassing restrictions.

## Architecture Highlights
- **100% Async / Non-Blocking**: Uses `httpx` to handle all URL resolutions and API calls simultaneously.
- **Smart Split-Routing**: Amazon links are auto-routed to the `Lehlah` API, while Flipkart/Myntra hit `Wishlink`.
- **FloodWait Handled**: Native API integration auto-backs-off when Telegram throws a `429 Too Many Requests`.
- **Restricted Channel Bypasser**: If forwarding is blocked, the bot dynamically streams the media to disk and re-uploads it instantly (preserving RAM).
- **Persistent Anti-Duplicate DB**: Utilizes local `SQLite` to track seen deals. No more duplicate posting when Render restarts.
- **Background Wake Service**: Has an embedded `aiohttp` `/health` route on port 10000. Hook `n8n` to this endpoint every 10 mins, and your Render free-tier instance will *never* go to sleep.

## Setup
1. Copy `.env.example` -> `.env` and fill the variables.
2. `pip install -r requirements.txt`
3. `python bot.py`
