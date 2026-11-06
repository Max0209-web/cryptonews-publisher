"""Shared Telethon client — single connection for both parsing and publishing."""
import logging
from telethon import TelegramClient
from telethon.sessions import StringSession

logger = logging.getLogger(__name__)

_client: TelegramClient | None = None
# Cache of per-admin clients: session_string -> TelegramClient
_admin_clients: dict[str, TelegramClient] = {}


async def _make_client(session_str: str) -> TelegramClient | None:
    """Create/reuse a connected authorized client for given session string."""
    from config import API_ID, API_HASH

    cached = _admin_clients.get(session_str)
    if cached is not None:
        if not cached.is_connected():
            await cached.connect()
        if await cached.is_user_authorized():
            return cached
        # stale — remove and recreate
        try:
            await cached.disconnect()
        except Exception:
            pass
        del _admin_clients[session_str]

    try:
        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return None
        _admin_clients[session_str] = client
        return client
    except Exception as ex:
        logger.warning("Telethon client error: %s", ex)
        return None


async def get_client() -> TelegramClient | None:
    """Return connected and authorized Telethon client (singleton). Returns None on any error."""
    global _client
    from config import API_ID, API_HASH, SESSION_STRING

    if not SESSION_STRING:
        return None

    try:
        if _client is None:
            _client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

        if not _client.is_connected():
            await _client.connect()

        if not await _client.is_user_authorized():
            logger.error("Telethon session not authorized")
            return None

        return _client
    except Exception as ex:
        logger.error("Telethon connect error: %s", ex)
        _client = None
        return None


async def is_channel_admin(client: TelegramClient, channel: str) -> bool:
    """Return True if client's account is admin or creator of the channel."""
    try:
        from telethon.tl.functions.channels import GetParticipantRequest
        from telethon.tl.types import ChannelParticipantAdmin, ChannelParticipantCreator
        me = await client.get_me()
        entity = await client.get_entity(channel)
        result = await client(GetParticipantRequest(entity, me))
        return isinstance(result.participant, (ChannelParticipantAdmin, ChannelParticipantCreator))
    except Exception:
        return False


async def get_best_client(admin_ids: list[int], channel: str | None = None) -> TelegramClient | None:
    """Return the best available Telethon client for publishing.

    If PREFERRED_SENDER_ID is set and has an active session, always use that account.
    Otherwise priority (when channel is given):
      1. Premium account that is admin in the channel
      2. Non-premium account that is admin in the channel
      3. Premium account (not admin — will still work if bot is admin)
      4. Any other authorized admin account
      5. Default SESSION_STRING client (fallback)
    """
    import database as db
    from config import PREFERRED_SENDER_ID

    # Always prefer the designated sender account
    if PREFERRED_SENDER_ID:
        sess = await db.get_setting(f"session_{PREFERRED_SENDER_ID}")
        if sess:
            client = await _make_client(sess)
            if client:
                try:
                    me = await client.get_me()
                    logger.info("Using preferred sender: %s (@%s)", me.id, me.username)
                except Exception:
                    pass
                return client

    # Gather info for all admin sessions
    candidates: list[tuple[TelegramClient, bool, bool]] = []  # (client, is_premium, is_chan_admin)
    for admin_id in admin_ids:
        sess = await db.get_setting(f"session_{admin_id}")
        if not sess:
            continue
        client = await _make_client(sess)
        if not client:
            continue
        try:
            me = await client.get_me()
            is_premium = bool(getattr(me, "premium", False))
        except Exception:
            is_premium = False

        if channel:
            chan_admin = await is_channel_admin(client, channel)
        else:
            chan_admin = False

        candidates.append((client, is_premium, chan_admin))
        logger.debug("Candidate id=%s premium=%s chan_admin=%s", getattr(me, "id", "?"), is_premium, chan_admin)

    # Pick by priority
    for want_premium, want_admin in [
        (True, True),   # premium + channel admin
        (False, True),  # channel admin only
        (True, False),  # premium only
        (False, False), # any
    ]:
        for client, is_premium, chan_admin in candidates:
            if (not want_premium or is_premium) and (not want_admin or chan_admin):
                try:
                    me = await client.get_me()
                    logger.info(
                        "Using Telethon account: %s (@%s) premium=%s chan_admin=%s",
                        me.id, me.username, is_premium, chan_admin,
                    )
                except Exception:
                    pass
                return client

    # Fallback to default SESSION_STRING
    return await get_client()


async def check_premium(session_str: str) -> tuple[bool, str]:
    """Check if given session string belongs to a Premium account.
    Returns (is_premium, display_name)."""
    client = await _make_client(session_str)
    if not client:
        return False, "не авторизован"
    try:
        me = await client.get_me()
        name = f"{me.first_name or ''} {me.last_name or ''}".strip() or f"id{me.id}"
        username = f" (@{me.username})" if me.username else ""
        is_premium = bool(getattr(me, "premium", False))
        return is_premium, f"{name}{username}"
    except Exception as ex:
        logger.warning("check_premium error: %s", ex)
        return False, "ошибка"


async def disconnect():
    global _client
    if _client and _client.is_connected():
        await _client.disconnect()
    _client = None
    for c in list(_admin_clients.values()):
        try:
            await c.disconnect()
        except Exception:
            pass
    _admin_clients.clear()
