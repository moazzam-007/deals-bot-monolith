# Telegram Affiliate Automation Bot — Progress Summary

## Project Overview

Ek fully automated Telegram affiliate bot banaya ja raha hai jo:

1. **Monitored channels** se incoming messages watch karta hai
2. Messages mein **product links** detect karta hai (Amazon, Flipkart, Myntra, Meesho, etc.)
3. Links ko **Lehlah API** se affiliate links mein convert karta hai
4. Affiliate links ke saath processed message ko **output channel** mein post karta hai
5. **Duplicate detection** karta hai — ek product ek baar hi post hota hai
6. **Render.com** pe 24/7 deployed hai

---

## Architecture — Monolith Bot (Current)

```
Telegram → [Pyrogram Userbot] → URL Extraction → Lehlah API → Output Channel
                  ↓
           Processing Queue (asyncio)
                  ↓
           SQLite DB (dedup + watermarks)
```

### Files:
| File | Purpose |
|------|---------|
| `bot.py` | Main bot logic — handlers, polling, queue worker |
| `config.py` | Environment variables loading |
| `database.py` | SQLite async operations |
| `api_providers.py` | Lehlah API integration |
| `url_resolver.py` | URL detection, platform identification, product ID extraction |

---

## Key Features Implemented

### ✅ Real-time Message Detection
- `@app.on_message(filters.channel | filters.group)` — catches messages as they arrive
- `CHANNELS_SET` = plain Python set for O(1) channel ID matching (bypasses Pyrogram bug)

### ✅ Polling Backup Loop (every 10 minutes)
- Fetches last 10 messages from each monitored channel
- SQLite watermarks track last processed message ID per channel
- Prevents re-processing old messages on restart
- First-run watermark initialization — no deploy spam

### ✅ Duplicate Detection (SQLite)
- `seen_ids` table stores processed product IDs with timestamp
- 7-day auto-cleanup — DB stays small over time
- Atomic: product marked only after successful Telegram post

### ✅ Rate Limit Handling
- `FloodWait` handled everywhere (handler, polling, activation)
- 2s gap between channels during polling
- Lehlah API rate limiter — batching + semaphore

### ✅ Media Support
- Text-only messages ✅
- Photo + caption messages ✅
- Media groups (albums) ✅ — buffered with `flush_media_group()`
- Restricted channels ✅ — fallback to download + re-upload

### ✅ Resilient Background Tasks
- `queue_worker` — processes messages sequentially
- `guard_worker` — watchdog for all 3 background tasks (worker, polling, cleanup)
- All tasks protected with try/except — no silent crashes

### ✅ Keep-Alive (Render Free Tier)
- n8n workflow pings `/health` endpoint every 13 minutes
- Prevents 15-minute spin-down on Render

### ✅ Admin Alerts
- Error alerts sent to admin Telegram ID
- Throttled: 1 alert per error type per 10 minutes

---

## Problems Faced & How Solved

### 🐛 Problem 1 — Channels Not Detected
**Root Cause:** Pyrogram 2.0.106 has a `MIN_CHANNEL_ID` bug.  
Newer Telegram channels (2024+) have IDs like `-1002809778017` which exceed Pyrogram's  
hardcoded boundary `-1002147483647`. `filters.chat(CHANNELS)` silently fails for these.

**Why only test channels worked:**  
Own channels → already in session string peer cache → match hote the.  
Monitored channels (joined, not owned) → peer cache mein nahi → silent fail.

**Fix Applied:**
```python
# Old (broken for new channel IDs):
@app.on_message(filters.chat(CHANNELS))

# New (100% reliable):
CHANNELS_SET = set(CHANNELS)
@app.on_message(filters.channel | filters.group)
async def handle_new_message(client, message):
    if not message.chat or message.chat.id not in CHANNELS_SET:
        return
```

---

### 🐛 Problem 2 — Peer Cache Empty on Startup
**Root Cause:** `get_dialogs(limit=0)` was fetching 0 dialogs (not all).

**Fix Applied:**
```python
async for _ in app.get_dialogs():   # No limit — fetches all
    count += 1
```

---

### 🐛 Problem 3 — Channel Activation FloodWait Unhandled
**Root Cause:** Rapid `get_chat()` calls for 35 channels triggered Telegram FloodWait.  
Failed channels were silently skipped.

**Fix Applied:** FloodWait caught → wait → retry logic for all failed channels.

---

### 🐛 Problem 4 — First Deploy Spam
**Root Cause:** Polling watermarks were 0 on fresh deploy → 350 old messages would queue.

**Fix Applied:** Polling loop first initializes watermarks (current latest msg ID) without processing.

---

### 🐛 Problem 5 — Albums Not Handled in Polling
**Root Cause:** `get_chat_history()` returns individual messages — album captions may be  
on photo 2, not photo 1. Polling would pick the wrong message.

**Fix Applied:** Album messages (`media_group_id`) skipped in polling — real-time handler  
handles them correctly with `flush_media_group()` buffering.

---

### 🐛 Problem 6 — Supergroups Missed
**Root Cause:** `filters.channel` only matches `ChatType.CHANNEL`. Some monitored  
channels are supergroups (megagroup=True, `-1001...` prefix) — they were missed.

**Fix Applied:** `filters.channel | filters.group` — covers both.

---

## What's Working Right Now

| Feature | Status |
|---------|--------|
| Real-time message detection | ✅ |
| Polling backup (10 min) | ✅ |
| Duplicate detection | ✅ |
| Affiliate link conversion (Lehlah) | ✅ |
| Amazon, Flipkart, Myntra, Meesho routing | ✅ |
| Album/media group support | ✅ |
| FloodWait handling everywhere | ✅ |
| Guard watchdog (all 3 tasks) | ✅ |
| 7-day DB cleanup | ✅ |
| Render keep-alive (n8n) | ✅ |
| Admin error alerts | ✅ |

---

## Pending / To Monitor

| Item | Status |
|------|--------|
| Deploy latest code | ⏳ Do manually on Render |
| Verify all 35 channels detecting | ⏳ Check logs post-deploy |
| `pyrogrammod` (full MIN_CHANNEL_ID fix) | ⏳ Add if polling still fails for some channels |

---

## Environment Variables Required (Render)

| Variable | Description |
|----------|-------------|
| `API_ID` | Telegram API ID |
| `API_HASH` | Telegram API Hash |
| `STRING_SESSION` | Pyrogram session string |
| `CHANNELS` | Comma-separated monitored channel IDs |
| `OUTPUT_CHANNEL_ID` | Where processed deals are posted |
| `LEHLAH_COOKIE` | Lehlah API auth cookie |
| `ADMIN_ID` | Telegram user ID for error alerts |
| `PORT` | 10000 (Render default) |
| `DB_PATH` | Path for SQLite file |

---

*Last Updated: April 2026*
