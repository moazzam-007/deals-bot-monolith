import time
import logging
import asyncio
import aiosqlite
import os
from config import DB_PATH

logger = logging.getLogger(__name__)

class DatabaseLog:
    def __init__(self):
        self.db_path = DB_PATH
        self.max_size = 20000
        self._lock = asyncio.Lock()

    async def init_db(self):
        """Initialize the SQLite database and create tables if they don't exist."""
        async with self._lock:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)

                async with aiosqlite.connect(self.db_path) as conn:
                    # Duplicate detection table
                    await conn.execute(
                        '''CREATE TABLE IF NOT EXISTS seen_ids
                           (product_id TEXT PRIMARY KEY, timestamp INTEGER)'''
                    )
                    # Watermarks for polling backup loop (last processed msg ID per channel)
                    await conn.execute(
                        '''CREATE TABLE IF NOT EXISTS channel_watermarks
                           (channel_id INTEGER PRIMARY KEY, last_msg_id INTEGER)'''
                    )
                    await conn.commit()

                    # Keep DB small: drop entries beyond max_size
                    await conn.execute(
                        f'''DELETE FROM seen_ids WHERE rowid NOT IN
                            (SELECT rowid FROM seen_ids ORDER BY timestamp DESC LIMIT {self.max_size})'''
                    )
                    await conn.commit()

                logger.info(f"Database initialized at {self.db_path} (Fully Async)")
            except Exception as e:
                logger.error(f"Error initializing database: {e}")

    # ------------------------------------------------------------------
    # Duplicate detection
    # ------------------------------------------------------------------
    async def seen(self, product_id: str) -> bool:
        """Return True if product_id has already been posted."""
        if not product_id:
            return False
        try:
            async with aiosqlite.connect(self.db_path) as conn:
                cur = await conn.execute(
                    "SELECT 1 FROM seen_ids WHERE product_id = ?", (product_id,)
                )
                return await cur.fetchone() is not None
        except Exception as e:
            logger.error(f"DB seen() error: {e}")
            return False

    async def mark_posted(self, product_id: str):
        """Mark a product as posted. Called only after successful Telegram send."""
        if not product_id:
            return
        async with self._lock:
            try:
                async with aiosqlite.connect(self.db_path) as conn:
                    await conn.execute(
                        "INSERT OR IGNORE INTO seen_ids (product_id, timestamp) VALUES (?, ?)",
                        (product_id, int(time.time()))
                    )
                    await conn.commit()
            except Exception as e:
                logger.error(f"DB mark_posted() error: {e}")

    async def cleanup_old_seen(self, days: int = 7):
        """Delete seen_ids older than N days so the DB stays small over time."""
        cutoff = int(time.time()) - (days * 86400)
        async with self._lock:
            try:
                async with aiosqlite.connect(self.db_path) as conn:
                    cur = await conn.execute(
                        "DELETE FROM seen_ids WHERE timestamp < ?", (cutoff,)
                    )
                    await conn.commit()
                    logger.info(f"DB Cleanup: removed {cur.rowcount} entries older than {days} days.")
            except Exception as e:
                logger.error(f"DB cleanup_old_seen() error: {e}")

    # ------------------------------------------------------------------
    # Polling watermarks (last processed message ID per channel)
    # ------------------------------------------------------------------
    async def get_watermark(self, channel_id: int) -> int:
        """Return the last processed message ID for a channel (0 if first run)."""
        try:
            async with aiosqlite.connect(self.db_path) as conn:
                cur = await conn.execute(
                    "SELECT last_msg_id FROM channel_watermarks WHERE channel_id = ?",
                    (channel_id,)
                )
                row = await cur.fetchone()
                return row[0] if row else 0
        except Exception as e:
            logger.error(f"DB get_watermark({channel_id}) error: {e}")
            return 0

    async def set_watermark(self, channel_id: int, msg_id: int):
        """Save the latest processed message ID for a channel."""
        async with self._lock:
            try:
                async with aiosqlite.connect(self.db_path) as conn:
                    await conn.execute(
                        "INSERT OR REPLACE INTO channel_watermarks (channel_id, last_msg_id) VALUES (?, ?)",
                        (channel_id, msg_id)
                    )
                    await conn.commit()
            except Exception as e:
                logger.error(f"DB set_watermark({channel_id}) error: {e}")


# Global instance
db = DatabaseLog()
