import asyncio
import logging
import httpx
from config import LEHLAH_COOKIE, WISHLINK_REFRESH_TOKEN, FIREBASE_API_KEY
import config

logger = logging.getLogger(__name__)

# =======================================================
# RATE LIMITER (Per Provider) 
# =======================================================
class RateLimiter:
    """Implement a 1s wait between conversions and 120s cooldown after batch limit."""
    def __init__(self, batch_limit=5, cooldown_seconds=120, wait_between=1.0):
        self.batch_limit = batch_limit
        self.cooldown_seconds = cooldown_seconds
        self.wait_between = wait_between
        
        self.calls_in_batch = 0
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            # 1. Base wait between every link to avoid triggering basic limits
            await asyncio.sleep(self.wait_between)
            
            self.calls_in_batch += 1
            if self.calls_in_batch > self.batch_limit:
                logger.info(f"RateLimiter: Hit batch limit ({self.batch_limit}). Cooling down for {self.cooldown_seconds}s...")
                await asyncio.sleep(self.cooldown_seconds)
                self.calls_in_batch = 1  # Reset batch, this is the first of the new batch


def with_retry(retries=3, base_wait=2.0):
    """Decorator for exponential backoff on API calls"""
    def decorator(func):
        async def wrapper(*args, **kwargs):
            for attempt in range(1, retries + 1):
                try:
                    result = await func(*args, **kwargs)
                    if result.get("ok"):
                        return result
                    
                    # If explicitly marked as rate limit or server error
                    error_msg = result.get("error", "").lower()
                    if "429" in error_msg or "too many" in error_msg or "timeout" in error_msg:
                        raise Exception(f"Transient error: {error_msg}")
                    
                    # Otherwise, it's a hard error (like invalid URL), don't retry
                    return result
                except Exception as e:
                    if attempt == retries:
                        logger.error(f"Function {func.__name__} failed after {retries} attempts: {e}")
                        return {"ok": False, "error": str(e)}
                    
                    wait_time = base_wait * (2 ** (attempt - 1))
                    logger.warning(f"Attempt {attempt} failed for {func.__name__}: {e}. Retrying in {wait_time}s...")
                    await asyncio.sleep(wait_time)
        return wrapper
    return decorator


# =======================================================
# LEHLAH PROVIDER
# =======================================================
class LehlahProvider:
    def __init__(self):
        self.limiter = RateLimiter(batch_limit=5, cooldown_seconds=60, wait_between=1.0)
        self.api_url = "https://creator.lehlah.club/api/campaign-url-builder"
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://creator.lehlah.club",
            "Referer": "https://creator.lehlah.club/link-genie",
            "Cookie": LEHLAH_COOKIE,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
            ),
        }

    @with_retry(retries=3, base_wait=2.0)
    async def _convert(self, url: str) -> dict:
        if not LEHLAH_COOKIE:
            return {"ok": False, "error": "LEHLAH_COOKIE is missing"}
            
        payload = {
            "title": "",
            "full_page_url": url,
            "page_no": 1,
            "DEVICE_TYPE": "web",
            "from": "LinkGenie",
        }
        
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(self.api_url, headers=self.headers, json=payload)
            response.raise_for_status()
            data = response.json()
            
            candidates = data.get("data", {}).get("data", {}).get("data", [])
            if candidates and isinstance(candidates, list):
                item = candidates[0]
                gen_url = item.get("generated_url")
                if gen_url:
                    return {"ok": True, "affiliate_link": gen_url}
            
            return {"ok": False, "error": "No affiliate link found in Lehlah response"}

    async def convert(self, url: str) -> dict:
        await self.limiter.acquire()
        return await self._convert(url)


# =======================================================
# WISHLINK PROVIDER
# =======================================================
class WishlinkProvider:
    def __init__(self):
        self.limiter = RateLimiter(batch_limit=5, cooldown_seconds=120, wait_between=1.0)
        self.api_url = "https://api.wishlink.com/api/c/convertSingleProductLink"
        self.id_token = None
        self.id_token_expires_at = 0

    async def ensure_token(self):
        """Refreshes Firebase ID token if expired or missing"""
        import time
        if self.id_token and time.time() < self.id_token_expires_at - 300: # 5 min buffer
            return True
            
        if not WISHLINK_REFRESH_TOKEN or not FIREBASE_API_KEY:
            logger.error("Missing Wishlink credentials (FIREBASE_API_KEY or WISHLINK_REFRESH_TOKEN)")
            return False

        auth_url = f"https://securetoken.googleapis.com/v1/token?key={FIREBASE_API_KEY}"
        payload = {"grant_type": "refresh_token", "refresh_token": WISHLINK_REFRESH_TOKEN}
        
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(auth_url, json=payload)
                resp.raise_for_status()
                data = resp.json()
                self.id_token = data.get("id_token")
                expires_in = int(data.get("expires_in", 3600))
                self.id_token_expires_at = time.time() + expires_in
                logger.info("Successfully refreshed Wishlink Auth Token")
                return True
        except Exception as e:
            logger.error(f"Failed to refresh Wishlink token: {e}")
            return False

    @with_retry(retries=3, base_wait=2.0)
    async def _convert(self, url: str) -> dict:
        auth_ok = await self.ensure_token()
        if not auth_ok or not self.id_token:
            return {"ok": False, "error": "Wishlink authentication failed"}

        headers = {
            "Authorization": f"Bearer {self.id_token}",
            "Content-Type": "application/json"
        }
        payload = {"url": url}

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(self.api_url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            
            wish_url = data.get("data", {}).get("wishlink") or data.get("wishlink")
            if wish_url:
                return {"ok": True, "affiliate_link": wish_url}
            return {"ok": False, "error": "Wishlink conversion failed. No link in response."}

    async def convert(self, url: str) -> dict:
        await self.limiter.acquire()
        return await self._convert(url)


# =======================================================
# SPLIT ROUTER
# =======================================================
lehlah = LehlahProvider()
wishlink = WishlinkProvider()

async def convert_url(url: str, platform: str) -> dict:
    """Smart router to balance load and prevent bans."""
    if platform == "amazon":
        logger.info(f"Routing {url} to Lehlah API")
        return await lehlah.convert(url)
    else:
        logger.info(f"Routing {url} to Wishlink API")
        return await wishlink.convert(url)
