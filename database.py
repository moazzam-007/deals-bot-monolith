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
        """Initialize the SQLite database and create seen_ids table if it doesn't exist."""
        async with self._lock:
            try:
                # Ensure directory exists if path contains a directory
                os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
                
                async with aiosqlite.connect(self.db_path) as conn:
                    # Create table
                    await conn.execute(
                        '''CREATE TABLE IF NOT EXISTS seen_ids 
                           (product_id TEXT PRIMARY KEY, timestamp INTEGER)'''
                    )
                    await conn.commit()
                    # Cleanup old entries exceeding max_size to save disk space
                    await conn.execute(
                        f'''DELETE FROM seen_ids WHERE rowid NOT IN 
                            (SELECT rowid FROM seen_ids ORDER BY timestamp DESC LIMIT {self.max_size})'''
                    )
                    await conn.commit()
                logger.info(f"Database initialized at {self.db_path} (Fully Async)")
            except Exception as e:
                logger.error(f"Error initializing database: {e}")

    async def seen(self, product_id: str) -> bool:
        """Check if a product_id exists in the database. Read-only."""
        if not product_id:
            return False
            
        try:
            async with aiosqlite.connect(self.db_path) as conn:
                cur = await conn.execute("SELECT 1 FROM seen_ids WHERE product_id = ?", (product_id,))
                exists = await cur.fetchone() is not None
                return exists
        except Exception as e:
            logger.error(f"Database error on seen check: {e}")
            return False

    async def mark_posted(self, product_id: str):
        """Write the product ID after explicitly confirming a successful post."""
        if not product_id:
            return
            
        async with self._lock:
            try:
                async with aiosqlite.connect(self.db_path) as conn:
                    # Insert ignoring duplicates just in case
                    await conn.execute(
                        "INSERT OR IGNORE INTO seen_ids (product_id, timestamp) VALUES (?, ?)", 
                        (product_id, int(time.time()))
                    )
                    await conn.commit()
            except Exception as e:
                logger.error(f"Failed to save product ID to db: {e}")

# Global instance
db = DatabaseLog()
