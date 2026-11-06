import asyncpg
import logging
from config import DATABASE_URL

logger = logging.getLogger(__name__)
POOL: asyncpg.Pool = None


async def init_db():
    global POOL
    POOL = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10, command_timeout=30)
    logger.info("PostgreSQL connected")

    async with POOL.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS parsed_news (
                id BIGSERIAL PRIMARY KEY,
                source_channel TEXT,
                source_msg_id BIGINT,
                original_text TEXT,
                image_url TEXT,
                parsed_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (source_channel, source_msg_id)
            )
        """)
        try:
            await conn.execute("ALTER TABLE parsed_news ADD COLUMN IF NOT EXISTS image_url TEXT")
        except Exception:
            pass
        try:
            await conn.execute("ALTER TABLE parsed_news ADD COLUMN IF NOT EXISTS source_url TEXT")
        except Exception:
            pass
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS generated_posts (
                id BIGSERIAL PRIMARY KEY,
                title TEXT,
                body TEXT,
                image_url TEXT,
                hashtags TEXT,
                status TEXT DEFAULT 'pending',
                source_ids TEXT,
                approved_at TIMESTAMPTZ,
                posted_at TIMESTAMPTZ,
                channel_msg_id BIGINT,
                poll_question TEXT,
                poll_options TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        # Migrate: add poll columns if not exist
        for col in ("poll_question", "poll_options"):
            try:
                await conn.execute(f"ALTER TABLE generated_posts ADD COLUMN IF NOT EXISTS {col} TEXT")
            except Exception:
                pass
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS image_feedback (
                id BIGSERIAL PRIMARY KEY,
                prompt TEXT,
                rating INTEGER,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS channel_stats (
                id BIGSERIAL PRIMARY KEY,
                post_id BIGINT REFERENCES generated_posts(id),
                views INTEGER DEFAULT 0,
                reactions INTEGER DEFAULT 0,
                checked_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
    logger.info("Tables ready")


async def get_setting(key, default=None):
    async with POOL.acquire() as conn:
        val = await conn.fetchval("SELECT value FROM bot_settings WHERE key=$1", key)
        return val if val is not None else default


async def set_setting(key, value):
    async with POOL.acquire() as conn:
        await conn.execute(
            "INSERT INTO bot_settings (key, value) VALUES ($1, $2) ON CONFLICT (key) DO UPDATE SET value=$2",
            key, str(value),
        )


async def save_parsed(source_channel, msg_id, text, image_url=None, source_url=None):
    async with POOL.acquire() as conn:
        await conn.execute(
            "INSERT INTO parsed_news (source_channel, source_msg_id, original_text, image_url, source_url) "
            "VALUES ($1, $2, $3, $4, $5) ON CONFLICT DO NOTHING",
            source_channel, msg_id, text, image_url, source_url,
        )


async def get_unparsed_news(hours=6):
    async with POOL.acquire() as conn:
        return [dict(r) for r in await conn.fetch(
            "SELECT * FROM parsed_news WHERE parsed_at > NOW() - ($1 * INTERVAL '1 hour') ORDER BY parsed_at DESC",
            int(hours),
        )]


async def get_used_source_ids():
    """Get source_ids from all generated posts to avoid reusing same news."""
    async with POOL.acquire() as conn:
        rows = await conn.fetch(
            "SELECT source_ids FROM generated_posts WHERE created_at > NOW() - INTERVAL '3 days' AND source_ids != ''"
        )
        used = set()
        import json
        for r in rows:
            try:
                ids = json.loads(r["source_ids"])
                if isinstance(ids, list):
                    used.update(ids)
            except Exception:
                pass
        return used


async def save_post(title, body, image_url, hashtags, source_ids, poll_question=None, poll_options=None):
    async with POOL.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO generated_posts (title, body, image_url, hashtags, source_ids, poll_question, poll_options) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id",
            title, body, image_url, hashtags, source_ids, poll_question, poll_options,
        )


async def approve_post(post_id):
    async with POOL.acquire() as conn:
        await conn.execute(
            "UPDATE generated_posts SET status='approved', approved_at=NOW() WHERE id=$1", post_id
        )


async def reject_post(post_id):
    async with POOL.acquire() as conn:
        await conn.execute("UPDATE generated_posts SET status='rejected' WHERE id=$1", post_id)


async def mark_posted(post_id, channel_msg_id):
    async with POOL.acquire() as conn:
        await conn.execute(
            "UPDATE generated_posts SET status='posted', posted_at=NOW(), channel_msg_id=$2 WHERE id=$1",
            post_id, channel_msg_id,
        )


async def get_post(post_id):
    async with POOL.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM generated_posts WHERE id=$1", post_id)
        return dict(row) if row else None


async def save_image_bytes(post_id: int, img_bytes: bytes):
    async with POOL.acquire() as conn:
        await conn.execute(
            "UPDATE generated_posts SET image_url='has_image' WHERE id=$1", post_id
        )
        await set_setting(f"img_bytes_{post_id}", "saved")
        # Save to file
        import os
        os.makedirs("/tmp/cryptonews_images", exist_ok=True)
        with open(f"/tmp/cryptonews_images/{post_id}.jpg", "wb") as f:
            f.write(img_bytes)


async def load_image_bytes(post_id: int) -> bytes | None:
    try:
        path = f"/tmp/cryptonews_images/{post_id}.jpg"
        import os
        if os.path.exists(path):
            with open(path, "rb") as f:
                return f.read()
    except Exception:
        pass
    return None


async def save_image_feedback(prompt: str, rating: int):
    async with POOL.acquire() as conn:
        await conn.execute(
            "INSERT INTO image_feedback (prompt, rating) VALUES ($1, $2)",
            prompt, rating,
        )


async def get_good_prompts(limit=5):
    async with POOL.acquire() as conn:
        rows = await conn.fetch(
            "SELECT prompt FROM image_feedback WHERE rating = 1 ORDER BY created_at DESC LIMIT $1",
            limit,
        )
        return [r["prompt"] for r in rows]


async def get_bad_prompts(limit=5):
    async with POOL.acquire() as conn:
        rows = await conn.fetch(
            "SELECT prompt FROM image_feedback WHERE rating = -1 ORDER BY created_at DESC LIMIT $1",
            limit,
        )
        return [r["prompt"] for r in rows]


async def get_recent_titles(days=7):
    async with POOL.acquire() as conn:
        rows = await conn.fetch(
            "SELECT title FROM generated_posts WHERE created_at > NOW() - ($1 * INTERVAL '1 day') "
            "AND status != 'rejected' ORDER BY created_at DESC LIMIT 50",
            int(days),
        )
        return [r["title"] for r in rows]


async def get_pending_posts():
    async with POOL.acquire() as conn:
        return [dict(r) for r in await conn.fetch(
            "SELECT * FROM generated_posts WHERE status='pending' ORDER BY created_at DESC LIMIT 10"
        )]


async def get_last_posted_at():
    """Return datetime of the most recently posted post, or None."""
    async with POOL.acquire() as conn:
        return await conn.fetchval(
            "SELECT posted_at FROM generated_posts WHERE status='posted' ORDER BY posted_at DESC LIMIT 1"
        )


async def cleanup_old_news(keep_hours: int = 48) -> int:
    """Delete parsed_news older than keep_hours. Returns count of deleted rows."""
    async with POOL.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM parsed_news WHERE parsed_at < NOW() - ($1 * INTERVAL '1 hour')",
            keep_hours,
        )
        count = int(result.split()[-1])
        if count:
            logger.info("Cleaned up %d old parsed_news rows (>%dh)", count, keep_hours)
        return count
