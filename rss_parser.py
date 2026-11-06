import asyncio
import logging
import re
import time
import hashlib
from datetime import datetime, timezone, timedelta

import aiohttp
import feedparser

import database as db

logger = logging.getLogger(__name__)

MAX_AGE_HOURS = 24

RSS_FEEDS = [
    {"url": "https://cointelegraph.com/rss",                    "name": "Cointelegraph"},
    {"url": "https://decrypt.co/feed",                          "name": "Decrypt"},
    {"url": "https://www.theblock.co/rss.xml",                  "name": "TheBlock"},
    {"url": "https://bitcoinist.com/feed/",                     "name": "Bitcoinist"},
    {"url": "https://cryptonews.com/news/feed/",                "name": "CryptoNews"},
    {"url": "https://www.coindesk.com/arc/outboundfeeds/rss/",  "name": "CoinDesk"},
    {"url": "https://cryptopotato.com/feed/",                   "name": "Cryptopotato"},
]

_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


def news_id(link: str) -> int:
    """Stable 48-bit ID: independent of Python's per-process hash seed."""
    return int.from_bytes(hashlib.sha256(link.encode()).digest()[:6], "big")


def _clean(text: str) -> str:
    """Strip HTML tags and normalise whitespace."""
    text = _TAG_RE.sub(" ", text or "")
    return _SPACE_RE.sub(" ", text).strip()


def _entry_image(entry) -> str | None:
    """Extract image or video URL from RSS entry."""
    VIDEO_EXT = (".mp4", ".mov", ".webm", ".m4v")
    IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp")

    for media in entry.get("media_content", []):
        url = media.get("url", "")
        mime = media.get("type", "")
        if not url or url.endswith((".gif", ".svg")):
            continue
        if mime.startswith("video/") or url.lower().endswith(VIDEO_EXT):
            return url  
        if mime.startswith("image/") or url.lower().endswith(IMAGE_EXT) or "://" in url:
            return url  

    # media:thumbnail
    for thumb in entry.get("media_thumbnail", []):
        url = thumb.get("url", "")
        if url:
            return url

    for enc in entry.get("enclosures", []):
        mime = enc.get("type", "")
        href = enc.get("href", "") or enc.get("url", "")
        if mime.startswith("video/") or href.lower().endswith(VIDEO_EXT):
            return href
        if mime.startswith("image/") or href.lower().endswith(IMAGE_EXT):
            return href

    desc = entry.get("summary", "") or entry.get("description", "") or ""
    m = re.search(r'<img[^>]+src=["\']([^"\']{20,})["\']', desc)
    if m:
        url = m.group(1)
        if not url.endswith((".gif", ".svg")):
            return url
    return None


def _entry_text(entry) -> str:
    """Extract readable text from an RSS entry (title + summary)."""
    title = _clean(entry.get("title", ""))
    summary = _clean(entry.get("summary", "") or entry.get("description", ""))
    if summary.startswith(title):
        summary = summary[len(title):].strip(" .-–")
    parts = [p for p in (title, summary) if p]
    return ". ".join(parts)[:600]


def _entry_date(entry) -> datetime | None:
    """Return timezone-aware datetime from RSS entry."""
    for field in ("published_parsed", "updated_parsed"):
        val = entry.get(field)
        if val:
            try:
                return datetime(*val[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


async def _fetch_feed(session: aiohttp.ClientSession, feed: dict) -> list[dict]:
    """Fetch and parse one RSS feed, return list of news dicts."""
    name = feed["name"]
    url = feed["url"]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    results = []

    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=20),
            allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 CryptoNewsBot/1.0"},
        ) as resp:
            if resp.status != 200:
                logger.warning("RSS %s: HTTP %d", name, resp.status)
                return []
            raw = await resp.text(errors="replace")

        parsed = feedparser.parse(raw)
        count = 0
        for entry in parsed.entries:
            pub = _entry_date(entry)
            if pub and pub < cutoff:
                continue  # too old

            text = _entry_text(entry)
            if len(text) < 60:
                continue

            link = entry.get("link", "")
            msg_id = news_id(link)
            image_url = _entry_image(entry)

            await db.save_parsed(f"rss_{name}", msg_id, text, image_url, source_url=link)
            results.append({
                "channel": f"rss_{name}",
                "msg_id": msg_id,
                "text": text,
                "date": pub,
                "image_url": image_url,
                "url": link,
            })
            count += 1

        logger.info("RSS %s: %d fresh items", name, count)

    except asyncio.TimeoutError:
        logger.warning("RSS %s: timeout", name)
    except Exception as ex:
        logger.warning("RSS %s: %s", name, ex)

    return results


async def parse_rss() -> list[dict]:
    """Fetch all RSS feeds concurrently. Returns list of fresh news dicts."""
    connector = aiohttp.TCPConnector()
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [_fetch_feed(session, feed) for feed in RSS_FEEDS]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    all_news = []
    for feed, r in zip(RSS_FEEDS, results):
        if isinstance(r, Exception):
            logger.error("RSS %s failed: %s", feed["name"], r)
        elif isinstance(r, list):
            all_news.extend(r)

    logger.info("RSS total: %d fresh items from %d feeds", len(all_news), len(RSS_FEEDS))
    return all_news
