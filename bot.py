import os
import sys
import asyncio
import logging
import html as html_lib
from aiohttp import web

# Pyrogram fix for Python 3.14+ (creates event loop before import)
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, ChatForwardsRestricted, MessageIdInvalid

from config import API_ID, API_HASH, STRING_SESSION, CHANNELS, OUTPUT_CHANNEL_ID, ADMIN_ID, PORT
from url_resolver import url_resolver
from database import db
from api_providers import convert_url

# Logging setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# Verify environment variables
if not STRING_SESSION:
    logger.error("STRING_SESSION environment variable is required!")
    sys.exit(1)

# Initialize Pyrogram App
app = Client(
    "deals_monolith",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=STRING_SESSION,
    in_memory=True # Use in_memory since we use session string
)

if not CHANNELS:
    logger.warning("No CHANNELS configured! Bot will not monitor any channel.")

# Set for O(1) manual channel check — bypasses Pyrogram's MIN_CHANNEL_ID bug
# filters.chat(CHANNELS) silently fails for newer channel IDs (> -1002147483647)
CHANNELS_SET = set(CHANNELS)

# ------------------------------------------------------------------
# 1. Server for Render Pinging (Prevents 15m spin-down if deployed as Web Service)
# ------------------------------------------------------------------
async def health_check(request):
    return web.Response(text=f"OK | Queue Size: {processing_queue.qsize()}")

async def start_web_server():
    server = web.Application()
    server.router.add_get("/health", health_check)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"Health web server started on port {PORT}")


# ------------------------------------------------------------------
# 2. Resilient Worker Components
# ------------------------------------------------------------------
processing_queue = asyncio.Queue(maxsize=500)
media_group_buffer = {}  # {group_id: [messages]}

# Simple throttle to prevent self-spam in Admin DM
error_alert_cooldowns = {}

async def send_error_alert(error_type: str, msg: str):
    """Send alert to Admin ID (or Saved Messages), throttled to 1 per 10 minutes per type."""
    import time
    now = time.time()
    last_sent = error_alert_cooldowns.get(error_type, 0)
    
    if now - last_sent < 600:
        return # Throttled
        
    error_alert_cooldowns[error_type] = now
    alert_target = ADMIN_ID if ADMIN_ID else "me"
    try:
        await app.send_message(alert_target, f"🚨 **{error_type}**\n\n{msg}", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Failed to send alert to {alert_target}: {e}")

async def safe_copy(message, chat_id, text_to_send, max_retries=3):
    """Wraps copy_message & send_message with FloodWait and Restricted handling."""
    for attempt in range(max_retries):
        try:
            # Reconstruct exact formatting
            # If msg is Text Only, send_message (copy_message ignores captions for text media)
            # If msg has Media, copy_message with new HTML caption
            if message.text and not message.media:
                return await app.send_message(
                    chat_id, 
                    text_to_send, 
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=False
                )
            else:
                return await app.copy_message(
                    chat_id, 
                    message.chat.id, 
                    message.id, 
                    caption=text_to_send,
                    parse_mode=ParseMode.HTML
                )
        except FloodWait as e:
            if e.value > 300: # If Telegram demands > 5 mins, skip to prevent stalling queue
                logger.error(f"FloodWait too long ({e.value}s). Skipping message.")
                await send_error_alert("FloodWait", f"Skipped msg due to {e.value}s FloodWait")
                return None
            logger.warning(f"FloodWait hit! Sleeping {e.value + 2}s")
            await asyncio.sleep(e.value + 2)
        except ChatForwardsRestricted:
            logger.info(f"Chat {message.chat.id} is Restricted! Falling back to manual download.")
            return await force_restricted_copy(message, chat_id, text_to_send)
        except MessageIdInvalid:
            logger.error("Message was deleted or invalid before we could copy it.")
            return None
        except Exception as e:
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt
                logger.warning(f"Copy attempt {attempt+1} failed: {e}. Retrying in {wait_time}s...")
                await asyncio.sleep(wait_time)
                continue
            logger.error(f"Failed to copy message after {max_retries} attempts: {e}")
            break
            
    await send_error_alert("Telegram Post Fail", f"Failed to post deal after {max_retries} retries.")
    return None

def get_file_size(message):
    for attr in ['video', 'document', 'audio', 'animation']:
        obj = getattr(message, attr, None)
        if obj and hasattr(obj, 'file_size') and obj.file_size:
            return obj.file_size
    if message.photo:
        return getattr(message.photo, 'file_size', 0) or 5 * 1024 * 1024  # Assume 5MB if pyrogram omits it
    return 0

async def force_restricted_copy(message, chat_id, text_to_send):
    """Fallback: Stream media to disk, upload, delete. Only allows files < 20MB."""
    import gc
    file_size = get_file_size(message)
    if file_size > 20 * 1024 * 1024:
        logger.warning(f"Skipping restricted media > 20MB. Sending text only.")
        return await app.send_message(chat_id, text_to_send, parse_mode=ParseMode.HTML)
    
    local_path = None
    try:
        local_path = await app.download_media(message, file_name="/tmp/")
        if message.photo:
            return await app.send_photo(chat_id, local_path, caption=text_to_send, parse_mode=ParseMode.HTML)
        elif message.video:
            return await app.send_video(chat_id, local_path, caption=text_to_send, parse_mode=ParseMode.HTML)
        else:
            return await app.send_document(chat_id, local_path, caption=text_to_send, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Restricted fallback failed: {e}")
    finally:
        if local_path and os.path.exists(local_path):
            os.remove(local_path)
            gc.collect() # Force free RAM

# ------------------------------------------------------------------
# 3. Message Processing Logic
# ------------------------------------------------------------------
async def process_single_message(message, album_messages=None):
    try:
        raw_text = message.text or message.caption or ""
        
        # 1. Extract URLs from Text and inline Markdown Entities
        urls = url_resolver.extract_urls(raw_text, entities=message.entities or message.caption_entities)
        
        if not urls:
            return # No URLs in this post
            
        # Get raw HTML representation of the message to safely replace URLs without breaking markup
        # message.html gives HTML-formatted text (Pyrogram 2.x property)
        html_payload = getattr(message, 'html', None) or raw_text
        
        url_updates = []
        is_new_deal = False
        valid_product_ids = []
        
        # Process each URL found in the message
        for original_url in urls:
            # Check if this URL is supported
            if not url_resolver.is_product_url(original_url):
                continue
                
            res = await url_resolver.process_url(original_url)
            product_id = res.get("product_id")
            
            # Duplicate check using aiosqlite database
            if await db.seen(product_id):
                logger.info(f"Duplicate detected: {product_id}. Skipping URL.")
                continue 
                
            is_new_deal = True
            valid_product_ids.append(product_id)
            
            # API Routing & Conversion
            api_res = await convert_url(res["resolved_url"], res["platform"])
            if api_res.get("ok"):
                affiliate_link = api_res["affiliate_link"]
                url_updates.append((original_url, affiliate_link))
            else:
                err = api_res.get("error", "Unknown error")
                logger.error(f"Conversion failed for {original_url}: {err}. Skipping post.")
                await send_error_alert(
                    "Conversion Fail — Post Skipped",
                    f"URL: {original_url}\nPlatform: {res['platform']}\nErr: {err}"
                )
                return  # Skip post — don't publish others' affiliate links
        
        if not is_new_deal:
            return # The message had only duplicated products, skip the post
            
        # 2. Rebuild the Caption
        # Instead of generic regex replacements that break HTML, replace specific matched strings
        # Also correctly handle &amp; encoded URLs in message.html
        for old_u, new_u in url_updates:
            encoded_old = html_lib.escape(old_u)
            html_payload = html_payload.replace(encoded_old, new_u)
            html_payload = html_payload.replace(old_u, new_u) # Fallback for unencoded
            
        # Enforce Telegram's 1024 char caption limit for media messages
        if message.media and len(html_payload) > 1024:
            truncated = html_payload[:1020]
            if truncated.count('<a') > truncated.count('</a'):
                truncated = truncated[:truncated.rfind('<a')]
            html_payload = truncated.strip() + "..."
            
        # 3. Post to Output Channel
        result = None
        if album_messages:
            # Forward the whole album correctly
            captions = [html_payload] + [""] * (len(album_messages) - 1)
            try:
                result = await app.copy_media_group(
                    chat_id=OUTPUT_CHANNEL_ID,
                    from_chat_id=album_messages[0].chat.id,
                    message_id=album_messages[0].id,
                    captions=captions
                )
            except Exception as e:
                logger.error(f"Album copy failed: {e}. Falling back to single.")
                result = await safe_copy(message, OUTPUT_CHANNEL_ID, html_payload)
        else:
            result = await safe_copy(message, OUTPUT_CHANNEL_ID, html_payload)
            
        # 4. Only Commit to DB if Send succeeded
        if result:
            for pid in valid_product_ids:
                await db.mark_posted(pid)
            logger.info(f"Successfully processed & posted message ID {message.id}")
        
    except Exception as e:
        logger.exception(f"Unhandled exception processing message {message.id}: {e}")

async def queue_worker():
    """Background task pulling from queue and processing sequentially."""
    logger.info("Queue Worker started.")
    while True:
        try:
            item = await processing_queue.get()
            
            # Handle list of grouped messages (Albums)
            if isinstance(item, list):
                logger.info(f"Processing Media Group with {len(item)} items.")
                # We process the first item that has a caption
                main_msg = next((m for m in item if m.caption), item[0])
                await process_single_message(main_msg, album_messages=item)
            else:
                await process_single_message(item)
                
            processing_queue.task_done()
        except asyncio.CancelledError:
            break
        except Exception as e:
             logger.error(f"Worker crashed on item: {e}")


# ------------------------------------------------------------------
# 4. Message Listeners (Media Buffering handling)
# ------------------------------------------------------------------
async def flush_media_group(group_id: str, delay: float = 2.0):
    await asyncio.sleep(delay)
    messages = media_group_buffer.pop(group_id, [])
    if messages:
        # Sort to ensure order matches original post
        messages.sort(key=lambda m: m.id)
        try:
            processing_queue.put_nowait(messages)
        except asyncio.QueueFull:
            pass

# filters.channel | filters.group — covers both channels AND supergroups (-1001... prefix)
# CHANNELS_SET does exact ID match as plain Python (100% reliable, bypasses MIN_CHANNEL_ID bug)
@app.on_message(filters.channel | filters.group)
async def handle_new_message(client, message):
    # Manual check bypasses Pyrogram's broken MIN_CHANNEL_ID resolver
    if not message.chat or message.chat.id not in CHANNELS_SET:
        return

    if not OUTPUT_CHANNEL_ID:
        logger.warning("OUTPUT_CHANNEL_ID is not configured!")
        return

    try:
        if message.media_group_id:
            gid = message.media_group_id
            media_group_buffer.setdefault(gid, []).append(message)
            # Schedule a flush
            if len(media_group_buffer[gid]) == 1:
                asyncio.create_task(flush_media_group(gid))
        else:
            processing_queue.put_nowait(message)
    except asyncio.QueueFull:
        logger.error("Queue is full! Dropping message to prevent OOM!")
        await send_error_alert("Queue Full", "Queue limit reached (500). Max capacity! Dropping messages.")


# ------------------------------------------------------------------
# 5. Polling Backup Loop (catches messages real-time handler may have missed)
# ------------------------------------------------------------------
async def polling_loop():
    """Every 10 minutes: fetch latest 10 msgs from each monitored channel.
    Uses SQLite watermarks to only process genuinely new messages.
    First run: initializes watermarks (no processing) to prevent deploy spam.
    Falls back gracefully on FloodWait."""
    POLL_INTERVAL = 600   # 10 minutes
    POLL_LIMIT    = 10    # max messages per channel per cycle

    logger.info("Polling backup loop started. Initializing watermarks in 2 minutes...")
    await asyncio.sleep(120)  # Let real-time handler settle first

    # --- First Run: Initialize watermarks only (no processing) ---
    # Prevents deploy spam (350 old messages posted at once on fresh deploy)
    logger.info("Polling: Initializing channel watermarks...")
    for ch_id in CHANNELS:
        try:
            if await db.get_watermark(ch_id) == 0:
                async for message in app.get_chat_history(ch_id, limit=1):
                    await db.set_watermark(ch_id, message.id)
                    logger.info(f"Watermark init: {ch_id} → msg {message.id}")
            await asyncio.sleep(1)
        except FloodWait as e:
            await asyncio.sleep(min(e.value + 5, 60))
        except Exception as e:
            logger.warning(f"Watermark init failed for {ch_id}: {e}")
    logger.info("Watermarks ready. Polling loop starting.")

    # --- Main Polling Loop ---
    while True:
        total_queued = 0
        logger.info("Polling cycle started...")
        for ch_id in CHANNELS:
            try:
                watermark = await db.get_watermark(ch_id)
                newest_id = watermark
                msgs_to_queue = []

                async for message in app.get_chat_history(ch_id, limit=POLL_LIMIT):
                    if message.id <= watermark:
                        break  # Older than our watermark — stop
                    msgs_to_queue.append(message)
                    if message.id > newest_id:
                        newest_id = message.id

                # Push in chronological order (oldest first) — skip albums
                # Albums need flush_media_group buffering; real-time handler handles them
                for message in reversed(msgs_to_queue):
                    try:
                        if message.media_group_id:
                            continue  # Skip albums in polling
                        processing_queue.put_nowait(message)
                        total_queued += 1
                    except asyncio.QueueFull:
                        logger.warning("Queue full during polling. Skipping remaining.")
                        break

                # Save watermark so next cycle won't re-queue same messages
                if newest_id > watermark:
                    await db.set_watermark(ch_id, newest_id)

                await asyncio.sleep(2)  # 2s gap between channels — prevents FloodWait

            except FloodWait as e:
                wait = min(e.value + 5, 60)  # Cap at 60s max per-channel wait
                logger.warning(f"Polling FloodWait {e.value}s for {ch_id}. Waiting {wait}s.")
                await asyncio.sleep(wait)
            except Exception as e:
                logger.warning(f"Polling error for channel {ch_id}: {e}")
                continue

        logger.info(f"Polling cycle done. Queued {total_queued} new messages. Next in 10 min.")
        await asyncio.sleep(POLL_INTERVAL)


async def cleanup_loop():
    """Daily: delete seen_ids older than 7 days to keep the SQLite DB small."""
    logger.info("Daily cleanup loop started.")
    await asyncio.sleep(86400)  # First run after 24 hours
    while True:
        await db.cleanup_old_seen(days=7)
        await asyncio.sleep(86400)


# ------------------------------------------------------------------
# Main Loop Setup
# ------------------------------------------------------------------
async def main():
    logger.info("Initializing system...")
    await db.init_db()
    await start_web_server()
    
    # Start all background tasks
    worker_task = asyncio.create_task(queue_worker())
    poll_task   = asyncio.create_task(polling_loop())
    clean_task  = asyncio.create_task(cleanup_loop())

    # Watchdog: guards queue_worker, polling_loop, AND cleanup_loop
    async def guard_worker():
        nonlocal worker_task, poll_task, clean_task
        while True:
            # -- Queue Worker --
            if worker_task.done():
                try:
                    exc = worker_task.exception()
                except asyncio.CancelledError:
                    exc = None
                if exc is not None:
                    logger.critical(f"Queue Worker Died. Restarting. Cause: {exc}")
                    await send_error_alert("Worker Died", f"Restarting. Cause: {exc}")
                else:
                    logger.warning("Queue Worker stopped normally. Restarting.")
                worker_task = asyncio.create_task(queue_worker())

            # -- Polling Loop --
            if poll_task.done():
                logger.warning("Polling loop died unexpectedly. Restarting.")
                await send_error_alert("Polling Died", "Polling backup loop restarted.")
                poll_task = asyncio.create_task(polling_loop())

            # -- Cleanup Loop --
            if clean_task.done():
                logger.warning("Cleanup loop died unexpectedly. Restarting.")
                clean_task = asyncio.create_task(cleanup_loop())

            await asyncio.sleep(30)

    asyncio.create_task(guard_worker())
    
    # Start Pyrogram
    logger.info("Starting Pyrogram bot...")
    await app.start()
    me = await app.get_me()
    logger.info(f"Bot authenticated successfully as: {me.username}")
    
    # Cache peers — NO limit (limit=0 in Pyrogram fetches only 1 page, not all)
    try:
        logger.info("Warming up peer cache from dialogs...")
        count = 0
        async for _ in app.get_dialogs():
            count += 1
        logger.info(f"Peer cache populated: {count} dialogs loaded.")
    except Exception as e:
        logger.warning(f"Failed to load peer cache: {e}")

    # Explicitly activate each monitored channel (1s gap to avoid FloodWait)
    # Failed ones are retried after their required wait time
    logger.info(f"Activating {len(CHANNELS)} monitored channels...")
    activated = 0
    failed_channels = []

    for ch_id in CHANNELS:
        try:
            await app.get_chat(ch_id)
            activated += 1
            await asyncio.sleep(1)
        except FloodWait as e:
            logger.warning(f"FloodWait {e.value}s for channel {ch_id}. Will retry later.")
            failed_channels.append((ch_id, e.value))
        except Exception as e:
            logger.warning(f"Could not activate channel {ch_id}: {e}")

    # Retry FloodWait channels after their required wait
    for ch_id, wait_sec in failed_channels:
        logger.info(f"Retrying channel {ch_id} after {wait_sec + 5}s...")
        await asyncio.sleep(wait_sec + 5)
        try:
            await app.get_chat(ch_id)
            activated += 1
            logger.info(f"Retry success for channel {ch_id}")
        except Exception as e:
            logger.error(f"Retry also failed for {ch_id}: {e}")

    logger.info(f"Activated {activated}/{len(CHANNELS)} channels. Bot ready!")

 
    # Run forever
    await asyncio.Event().wait()
    
    await app.stop()
    worker_task.cancel()
    await url_resolver.close()

if __name__ == "__main__":
    try:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("Shutting down gracefully.")
    except Exception as e:
        logger.exception("Fatal runtime error")
