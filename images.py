import asyncio
import logging
import os
import tempfile

import aiohttp

logger = logging.getLogger(__name__)

VIDEO_EXT = (".mp4", ".mov", ".webm", ".m4v")
MEDIA_DIR = "/tmp/cryptonews_images"

# Only collect images from these RSS sources (clean charts, no watermark processing)
_TRUSTED_IMAGE_SOURCES = frozenset({"rss_CoinDesk", "rss_TheBlock"})


def is_video(url_or_path: str) -> bool:
    return url_or_path.lower().endswith(VIDEO_EXT)


async def convert_to_9x16(video_bytes: bytes) -> bytes:
    """Convert video to 9:16 portrait format using ffmpeg (pad with black bars)."""
    os.makedirs(MEDIA_DIR, exist_ok=True)
    tmp_in = tempfile.NamedTemporaryFile(suffix=".mp4", dir=MEDIA_DIR, delete=False)
    tmp_out_path = tmp_in.name + "_9x16.mp4"
    try:
        tmp_in.write(video_bytes)
        tmp_in.close()
        cmd = [
            "ffmpeg", "-y", "-i", tmp_in.name,
            "-vf",
            "scale=1080:1920:force_original_aspect_ratio=decrease,"
            "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,"
            "setsar=1",
            "-c:v", "libx264", "-crf", "26", "-preset", "fast",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            tmp_out_path,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=90)
        except asyncio.TimeoutError:
            proc.kill()
            logger.warning("ffmpeg timeout")
            return video_bytes

        if proc.returncode == 0 and os.path.exists(tmp_out_path):
            with open(tmp_out_path, "rb") as f:
                result = f.read()
            if len(result) > 10000:
                logger.info("Video converted to 9:16: %dKB → %dKB",
                            len(video_bytes) // 1024, len(result) // 1024)
                return result
        logger.warning("ffmpeg failed (code %s)", proc.returncode)
    except Exception as ex:
        logger.warning("convert_to_9x16 error: %s", ex)
    finally:
        for p in (tmp_in.name, tmp_out_path):
            try:
                os.unlink(p)
            except OSError:
                pass
    return video_bytes


async def _fetch_url(url: str) -> bytes | None:
    """Download media from URL, respecting size limits."""
    max_size = 50 * 1024 * 1024  # 50 MB
    try:
        timeout = aiohttp.ClientTimeout(total=40)
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, timeout=timeout, allow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0"},
            ) as resp:
                if resp.status != 200:
                    return None
                cl = resp.headers.get("Content-Length")
                if cl and int(cl) > max_size:
                    logger.warning("Media too large (%s bytes), skipping %s", cl, url[:60])
                    return None
                data = await resp.read()
                return data if len(data) > 5000 else None
    except Exception as ex:
        logger.debug("Fetch %s: %s", url[:60], ex)
        return None


async def _fetch_unsplash_multi(query: str, access_key: str, count: int = 1) -> list[bytes]:
    """Fetch up to `count` unique random images from Unsplash. Returns list of bytes."""
    if not access_key or not query or count < 1:
        return []
    try:
        headers = {"Authorization": f"Client-ID {access_key}"}
        params = {"query": query, "orientation": "landscape", "content_filter": "high", "count": min(count, 10)}
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.unsplash.com/photos/random",
                headers=headers, params=params,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    logger.warning("Unsplash API: HTTP %d", resp.status)
                    return []
                data = await resp.json()
        # API returns a list when count>1, a single object when count=1
        items = data if isinstance(data, list) else [data]
        urls = [item.get("urls", {}).get("regular") for item in items]
        urls = [u for u in urls if u]
        downloaded = await asyncio.gather(*[_fetch_url(u) for u in urls])
        results = [d for d in downloaded if d]
        if results:
            logger.info("Unsplash fetched %d image(s) for query '%s'", len(results), query[:40])
        return results
    except Exception as ex:
        logger.debug("Unsplash error: %s", ex)
        return []


async def get_source_media(
    news_items: list[dict],
    max_items: int = 3,
    excluded_urls: set[str] | None = None,
    query: str = "",
    unsplash_key: str = "",
) -> tuple[list[tuple[bytes, str]], list[dict]]:
    """Collect up to max_items media from trusted RSS sources (CoinDesk, TheBlock).
    Returns ([(bytes, media_type)], used_news_items).
    """
    excluded = excluded_urls or set()
    seen_urls: set[str] = set()
    candidates: list[str] = []
    url_to_item: dict[str, dict] = {}

    for item in news_items:
        if item.get("channel") not in _TRUSTED_IMAGE_SOURCES:
            continue
        url = item.get("image_url")
        if url and url not in seen_urls and url not in excluded:
            seen_urls.add(url)
            candidates.append(url)
            url_to_item[url] = item
        if len(candidates) >= max_items * 4:
            break

    downloaded = await asyncio.gather(*[_fetch_url(u) for u in candidates])

    results: list[tuple[bytes, str]] = []
    used_items: list[dict] = []
    for url, data in zip(candidates, downloaded):
        if data is None:
            continue
        mtype = "video" if is_video(url) else "photo"
        if mtype == "video":
            data = await convert_to_9x16(data)
        results.append((data, mtype))
        if url in url_to_item:
            used_items.append(url_to_item[url])
        logger.info("Collected %s #%d: %dKB", mtype, len(results), len(data) // 1024)
        if len(results) >= max_items:
            break

    # Unsplash fallback: fill remaining slots with unique images in one API call
    if len(results) < max_items and unsplash_key and query:
        needed = max_items - len(results)
        unsplash_images = await _fetch_unsplash_multi(query, unsplash_key, count=needed)
        for data in unsplash_images:
            results.append((data, "photo"))
            if len(results) >= max_items:
                break

    return results, used_items
