import re
import hashlib
import logging
from urllib.parse import urlparse, parse_qs
import httpx
import asyncio
from config import LEHLAH_COOKIE

logger = logging.getLogger(__name__)

# Domains that need HTTP redirect resolution
SHORTENED_DOMAINS = [
    "amzn.to", "a.co",
    "fkrt.it", "fkrt.cc",
    "myntr.it",
    "ajiio.in",
    "bittli.in", "bitli.in",
    "bit.ly", "tinyurl.com",
    "ekaro.in", "earnkaro.com",
    "cutt.ly", "cuttli.in", "bitly.cx", "web.lehlah.club",
    "linkredirect.in",
]

# E-commerce domains we care about
ECOMMERCE_DOMAINS = [
    "amazon.in", "amazon.com", "amazon.co.uk",
    "flipkart.com",
    "myntra.com",
    "ajio.com",
    "nykaa.com", "nykaafashion.com",
    "meesho.com",
    "snapdeal.com",
    "jiomart.com",
    "tatacliq.com",
    "shopsy.in",
]

# Combined list for URL detection
ALL_KNOWN_DOMAINS = SHORTENED_DOMAINS + ECOMMERCE_DOMAINS


def _domain_matches(netloc, domain):
    """Exact domain match: 'www.amazon.in' matches 'amazon.in', but 'amazon.in.evil.com' does not."""
    netloc = netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc == domain or netloc.endswith("." + domain)


def _any_domain_matches(netloc, domain_list):
    """Check if netloc matches any domain in the list."""
    return any(_domain_matches(netloc, d) for d in domain_list)


# Tracking params to strip universally
_UNIVERSAL_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "otracker", "otracker1", "otracker2",
    "affid", "affextparam1", "affextparam2",
    "tag",          # Amazon/others affiliate tag (we replace with our own)
    "ref",
    "source", "source_tag",
    "clickid", "click_id",
    "sub1", "sub2", "sub3",
}

# Params that are tracking-specific ONLY on Amazon (safe to strip there, may be legit elsewhere)
_AMAZON_ONLY_TRACKING_PARAMS = {
    "s",    # Amazon sort/search param
    "qid",  # Amazon query ID
    "ds",   # Amazon dataset param
    "ref_",
}

def clean_url(url: str) -> str:
    """Strip tracking params. Amazon-specific params only stripped on Amazon URLs."""
    try:
        from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
        parsed = urlparse(url)
        qs = parse_qs(parsed.query, keep_blank_values=False)
        is_amazon = "amazon" in parsed.netloc.lower()
        params_to_strip = _UNIVERSAL_TRACKING_PARAMS | (_AMAZON_ONLY_TRACKING_PARAMS if is_amazon else set())
        clean_qs = {k: v for k, v in qs.items() if k.lower() not in params_to_strip}
        new_query = urlencode(clean_qs, doseq=True)
        clean = urlunparse((
            parsed.scheme, parsed.netloc, parsed.path,
            parsed.params, new_query, ""
        ))
        return clean.rstrip("?&")
    except Exception:
        return url


class URLResolver:
    def __init__(self):
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            )
        }
        self._client = None
        self._lock = asyncio.Lock()

    async def get_client(self):
        """Returns a globally shared connection pool (httpx.AsyncClient)"""
        async with self._lock:
            if self._client is None or self._client.is_closed:
                self._client = httpx.AsyncClient(
                    headers=self.headers,
                    follow_redirects=True,
                    timeout=8.0
                )
            return self._client
        
    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # URL extraction from message text AND entities
    # ------------------------------------------------------------------
    def extract_urls(self, text, entities=None):
        """Extract all HTTP/HTTPS URLs from text and Telegram message entities (markdown links)."""
        urls = []
        
        # 1. First parse hidden Text Links from entities
        # This catches [Buy Here](https://amzn.to/123) which regex misses
        if entities:
            # entities are Pyrogram MessageEntity objects
            # To avoid circular imports, we just duck-type it
            for ent in entities:
                ent_type = getattr(ent, "type", str(getattr(ent, "type", "")))
                if "TEXT_LINK" in str(ent_type).upper() and ent.url:
                    urls.append(ent.url)
                elif "URL" in str(ent_type).upper() and text:
                    # Raw URL mapped in entity offset
                    try:
                        urls.append(text[ent.offset : ent.offset + ent.length])
                    except Exception:
                        pass
        
        # 2. Use regex to catch regular raw text links as backup
        if text:
            regex_urls = re.findall(r"https?://[^\s<>\"')\]\n]+", text, re.IGNORECASE)
            for u in regex_urls:
                u = u.rstrip(".,;:!?")
                if u not in urls:
                    urls.append(u)
                    
        # Remove duplicates while preserving order
        seen = set()
        return [x for x in urls if not (x in seen or seen.add(x))]

    def is_product_url(self, url):
        """Check if URL belongs to a known e-commerce or shortener domain."""
        try:
            netloc = urlparse(url).netloc.lower()
            return _any_domain_matches(netloc, ALL_KNOWN_DOMAINS)
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Lehlah Short Link Resolver (API-based — extracts original product URL)
    # ------------------------------------------------------------------
    async def _resolve_lehlah_short(self, url: str) -> str:
        """Call Lehlah's redirection API to get the original product URL from web.lehlah.club/s/SHORTCODE."""
        if not LEHLAH_COOKIE:
            logger.warning("LEHLAH_COOKIE missing — cannot resolve Lehlah short link. Returning as-is.")
            return url
        try:
            match = re.search(r'/s/([a-zA-Z0-9]+)', url)
            if not match:
                logger.warning(f"Lehlah short URL unexpected format: {url}")
                return url

            short_code = match.group(1)
            api_url = "https://web.lehlah.club/api/redirection/generate-redirect-url-in-app-redirection"
            payload = {
                "short_code": short_code,
                "referrer": "https://creator.lehlah.club/link-genie",
                "is_in_app": False,
                "is_telegram": False,
                "is_youtube": False,
                "is_instagram": False,
                "is_ios": False,
                "is_android": False,
            }
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://web.lehlah.club",
                "Referer": "https://web.lehlah.club/",
                "Cookie": LEHLAH_COOKIE,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/123.0.0.0 Safari/537.36",
            }
            client = await self.get_client()
            resp = await client.post(api_url, json=payload, headers=headers, timeout=15.0)
            data = resp.json()
            redirect_url = data.get("redirect_url")
            if redirect_url:
                logger.info(f"Lehlah short resolved: {url} → {redirect_url}")
                return redirect_url
            logger.warning(f"Lehlah short API returned no redirect_url for {url}")
            return url
        except Exception as e:
            logger.warning(f"Failed to resolve Lehlah short link {url}: {e}")
            return url

    # ------------------------------------------------------------------
    # Async Redirect Resolution
    # ------------------------------------------------------------------
    async def resolve_url(self, url):
        """Resolve shortened URLs by following redirects asyncly. Returns final URL."""
        try:
            netloc = urlparse(url).netloc.lower()

            # Special: Lehlah short links need API-based resolution (not HTTP redirect)
            if _domain_matches(netloc, "web.lehlah.club"):
                return await self._resolve_lehlah_short(url)

            if not _any_domain_matches(netloc, SHORTENED_DOMAINS):
                return url

            client = await self.get_client()
            response = await client.head(url)
            # Some sites block HEAD requests, fallback to GET
            if response.status_code >= 400 and response.status_code != 405:
                 response = await client.get(url)

            final = str(response.url)

            # Extract target from linkredirect.in/Earnkaro which blocks HTTPX with 403 Forbidden
            if "linkredirect.in" in final or "earnkaro.com" in final:
                from urllib.parse import parse_qs, unquote
                qs = parse_qs(urlparse(final).query)
                if "dl" in qs:
                    final = unquote(qs["dl"][0])

            # Second-level: if resolved to a Lehlah short link, extract original product URL
            if _domain_matches(urlparse(final).netloc.lower(), "web.lehlah.club"):
                final = await self._resolve_lehlah_short(final)

            logger.info(f"Resolved {url} -> {final}")
            return final
        except Exception as e:
            logger.warning(f"Failed to resolve async {url}: {e}")
            return url

    # ------------------------------------------------------------------
    # Product ID extraction per platform
    # ------------------------------------------------------------------
    def _extract_amazon_id(self, parsed):
        """Extract ASIN from Amazon URL."""
        match = re.search(r"(?:/dp/|/gp/product/|/product/)([A-Z0-9]{10})", parsed.path)
        if match:
            return f"amz_{match.group(1)}"
        params = parse_qs(parsed.query)
        for key in ("ASIN", "asin"):
            if key in params:
                return f"amz_{params[key][0]}"
        return None

    def _extract_flipkart_id(self, parsed):
        match = re.search(r"/p/([a-zA-Z0-9]+)", parsed.path)
        if match:
            return f"fk_{match.group(1)}"
        params = parse_qs(parsed.query)
        pid = params.get("pid", [None])[0]
        if pid:
            return f"fk_{pid}"
        return None

    def _extract_myntra_id(self, parsed):
        match = re.search(r"/(\d{5,})", parsed.path)
        if match:
            return f"myn_{match.group(1)}"
        return None

    def _extract_ajio_id(self, parsed):
        match = re.search(r"/p/([a-zA-Z0-9_]+)", parsed.path)
        if match:
            return f"ajio_{match.group(1)}"
        return None

    def _extract_meesho_id(self, parsed):
        match = re.search(r"/([a-zA-Z0-9-]+)/p/([a-zA-Z0-9]+)", parsed.path)
        if match:
            return f"msh_{match.group(2)}"
        return None

    def extract_product_id(self, url):
        """Extract a platform-specific product ID from URL."""
        try:
            parsed = urlparse(url)
            domain = parsed.netloc.lower()

            if _any_domain_matches(domain, ["amazon.in", "amazon.com", "amazon.co.uk"]):
                pid = self._extract_amazon_id(parsed)
                if pid: return pid
            elif _domain_matches(domain, "flipkart.com"):
                pid = self._extract_flipkart_id(parsed)
                if pid: return pid
            elif _domain_matches(domain, "myntra.com"):
                pid = self._extract_myntra_id(parsed)
                if pid: return pid
            elif _domain_matches(domain, "ajio.com"):
                pid = self._extract_ajio_id(parsed)
                if pid: return pid
            elif _domain_matches(domain, "meesho.com"):
                pid = self._extract_meesho_id(parsed)
                if pid: return pid

            # Fallback: Hash of the cleaned URL ignoring tracking parameters
            clean = url.split("?")[0].rstrip("/")
            url_hash = hashlib.md5(clean.encode()).hexdigest()[:12]
            return f"url_{url_hash}"

        except Exception as e:
            logger.warning(f"Product ID extraction failed for {url}: {e}")
            url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
            return f"url_{url_hash}"

    def detect_platform(self, url):
        """Detect which e-commerce platform the URL belongs to."""
        try:
            domain = urlparse(url).netloc.lower()
        except Exception:
            return "unknown"

        platform_map = {
            "amazon":   ["amazon.in", "amazon.com", "amazon.co.uk"],
            "meesho":   ["meesho.com"],
            "ajio":     ["ajio.com"],
            "shopsy":   ["shopsy.in"],
            "flipkart": ["flipkart.com", "dl.flipkart.com"],
            "myntra":   ["myntra.com"],
            "nykaa":    ["nykaa.com", "nykaafashion.com"],
            "snapdeal": ["snapdeal.com"],
            "jiomart":  ["jiomart.com"],
            "tatacliq": ["tatacliq.com"],
        }
        for platform, domains in platform_map.items():
            if _any_domain_matches(domain, domains):
                return platform
        return "unknown"

    # ------------------------------------------------------------------
    # Full processing pipeline (ASYNC)
    # ------------------------------------------------------------------
    async def process_url(self, url):
        """Complete async pipeline: resolve shortened URL, extract product ID, detect platform."""
        resolved = await self.resolve_url(url)
        cleaned  = clean_url(resolved)          # strip tracking params
        product_id = self.extract_product_id(cleaned)
        platform   = self.detect_platform(cleaned)

        return {
            "original_url": url,
            "resolved_url": cleaned,            # clean URL goes to affiliate API
            "product_id":   product_id,
            "platform":     platform,
        }

url_resolver = URLResolver()
