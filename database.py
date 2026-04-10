import sqlite3
import time
import logging
import threading
from config import DB_PATH

logger = logging.getLogger(__name__)

class DatabaseLog:
    def __init__(self):
        self.db_path = DB_PATH
        self.max_size = 20000 
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        """Initialize the SQLite database and create seen_ids table if it doesn't exist."""
        with self._lock:
            try:
                conn = sqlite3.connect(self.db_path)
                # Create table
                conn.execute(
                    '''CREATE TABLE IF NOT EXISTS seen_ids 
                       (product_id TEXT PRIMARY KEY, timestamp INTEGER)'''
                )
                conn.commit()
                # Cleanup old entries exceeding max_size to save disk space
                conn.execute(
                    f'''DELETE FROM seen_ids WHERE rowid NOT IN 
                        (SELECT rowid FROM seen_ids ORDER BY timestamp DESC LIMIT {self.max_size})'''
                )
                conn.commit()
                conn.close()
                logger.info(f"Database initialized at {self.db_path}")
            except Exception as e:
                logger.error(f"Error initializing database: {e}")

    def is_duplicate(self, product_id: str) -> bool:
        """Check if a product_id exists in the database. If not, add it seamlessly."""
        if not product_id:
            return False

        with self._lock:
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                cursor.execute("SELECT 1 FROM seen_ids WHERE product_id = ?", (product_id,))
                exists = cursor.fetchone() is not None
                
                if exists:
                    conn.close()
                    return True
                
                # If new, add it immediately to prevent race conditions
                cursor.execute(
                    "INSERT INTO seen_ids (product_id, timestamp) VALUES (?, ?)", 
                    (product_id, int(time.time()))
                )
                conn.commit()
                conn.close()
                return False
            except Exception as e:
                logger.error(f"Database error on duplicate check: {e}")
                # On error, fallback to allowing it through rather than failing
                return False

# Global instance
db = DatabaseLog()
