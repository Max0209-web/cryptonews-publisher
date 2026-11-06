import logging
import os
from datetime import datetime, timezone, timedelta

from config import SOURCE_CHANNELS, PARSE_LIMIT
import database as db
import telethon_client as tg

logger = logging.getLogger(__name__)

MAX_AGE_HOURS = 24


async def parse_channels() -> list[dict]:
    """Parse recent messages from source channels. Returns list of fresh news."""
    client = await tg.get_client()
    if not client:
        logger.warning("No Telethon client — skipping TG parse")
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    all_news = []
    os.makedirs("/tmp/cryptonews_images", exist_ok=True)

    for channel_url in SOURCE_CHANNELS:
        try:
            channel = channel_url.split("/")[-1]
            entity = await client.get_entity(channel)
            count = 0
            async for msg in client.iter_messages(entity, limit=PARSE_LIMIT):
                if not msg.text or len(msg.text) < 50:
                    continue
                if msg.date and msg.date < cutoff:
                    continue
                image_url = None
                if msg.photo:
                    try:
                        img_bytes = await client.download_media(msg.photo, bytes)
                        if img_bytes and len(img_bytes) > 5000:
                            path = f"/tmp/cryptonews_images/tg_{channel}_{msg.id}.jpg"
                            with open(path, "wb") as f:
                                f.write(img_bytes)
                            image_url = f"file://{path}"
                    except Exception:
                        pass
                elif msg.video and msg.video.size < 20 * 1024 * 1024:
                    try:
                        vid_bytes = await client.download_media(msg.video, bytes)
                        if vid_bytes and len(vid_bytes) > 10000:
                            path = f"/tmp/cryptonews_images/tg_{channel}_{msg.id}.mp4"
                            with open(path, "wb") as f:
                                f.write(vid_bytes)
                            image_url = f"file://{path}"
                    except Exception:
                        pass
                await db.save_parsed(channel, msg.id, msg.text, image_url)
                all_news.append({
                    "channel": channel,
                    "msg_id": msg.id,
                    "text": msg.text,
                    "date": msg.date,
                    "image_url": image_url,
                })
                count += 1
            logger.info("Parsed %s: %d fresh", channel, count)
        except Exception as ex:
            logger.warning("Failed to parse %s: %s", channel_url, ex)

    logger.info("Total parsed: %d news (last %dh)", len(all_news), MAX_AGE_HOURS)
    return all_news
