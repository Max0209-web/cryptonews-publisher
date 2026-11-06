import asyncio
import json
import logging
import random
import re
import aiohttp
from datetime import datetime, timezone, timedelta

MSK = timezone(timedelta(hours=3))

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
    BufferedInputFile, InputMediaPhoto, InputMediaVideo,
)

import database as db
from config import BOT_TOKEN, ADMIN_IDS, CHANNEL_ID, DEFAULT_POST_HOURS, UNSPLASH_ACCESS_KEY
from parser import parse_channels
from rss_parser import parse_rss
from ai import generate_post
from images import get_source_media

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

router = Router()
router.message.filter(F.chat.type == "private")
router.callback_query.filter(lambda call: call.from_user.id in ADMIN_IDS)
_media_cache: dict = {}
_used_image_urls: set = set()
_media_source_urls: dict = {}
_session_clients: dict = {}
bot: Bot = None
_last_posted_at: datetime | None = None
_last_cleanup: datetime | None = None
_last_scheduled_slot: tuple | None = None  # (date, hour) to prevent double-post on restart
# Tracks preview message IDs per chat so cn_back can delete them: chat_id -> [msg_id, ...]
_preview_msg_ids: dict[int, list[int]] = {}


def _track_media_urls(news_items: list[dict]) -> None:
    """Mark image URLs from these news items as used. Keeps set bounded to 200 entries."""
    global _used_image_urls
    for n in news_items:
        url = n.get("image_url")
        if url:
            _used_image_urls.add(url)
    if len(_used_image_urls) > 200:
        _used_image_urls = set(list(_used_image_urls)[100:])


async def _get_all_prev_titles() -> list[str]:
    """Combine DB-tracked titles + recent channel posts to avoid topic repeats."""
    channel = await _get_channel()
    db_titles = await db.get_recent_titles()
    channel_titles = await _get_channel_titles(channel)
    seen: set[str] = set()
    result = []
    for t in db_titles + channel_titles:
        if t not in seen:
            seen.add(t)
            result.append(t)
    return result


async def _get_channel_titles(channel: str, limit: int = 30) -> list[str]:
    """Read recent posts from the channel via Telethon and return their first lines (titles)."""
    if not channel:
        return []
    try:
        import telethon_client as tg
        client = await tg.get_client()
        if not client:
            return []
        entity = await client.get_entity(channel)
        titles = []
        async for msg in client.iter_messages(entity, limit=limit):
            if msg.text and len(msg.text) > 30:
                first_line = msg.text.split('\n')[0].strip()
                if first_line:
                    titles.append(first_line)
        logger.info("Fetched %d recent titles from channel %s", len(titles), channel)
        return titles
    except Exception as ex:
        logger.warning("_get_channel_titles: %s", ex)
        return []


def _filter_relevant_news(post_title: str, news_items: list[dict]) -> list[dict]:
    """Return news items whose text is relevant to the post title (keyword overlap).
    Falls back to all items if nothing matches."""
    stop = {
        "в", "на", "и", "с", "по", "из", "для", "что", "как", "это", "то",
        "the", "a", "an", "of", "in", "to", "for", "is", "at", "by", "with",
        "and", "or", "that", "this", "are", "was", "has", "have", "will",
    }
    title_words = {w.lower() for w in re.findall(r'\b\w{3,}\b', post_title) if w.lower() not in stop}
    if not title_words:
        return news_items

    scored = []
    for item in news_items:
        text = (item.get("text", "") + " " + item.get("title", "")).lower()
        score = sum(1 for w in title_words if w in text)
        scored.append((score, item))

    scored.sort(key=lambda x: -x[0])
    relevant = [item for score, item in scored if score >= 1]
    return relevant if relevant else news_items


def _is_duplicate_title(new_title: str, recent_titles: list[str]) -> bool:
    """Return True if new_title shares 2+ keywords with any recent title."""
    stop = {
        "в", "на", "и", "с", "по", "из", "для", "что", "как", "это", "то",
        "the", "a", "an", "of", "in", "to", "for", "is", "at", "by", "with",
        "and", "or", "that", "this", "are", "was", "has", "have", "will",
    }
    # Strip HTML tags before comparing to avoid false matches on tag words like "emoji"
    def _clean(t: str) -> str:
        return re.sub(r'<[^>]+>', '', t)

    new_words = {w.lower() for w in re.findall(r'\b\w{4,}\b', _clean(new_title)) if w.lower() not in stop}
    if len(new_words) < 2:
        return False
    for recent in recent_titles[:20]:
        recent_words = {w.lower() for w in re.findall(r'\b\w{4,}\b', _clean(recent)) if w.lower() not in stop}
        if len(new_words & recent_words) >= 2:
            logger.info("Duplicate title skipped: '%s' ~ '%s'", new_title, recent)
            return True
    return False


def _build_media_source_links(news_items: list[dict]) -> list[str]:
    """Build source links for news items that have images (Telegram posts or article URLs)."""
    links = []
    for n in news_items:
        if not n.get("image_url"):
            continue
        channel = n.get("channel", "")
        msg_id = n.get("msg_id")
        if channel and msg_id and not channel.startswith("rss_"):
            link = _channel_post_link(channel, msg_id)
            if link:
                links.append(link)
        elif n.get("url", "").startswith("http"):
            links.append(n["url"])
        if len(links) >= 5:
            break
    return links


def is_admin(uid):
    return uid in ADMIN_IDS


def pe(key: str) -> str:
    return ""


def _ib(text: str, callback_data: str = None, icon: str = None, url: str = None, style: str = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=callback_data, url=url)


def _channel_post_link(channel: str, msg_id: int) -> str:
    """Build a t.me link to a channel message."""
    if not msg_id:
        return ""
    if channel.startswith("@"):
        return f"https://t.me/{channel.lstrip('@')}/{msg_id}"
    if channel.startswith("-100"):
        return f"https://t.me/c/{channel[4:]}/{msg_id}"
    if channel:
        return f"https://t.me/{channel}/{msg_id}"
    return ""


async def _get_schedule() -> list[int]:
    raw = await db.get_setting("schedule")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return DEFAULT_POST_HOURS


async def _set_schedule(hours: list[int]):
    await db.set_setting("schedule", json.dumps(sorted(set(hours))))


async def _get_channel():
    return await db.get_setting("channel_id") or CHANNEL_ID


async def _set_channel(channel_id: str):
    await db.set_setting("channel_id", channel_id)


async def _get_posting_account_info(channel: str | None = None) -> str:
    """Return info about which Telethon account will be used for posting to the channel."""
    import telethon_client as tg
    channel = channel or await _get_channel()
    client = await tg.get_best_client(ADMIN_IDS, channel=channel)
    if not client:
        return " Нет авторизованного аккаунта — Premium-эмодзи могут не работать"
    try:
        me = await client.get_me()
        name = f"{me.first_name or ''} {me.last_name or ''}".strip() or f"id{me.id}"
        uname = f" (@{me.username})" if me.username else ""
        is_premium = bool(getattr(me, "premium", False))
        is_admin = await tg.is_channel_admin(client, channel) if channel else False
        icon = "" if is_premium else ""
        parts = []
        if is_premium:
            parts.append("Premium ")
        if channel:
            parts.append("админ канала " if is_admin else "не админ ")
        detail = " — " + ", ".join(parts) if parts else ""
        return f"{icon} Постит: {name}{uname}{detail}"
    except Exception:
        return " Ошибка проверки аккаунта"


async def _main_menu_text():
    schedule = await _get_schedule()
    channel = await _get_channel()
    autopost = await db.get_setting("autopost", "1")
    sch_str = ", ".join(f"{h}:00" for h in schedule) if schedule else "не задано"
    import telethon_client as tg
    # Check which account will post (premium priority)
    tg_status = " не авторизован"
    found_session = False
    for admin_id in ADMIN_IDS:
        sess = await db.get_setting(f"session_{admin_id}")
        if not sess:
            continue
        client = await tg._make_client(sess)
        if not client:
            continue
        try:
            me = await client.get_me()
            name = f"{me.first_name or ''} {me.last_name or ''}".strip() or f"id{me.id}"
            is_premium = bool(getattr(me, "premium", False))
            tg_status = f"{'' if is_premium else ''} {name}" + (" (Premium)" if is_premium else "")
            found_session = True
            if is_premium:
                break  # prefer premium — stop at first one
        except Exception:
            continue
    if not found_session:
        # No per-admin sessions — check default client
        try:
            if tg._client is not None and tg._client.is_connected():
                tg_status = " подключён"
        except Exception:
            pass

    auto_str = "включён" if autopost == "1" else "выключен"
    return (
        f"{pe('rocket')} Крипто-новостной бот\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        f"{pe('bell')} Канал: {channel or 'не задан'}\n"
        f"{pe('calendar')} Расписание: {sch_str} МСК\n"
        f" Автопостинг: {auto_str}\n"
        f"{pe('globe')} Telethon: {tg_status}\n"
    )


def _main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [_ib("Сгенерировать пост", "cn_generate", icon="rocket", style="primary")],
        [
            _ib("Дайджест", "cn_digest", icon="stats", style="primary"),
            _ib("Неопубликованные", "cn_pending", icon="news", style="primary"),
        ],
        [_ib("Настройки", "cn_settings", icon="shield", style="primary")],
    ])


def _settings_kb(autopost: str, schedule: list, channel: str):
    auto_icon = "ok" if autopost == "1" else "no"
    auto_style = "success" if autopost == "1" else "danger"
    auto_text = "Автопостинг: ВКЛ" if autopost == "1" else "Автопостинг: ВЫКЛ"
    return InlineKeyboardMarkup(inline_keyboard=[
        [_ib(auto_text, "cn_toggle_auto", icon=auto_icon, style=auto_style)],
        [_ib("Расписание", "cn_schedule", icon="calendar", style="primary")],
        [_ib(f"Канал: {channel or '?'}", "cn_set_channel", icon="globe", style="primary")],
        [_ib("Сессия Telethon", "cn_session", icon="shield", style="primary")],
        [_ib("Назад", "cn_back", icon="refresh")],
    ])


def _schedule_kb(hours: list):
    rows = []
    for h in range(6, 24):
        active = h in hours
        rows.append((f"{'' if active else '◻'} {h}:00", f"cn_hour_{h}"))

    kb_rows = []
    for i in range(0, len(rows), 4):
        kb_rows.append([InlineKeyboardButton(text=t, callback_data=d) for t, d in rows[i:i+4]])
    kb_rows.append([_ib("Назад", "cn_settings", icon="refresh")])
    return InlineKeyboardMarkup(inline_keyboard=kb_rows)


@router.message(Command("start"))
async def cmd_start(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Этот бот только для админов.")
        return
    text = await _main_menu_text()
    await message.answer(text, parse_mode="HTML", reply_markup=_main_kb())


@router.callback_query(lambda c: c.data == "cn_back")
async def cb_back(call: CallbackQuery):
    text = await _main_menu_text()
    await call.answer()
    chat_id = call.from_user.id
    preview_ids = _preview_msg_ids.pop(chat_id, None)
    if preview_ids:
        # Delete all preview messages (photo, text, links) and send fresh menu
        for mid in preview_ids:
            try:
                await bot.delete_message(chat_id, mid)
            except Exception:
                pass
        await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=_main_kb())
    else:
        try:
            await call.message.edit_text(text, parse_mode="HTML", reply_markup=_main_kb())
        except Exception:
            await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=_main_kb())


@router.callback_query(lambda c: c.data == "cn_settings")
async def cb_settings(call: CallbackQuery):
    autopost = await db.get_setting("autopost", "1")
    schedule = await _get_schedule()
    channel = await _get_channel()
    await call.answer()
    try:
        await call.message.edit_text(
            " Настройки\n━━━━━━━━━━━━━━━━━━━",
            parse_mode="HTML",
            reply_markup=_settings_kb(autopost, schedule, channel),
        )
    except Exception:
        pass


@router.callback_query(lambda c: c.data == "cn_toggle_auto")
async def cb_toggle_auto(call: CallbackQuery):
    current = await db.get_setting("autopost", "1")
    new_val = "0" if current == "1" else "1"
    await db.set_setting("autopost", new_val)
    autopost = new_val
    schedule = await _get_schedule()
    channel = await _get_channel()
    status = "включён" if new_val == "1" else "выключен"
    await call.answer(f"Автопостинг {status}")
    try:
        await call.message.edit_reply_markup(reply_markup=_settings_kb(autopost, schedule, channel))
    except Exception:
        pass


@router.callback_query(lambda c: c.data == "cn_schedule")
async def cb_schedule(call: CallbackQuery):
    schedule = await _get_schedule()
    await call.answer()
    try:
        await call.message.edit_text(
            " Расписание\n━━━━━━━━━━━━━━━━━━━\n\n"
            "Нажми на время чтобы включить/выключить:",
            parse_mode="HTML",
            reply_markup=_schedule_kb(schedule),
        )
    except Exception:
        pass


@router.callback_query(lambda c: c.data and c.data.startswith("cn_hour_"))
async def cb_toggle_hour(call: CallbackQuery):
    h = int(call.data[8:])
    schedule = await _get_schedule()
    if h in schedule:
        schedule.remove(h)
    else:
        schedule.append(h)
    await _set_schedule(schedule)
    await call.answer(f"{h}:00 {'' if h in schedule else ''}")
    try:
        await call.message.edit_reply_markup(reply_markup=_schedule_kb(schedule))
    except Exception:
        pass


@router.callback_query(lambda c: c.data == "cn_set_channel")
async def cb_set_channel_prompt(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await call.message.edit_text(
        "Отправь @username канала:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_ib("Отмена", "cn_settings", icon="no")],
        ]),
    )
    await state.set_state("waiting_channel")


@router.message(F.text.startswith("@"))
async def msg_set_channel(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    current_state = await state.get_state()
    if current_state != "waiting_channel":
        return
    await state.clear()
    channel = message.text.strip()
    await _set_channel(channel)

    account_info = await _get_posting_account_info()

    text = await _main_menu_text()
    await message.answer(
        f" Канал: {channel}\n{account_info}\n\n{text}",
        parse_mode="HTML", reply_markup=_main_kb(),
    )


@router.callback_query(lambda c: c.data == "cn_session")
async def cb_session_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await call.answer()
    await call.message.edit_text(
        " Генерация новой Telethon-сессии\n\n"
        "Введи номер телефона аккаунта Telegram в формате:\n"
        "<code>+79991234567</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_ib("Отмена", "cn_settings", icon="no")],
        ]),
    )
    await state.set_state("session_phone")


@router.message(F.text & F.text.regexp(r'^\+\d+'))
async def msg_session_phone(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if await state.get_state() != "session_phone":
        return
    phone = message.text.strip()
    await message.delete()
    msg = await message.answer("⏳ Отправляю код...")
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        from config import API_ID, API_HASH

        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        result = await client.send_code_request(phone)
        _session_clients[message.from_user.id] = client
        await state.update_data(phone=phone, phone_hash=result.phone_code_hash)
        await msg.edit_text(
            " Код отправлен в Telegram.\n\n"
            "Введи код через точки, чтобы Telegram не заблокировал:\n"
            "Пример: <code>1.2.3.4.5</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [_ib("Отмена", "cn_session_cancel", icon="no")],
            ]),
        )
        await state.set_state("session_code")
    except Exception as ex:
        await msg.edit_text(f" Ошибка: <code>{ex}</code>", parse_mode="HTML")
        await state.clear()


@router.message(F.text & F.text.regexp(r'^[\d][.\d]+$'))
async def msg_session_code(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if await state.get_state() != "session_code":
        return
    code = message.text.strip().replace(".", "").replace(" ", "")
    await message.delete()
    msg = await message.answer("⏳ Проверяю код...")
    data = await state.get_data()
    client = _session_clients.get(message.from_user.id)
    if not client:
        await msg.edit_text(" Сессия истекла. Начни заново через Настройки.")
        await state.clear()
        return
    try:
        await client.sign_in(phone=data["phone"], code=code, phone_code_hash=data["phone_hash"])
        await _finish_session(message.from_user.id, client, msg, state)
    except Exception as ex:
        if "SessionPassword" in type(ex).__name__ or "two" in str(ex).lower():
            await msg.edit_text(
                " Требуется пароль двухфакторной аутентификации.\n\nВведи пароль:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [_ib("Отмена", "cn_session_cancel", icon="no")],
                ]),
            )
            await state.set_state("session_2fa")
        else:
            await msg.edit_text(f" Ошибка: <code>{ex}</code>", parse_mode="HTML")
            await state.clear()
            _session_clients.pop(message.from_user.id, None)


@router.message(F.text, StateFilter("session_2fa"))
async def msg_session_2fa(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if await state.get_state() != "session_2fa":
        return
    password = message.text.strip()
    await message.delete()
    msg = await message.answer("⏳ Проверяю пароль...")
    client = _session_clients.get(message.from_user.id)
    if not client:
        await msg.edit_text(" Сессия истекла. Начни заново через Настройки.")
        await state.clear()
        return
    try:
        await client.sign_in(password=password)
        await _finish_session(message.from_user.id, client, msg, state)
    except Exception as ex:
        await msg.edit_text(f" Неверный пароль: <code>{ex}</code>", parse_mode="HTML")
        await state.clear()
        _session_clients.pop(message.from_user.id, None)


async def _finish_session(user_id: int, client, msg, state: FSMContext):
    """Save new session string to .env and DB, restart Telethon."""
    import telethon_client as tg
    session_str = client.session.save()
    await client.disconnect()
    _session_clients.pop(user_id, None)
    await state.clear()

    # Save to .env on server (default session)
    try:
        env_path = "/root/bot/cryptonews/.env"
        try:
            with open(env_path, "r") as f:
                env_content = f.read()
        except FileNotFoundError:
            env_content = ""

        if "SESSION_STRING=" in env_content:
            env_content = re.sub(r"SESSION_STRING=.*", f"SESSION_STRING={session_str}", env_content)
        else:
            env_content += f"\nSESSION_STRING={session_str}\n"

        with open(env_path, "w") as f:
            f.write(env_content)
    except Exception as ex:
        logger.warning("Could not write .env: %s", ex)

    import config
    config.SESSION_STRING = session_str
    tg._client = None

    # Save per-admin session to DB so get_best_client() can find it
    await db.set_setting(f"session_{user_id}", session_str)

    # Check if this account has Premium
    is_premium, display_name = await tg.check_premium(session_str)
    premium_line = f"{pe('coin')} Аккаунт подключён" if is_premium else f"{pe('warn')} Аккаунт подключён"

    await msg.edit_text(
        f"{pe('ok')} Новая сессия сохранена!\n\n"
        f" {display_name}\n"
        f"{premium_line}\n\n"
        f"<code>{session_str[:40]}...</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_ib("Главная", "cn_back", icon="rocket", style="primary")],
        ]),
    )
    logger.info("New Telethon session saved for user %s (%s), premium=%s", user_id, display_name, is_premium)


@router.callback_query(lambda c: c.data == "cn_session_cancel")
async def cb_session_cancel(call: CallbackQuery, state: FSMContext):
    user_id = call.from_user.id
    client = _session_clients.pop(user_id, None)
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass
    await state.clear()
    autopost = await db.get_setting("autopost", "1")
    schedule = await _get_schedule()
    channel = await _get_channel()
    await call.answer()
    await call.message.edit_text(
        " Настройки", parse_mode="HTML",
        reply_markup=_settings_kb(autopost, schedule, channel),
    )


@router.callback_query(lambda c: c.data == "cn_generate")
async def cb_generate(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    import time as _time
    _start = _time.time()
    await call.answer("⏳ Генерирую...")
    await call.message.edit_text("⏳ Парсинг каналов + генерация...\nпримерно ~1 мин", parse_mode="HTML")

    try:
        cached = await db.get_unparsed_news(3)
        news = [{"channel": c["source_channel"], "msg_id": c["source_msg_id"], "text": c["original_text"], "image_url": c.get("image_url"), "url": c.get("source_url") or ""} for c in cached]
        if len(news) < 10:
            await call.message.edit_text("⏳ Парсинг источников...", parse_mode="HTML")
            fresh = await parse_all_sources()
            if fresh:
                news = fresh
        if not news:
            text = await _main_menu_text()
            await call.message.edit_text(text + "\n Нет новостей", parse_mode="HTML", reply_markup=_main_kb())
            return

        used_ids = await db.get_used_source_ids()
        fresh_news = [n for n in news if n["msg_id"] not in used_ids]
        if not fresh_news:
            fresh_news = news

        prev_titles = await _get_all_prev_titles()
        post_count = len(prev_titles)
        include_poll = (post_count % 5 == 4)
        post = await generate_post(fresh_news, previous_titles=prev_titles, include_poll=include_poll)
        if not post:
            text = await _main_menu_text()
            await call.message.edit_text(text + "\n AI не смог сгенерировать", parse_mode="HTML", reply_markup=_main_kb())
            return

        relevant_news = _filter_relevant_news(post["title"], fresh_news)
        media_list, media_used_items = await get_source_media(
            relevant_news, excluded_urls=_used_image_urls,
            query=post["title"], unsplash_key=UNSPLASH_ACCESS_KEY,
        )
        _track_media_urls(media_used_items)
        image_url = "bytes:pending" if media_list else None
        full_text = post["title"] + "\n\n" + post["body"]
        if post.get("analysis"):
            full_text += "\n\n" + post["analysis"]
        import json as _json
        src_ids = _json.dumps([n["msg_id"] for n in fresh_news[:10]])
        poll_q = post.get("poll_question")
        poll_o = _json.dumps(post["poll_options"]) if post.get("poll_options") else None
        post_id = await db.save_post(post["title"], full_text, image_url, post.get("hashtags", ""), src_ids, poll_q, poll_o)
        if media_list:
            _media_cache[post_id] = media_list
            _media_source_urls[post_id] = _build_media_source_links(media_used_items)
            await db.save_image_bytes(post_id, media_list[0][0])

        try:
            await call.message.edit_text(f" Готово за {_time.time() - _start:.0f}с, загружаю...")
        except Exception:
            pass
        await _send_preview(call.from_user.id, post_id)
        try:
            await call.message.delete()
        except Exception:
            pass

    except Exception as ex:
        logger.error("cb_generate error: %s", ex, exc_info=True)
        try:
            await call.message.edit_text(f" Ошибка генерации: {ex}", reply_markup=_main_kb())
        except Exception:
            pass


@router.callback_query(lambda c: c.data == "cn_pending")
async def cb_pending(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    posts = await db.get_pending_posts()
    if not posts:
        await call.answer("Нет неопубликованных постов")
        return
    await call.answer()
    for p in posts[:5]:
        await _send_preview(call.from_user.id, p["id"])


@router.callback_query(lambda c: c.data == "cn_digest")
async def cb_digest(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    await call.answer("⏳ Собираю дайджест...")

    news = await parse_all_sources()
    if not news:
        cached = await db.get_unparsed_news(24)
        news = [{"channel": c["source_channel"], "text": c["original_text"]} for c in cached]

    if not news:
        try:
            await call.message.edit_text(" Нет новостей за 24 часа", reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[_ib("Назад", "cn_back", icon="refresh")]]))
        except Exception:
            pass
        return

    def _clean_news_text(text: str) -> str:
        """Strip markdown, links, emojis — leave only clean text."""
        t = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)  # [text](url) → text
        t = re.sub(r'https?://\S+', '', t)  # remove URLs
        t = re.sub(r'\*\*(.+?)\*\*', r'\1', t)  # **bold** → bold
        t = re.sub(r'__(.+?)__', r'\1', t)  # __italic__ → italic
        t = re.sub(r'[⬆⬇⏰‼]', '', t)
        t = re.sub(r'\s+', ' ', t).strip()
        return t[:100]

    seen = set()
    topics = []
    for n in news:
        clean = _clean_news_text(n["text"])
        if len(clean) < 20:
            continue
        key = clean[:30].lower()
        if key in seen:
            continue
        seen.add(key)
        ch = n.get("channel", "?")
        topics.append(f"▸ {clean}\n  ({ch})")

    digest = (
        f" Новости за 24 часа\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"{len(topics)} тем из {len(set(n.get('channel','') for n in news))} каналов\n\n"
    )
    digest += "\n\n".join(topics[:20])

    try:
        await call.message.edit_text(
            digest[:4000],
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [_ib("Сгенерировать пост", "cn_generate", icon="rocket", style="primary")],
                [_ib("Назад", "cn_back", icon="refresh")],
            ]),
        )
    except Exception:
        await call.message.answer(digest[:4000], parse_mode="HTML")


async def _generate_train_image(prompt: str) -> bytes | None:
    """Generate image via Pollinations for the train feature."""
    import urllib.parse
    encoded = urllib.parse.quote(prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=1024&nologo=true"
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=timeout) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    return data if len(data) > 5000 else None
    except Exception as ex:
        logger.debug("Pollinations train image error: %s", ex)
    return None


@router.callback_query(lambda c: c.data == "cn_train")
async def cb_train(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    await call.answer("⏳ Генерирую картинку для оценки...")

    samples = [
        "золотая монета биткоина на тёмном столе",
        "торговый зал биржи с мониторами",
        "серверная комната с синей подсветкой",
        "здание суда с колоннами",
        "монеты криптовалют крупным планом",
        "ночной город с небоскрёбами финансового района",
        "экран компьютера с графиками",
        "сейф банка с золотыми слитками",
    ]
    titles = await db.get_recent_titles()
    if titles:
        import re as _re
        clean = [_re.sub(r'<[^>]+>', '', t).strip()[:40] for t in titles[:3]]
        samples = clean + samples
    sample = random.choice(samples)

    img_data = await _generate_train_image(sample)
    if not img_data:
        img_data = await _generate_train_image("золотая монета биткоина на тёмном фоне")
    if not img_data:
        await call.message.answer(" Не удалось сгенерировать изображение.")
        return

    prompt_id = hash(sample) % 999999
    _media_cache[f"train_{prompt_id}"] = [(img_data, "photo")]
    await db.set_setting(f"train_prompt_{prompt_id}", sample)

    good_count = len(await db.get_good_prompts(100))
    bad_count = len(await db.get_bad_prompts(100))

    await bot.send_photo(
        call.from_user.id,
        BufferedInputFile(img_data, "train.jpg"),
        caption=f" Обучение картинок\n\nПромпт: {sample[:80]}\n\nОценок:  {good_count} /  {bad_count}\n\nОцени эту картинку:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                _ib("Нравится", f"cn_rate_1_{prompt_id}", icon="ok", style="success"),
                _ib("Не нравится", f"cn_rate_0_{prompt_id}", icon="no", style="danger"),
            ],
            [_ib("Ещё картинку", "cn_train", icon="refresh", style="primary")],
            [_ib("Назад", "cn_back", icon="rocket")],
        ]),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("cn_rate_"))
async def cb_rate(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    parts = call.data.split("_")
    if len(parts) < 4:
        await call.answer()
        return
    try:
        rating = int(parts[2]) 
    except ValueError:
        await call.answer()
        return
    prompt_id = parts[3]

    prompt_text = await db.get_setting(f"train_prompt_{prompt_id}")
    if prompt_text:
        await db.save_image_feedback(prompt_text, 1 if rating == 1 else -1)

    emoji = "" if rating == 1 else ""
    await call.answer(f"{emoji} Оценка сохранена")

    try:
        await call.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_ib(f"{emoji} Оценено", "noop")],
            [_ib("Ещё картинку", "cn_train", icon="refresh")],
            [_ib("Назад", "cn_back", icon="rocket")],
        ]))
    except Exception:
        pass


@router.callback_query(lambda c: c.data == "cn_status")
async def cb_status(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    cached = await db.get_unparsed_news(24)
    pending = await db.get_pending_posts()
    schedule = await _get_schedule()
    now = datetime.now(MSK)
    next_hours = sorted([h for h in schedule if h > now.hour])
    next_post = f"{next_hours[0]}:00" if next_hours else (f"{sorted(schedule)[0]}:00 (завтра)" if schedule else "—")
    await call.answer()
    try:
        await call.message.edit_text(
            f" Статус\n"
            f"━━━━━━━━━━━━━━━━━━━\n\n"
            f" Новостей за 24ч: {len(cached)}\n"
            f" Ожидают: {len(pending)}\n"
            f"⏰ Следующий пост: {next_post} МСК",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [_ib("Назад", "cn_back", icon="refresh")],
            ]),
        )
    except Exception:
        pass


@router.message(Command("test"))
async def cmd_test(message: Message):
    if not is_admin(message.from_user.id):
        return
    channel = await _get_channel()
    if not channel:
        await message.answer("Канал не задан! /setchannel @name")
        return
    msg = await message.answer("⏳ Генерирую тестовый пост...")

    fake_news = [
        {"channel": "test", "msg_id": 1, "text": "Bitcoin преодолел отметку $95,000 на фоне притока институциональных инвестиций. По данным CoinGlass, за последние 24 часа объём торгов BTC вырос на 45%. Крупнейшие криптобиржи фиксируют рекордные объёмы покупок от крупных институциональных игроков."},
        {"channel": "test", "msg_id": 2, "text": "Ethereum завершил обновление Pectra, которое снизило комиссии в сети на 30%. Разработчики подтвердили успешное развёртывание на основной сети. Цена ETH выросла на 8% после новости, достигнув $3,800."},
        {"channel": "test", "msg_id": 3, "text": "SEC одобрила заявку на спотовый Solana ETF от Franklin Templeton. Это третий крипто-ETF после Bitcoin и Ethereum. Аналитики Bloomberg ожидают приток $2-5 млрд в первый год торгов."},
    ]

    post = await generate_post(fake_news)
    if not post:
        await msg.edit_text(" AI не смог сгенерировать")
        return

    relevant_fake = _filter_relevant_news(post["title"], fake_news)
    media_list, media_used_items = await get_source_media(
        relevant_fake, excluded_urls=_used_image_urls,
        query=post["title"], unsplash_key=UNSPLASH_ACCESS_KEY,
    )
    _track_media_urls(media_used_items)
    image_url = "bytes:pending" if media_list else None
    full_text = post["title"] + "\n\n" + post["body"]
    if post.get("analysis"):
        full_text += "\n\n" + post["analysis"]

    import json as _json
    poll_q = post.get("poll_question")
    poll_o = _json.dumps(post["poll_options"]) if post.get("poll_options") else None
    post_id = await db.save_post(post["title"], full_text, image_url, post.get("hashtags", ""), "", poll_q, poll_o)
    if media_list:
        _media_cache[post_id] = media_list
        _media_source_urls[post_id] = _build_media_source_links(media_used_items)
    await msg.delete()
    await _send_preview(message.chat.id, post_id)



async def _show_pending_posts(chat_id: int, notify_msg=None):
    """Show list of generated-but-not-posted posts."""
    pending = await db.get_pending_posts()
    if not pending:
        if notify_msg:
            await notify_msg.edit_text(
                f"{pe('warn')} Новые темы закончились и нет готовых постов.\n"
                "Попробуй позже когда появятся новые новости.",
                parse_mode="HTML",
            )
        return
    if notify_msg:
        await notify_msg.edit_text(
            f"{pe('warn')} Новые темы закончились — все актуальные уже опубликованы.\n\n"
            f"{pe('news')} Готовые неопубликованные посты ({len(pending)}):",
            parse_mode="HTML",
        )
    for p in pending[:5]:
        await _send_preview(chat_id, p["id"])


@router.message(Command("generate"))
async def cmd_generate(message: Message):
    if not is_admin(message.from_user.id):
        return
    import time as _time
    _start = _time.time()
    msg = await message.answer("⏳ Парсинг + генерация...\nпримерно ~1 мин", parse_mode="HTML")

    try:
        cached = await db.get_unparsed_news(3)
        news = [{"channel": c["source_channel"], "msg_id": c["source_msg_id"], "text": c["original_text"], "image_url": c.get("image_url"), "url": c.get("source_url") or ""} for c in cached]
        if len(news) < 10:
            await msg.edit_text("⏳ Парсинг источников...")
            fresh = await parse_all_sources()
            if fresh:
                news = fresh

        if not news:
            await msg.edit_text(" Нет новостей для генерации")
            return

        # Filter already-used source IDs
        used_ids = await db.get_used_source_ids()
        fresh_news = [n for n in news if n["msg_id"] not in used_ids] or news

        await msg.edit_text("⏳ AI генерирует пост...")
        prev_titles = await _get_all_prev_titles()
        post = await generate_post(fresh_news, previous_titles=prev_titles)

        # If AI failed or generated a duplicate topic — show pending posts instead
        if not post or _is_duplicate_title(post["title"], prev_titles):
            await _show_pending_posts(message.chat.id, notify_msg=msg)
            return

        relevant_news = _filter_relevant_news(post["title"], fresh_news)
        media_list, media_used_items = await get_source_media(
            relevant_news, excluded_urls=_used_image_urls,
            query=post["title"], unsplash_key=UNSPLASH_ACCESS_KEY,
        )
        _track_media_urls(media_used_items)
        image_url = "bytes:pending" if media_list else None
        full_text = post["title"] + "\n\n" + post["body"]
        if post.get("analysis"):
            full_text += "\n\n" + post["analysis"]

        import json as _json
        src_ids = _json.dumps([n["msg_id"] for n in fresh_news[:10]])
        poll_q = post.get("poll_question")
        poll_o = _json.dumps(post["poll_options"]) if post.get("poll_options") else None
        post_id = await db.save_post(
            post["title"], full_text, image_url,
            post.get("hashtags", ""), src_ids, poll_q, poll_o,
        )
        if media_list:
            _media_cache[post_id] = media_list
            _media_source_urls[post_id] = _build_media_source_links(media_used_items)
            await db.save_image_bytes(post_id, media_list[0][0])

        await msg.edit_text(f" Готово за {_time.time() - _start:.0f}с, загружаю...")
        await _send_preview(message.chat.id, post_id)
        await msg.delete()

    except Exception as ex:
        logger.error("cmd_generate error: %s", ex, exc_info=True)
        try:
            await msg.edit_text(f" Ошибка: {ex}")
        except Exception:
            pass


@router.message(Command("pending"))
async def cmd_pending(message: Message):
    if not is_admin(message.from_user.id):
        return
    posts = await db.get_pending_posts()
    if not posts:
        await message.answer("Нет неопубликованных постов.")
        return
    for p in posts[:5]:
        await _send_preview(message.chat.id, p["id"])


@router.message(Command("status"))
async def cmd_status(message: Message):
    if not is_admin(message.from_user.id):
        return
    cached = await db.get_unparsed_news(24)
    pending = await db.get_pending_posts()
    schedule = await _get_schedule()
    channel = await _get_channel()
    now = datetime.now(MSK)
    next_hours = [h for h in schedule if h > now.hour]
    next_post = f"{next_hours[0]}:00 МСК" if next_hours else (f"{sorted(schedule)[0]}:00 МСК (завтра)" if schedule else "нет")

    await message.answer(
        f"Статус\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f" Новостей за 24ч: {len(cached)}\n"
        f" Ожидают: {len(pending)}\n"
        f"⏰ Следующий: {next_post}\n"
        f" Канал: {channel or 'не задан'}\n"
        f" Расписание: {', '.join(f'{h}:00' for h in schedule)} МСК",
        parse_mode="HTML",
    )


@router.message(Command("schedule"))
async def cmd_schedule(message: Message):
    if not is_admin(message.from_user.id):
        return
    schedule = await _get_schedule()
    hours_str = ", ".join(f"{h}:00" for h in sorted(schedule)) if schedule else "не задано"
    await message.answer(
        f"Расписание постинга (МСК):\n\n"
        f"{hours_str}\n\n"
        f"Изменить: /setschedule 6,10,15,19\n"
        f"Добавить: /addtime 14\n"
        f"Убрать: /deltime 10",
        parse_mode="HTML",
    )


@router.message(Command("setschedule"))
async def cmd_setschedule(message: Message):
    if not is_admin(message.from_user.id):
        return
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Формат: /setschedule 6,10,15,19")
        return
    try:
        hours = [int(h.strip()) for h in args[1].split(",") if 0 <= int(h.strip()) <= 23]
        await _set_schedule(hours)
        await message.answer(f" Расписание (МСК): {', '.join(f'{h}:00' for h in sorted(hours))}")
    except Exception:
        await message.answer(" Формат: /setschedule 6,10,15,19")


@router.message(Command("addtime"))
async def cmd_addtime(message: Message):
    if not is_admin(message.from_user.id):
        return
    args = (message.text or "").split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("Формат: /addtime 14 (час МСК)")
        return
    h = int(args[1])
    if not 0 <= h <= 23:
        await message.answer("Час от 0 до 23")
        return
    schedule = await _get_schedule()
    if h not in schedule:
        schedule.append(h)
        await _set_schedule(schedule)
    await message.answer(f" Добавлено {h}:00 МСК")


@router.message(Command("deltime"))
async def cmd_deltime(message: Message):
    if not is_admin(message.from_user.id):
        return
    args = (message.text or "").split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("Формат: /deltime 14")
        return
    h = int(args[1])
    schedule = await _get_schedule()
    if h in schedule:
        schedule.remove(h)
        await _set_schedule(schedule)
        await message.answer(f" Убрано {h}:00 МСК")
    else:
        await message.answer(f" {h}:00 МСК нет в расписании")


@router.message(Command("setchannel"))
async def cmd_setchannel(message: Message):
    if not is_admin(message.from_user.id):
        return
    args = (message.text or "").split()
    if len(args) < 2:
        await message.answer("Формат: /setchannel @channelname")
        return
    channel = args[1]
    await _set_channel(channel)
    account_info = await _get_posting_account_info(channel)
    await message.answer(f" Канал: {channel}\n{account_info}", parse_mode="HTML")


_ALLOWED_TAGS = {"b", "i", "u", "s", "code", "pre", "a", "blockquote", "tg-spoiler", "tg-emoji", "span"}


def _clean_html(text: str) -> str:
    """Remove unsupported HTML tags, fix unclosed tags."""
    # Replace <br>, <br/>, <p>, </p> with newlines
    text = re.sub(r'<br\s*/?\s*>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</?p\s*>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</?div\s*>', '\n', text, flags=re.IGNORECASE)
    def _filter_tag(match):
        full = match.group(0)
        tag_name = re.match(r'</?([\w-]+)', full)
        if tag_name and tag_name.group(1).lower() in _ALLOWED_TAGS:
            return full
        return ""
    text = re.sub(r'<[^>]+>', _filter_tag, text)
    for tag in ("b", "i", "u", "s", "code", "blockquote"):
        opens = len(re.findall(f'<{tag}>', text, re.IGNORECASE))
        closes = len(re.findall(f'</{tag}>', text, re.IGNORECASE))
        for _ in range(opens - closes):
            text += f"</{tag}>"
        while closes > opens:
            text = text.replace(f"</{tag}>", "", 1)
            closes -= 1

    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _strip_custom_emoji(text: str) -> str:
    """Remove tg-emoji tags, keep fallback Unicode inside."""
    return re.sub(r"<tg-emoji[^>]*>(.*?)</tg-emoji>", r"\1", text)


def _html_to_bot_api(html: str):
    """Parse HTML into (plain_text, aiogram MessageEntity list) for Bot API.
    Offsets are UTF-16 code units — compatible with both Telethon and Bot API.
    Allows sending custom_emoji, bold, italic, spoiler, etc. without parse_mode."""
    from aiogram.types import MessageEntity as AiogramEntity
    plain, tl_ents = _telethon_parse_html(html)
    from telethon.tl.types import (
        MessageEntityBold, MessageEntityItalic, MessageEntityCode,
        MessageEntityPre, MessageEntityTextUrl, MessageEntityUrl,
        MessageEntityUnderline, MessageEntityStrike,
        MessageEntitySpoiler, MessageEntityCustomEmoji, MessageEntityBlockquote,
    )
    _MAP = {
        MessageEntityBold: "bold",
        MessageEntityItalic: "italic",
        MessageEntityCode: "code",
        MessageEntityPre: "pre",
        MessageEntityUrl: "url",
        MessageEntityUnderline: "underline",
        MessageEntityStrike: "strikethrough",
        MessageEntitySpoiler: "spoiler",
        MessageEntityBlockquote: "blockquote",
    }
    result = []
    for ent in tl_ents:
        t = type(ent)
        if t in _MAP:
            result.append(AiogramEntity(type=_MAP[t], offset=ent.offset, length=ent.length))
        elif t == MessageEntityTextUrl:
            result.append(AiogramEntity(type="text_link", offset=ent.offset, length=ent.length, url=ent.url))
        elif t == MessageEntityCustomEmoji:
            result.append(AiogramEntity(type="custom_emoji", offset=ent.offset, length=ent.length, custom_emoji_id=str(ent.document_id)))
    return plain, result


def _telethon_parse_html(html: str):
    """Parse HTML for Telethon.
    Manually handles <tg-emoji emoji-id='...'> → MessageEntityCustomEmoji
    and <tg-spoiler> → MessageEntitySpoiler (neither supported natively by Telethon parser).
    Returns (plain_text, entities) for use with formatting_entities= parameter."""
    from telethon.extensions.html import HTMLToTelegramParser
    from telethon.helpers import add_surrogate, del_surrogate
    from telethon.tl.types import MessageEntitySpoiler, MessageEntityCustomEmoji

    # Extract custom emoji in order: (fallback_text, emoji_id)
    emoji_items: list[tuple[str, int]] = []
    def _strip_emoji(m: re.Match) -> str:
        eid = m.group(1)
        fallback = m.group(2)
        emoji_items.append((fallback, int(eid)))
        return fallback
    html_no_emoji = re.sub(
        r"<tg-emoji[^>]*emoji-id=['\"](\d+)['\"][^>]*>(.*?)</tg-emoji>",
        _strip_emoji, html, flags=re.DOTALL,
    )

    # Extract spoiler plain texts
    spoiler_plains: list[str] = []
    for m in re.finditer(r'<tg-spoiler>(.*?)</tg-spoiler>', html_no_emoji, re.DOTALL):
        plain = re.sub(r'<[^>]+>', '', m.group(1))
        if plain:
            spoiler_plains.append(plain)

    parser = HTMLToTelegramParser()
    parser.feed(add_surrogate(html_no_emoji))
    parser.close()
    parsed = del_surrogate(parser.text)
    entities = list(parser.entities)

    # Inject MessageEntityCustomEmoji at the positions of each fallback char
    search_from = 0
    for fallback, doc_id in emoji_items:
        idx = parsed.find(fallback, search_from)
        if idx < 0:
            continue
        offset = len(add_surrogate(parsed[:idx]))
        length = len(add_surrogate(fallback))
        entities.append(MessageEntityCustomEmoji(offset=offset, length=length, document_id=doc_id))
        search_from = idx + len(fallback)

    # Inject MessageEntitySpoiler
    search_from = 0
    for plain in spoiler_plains:
        idx = parsed.find(plain, search_from)
        if idx < 0:
            continue
        offset = len(add_surrogate(parsed[:idx]))
        length = len(add_surrogate(plain))
        entities.append(MessageEntitySpoiler(offset=offset, length=length))
        search_from = idx + len(plain)

    return parsed, entities


def _safe_truncate(text: str, limit: int = 1024) -> str:
    """Truncate text without breaking HTML tags."""
    text = _clean_html(text)
    if len(text) <= limit:
        return text
    cut = text[:limit]
    last_open = cut.rfind('<')
    last_close = cut.rfind('>')
    if last_open > last_close:
        cut = cut[:last_open]
    return _clean_html(cut)


async def _send_preview(chat_id: int, post_id: int):
    post = await db.get_post(post_id)
    if not post:
        return

    text = _clean_html(post["body"])
    if post.get("hashtags"):
        text += f"\n\n{post['hashtags']}"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            _ib("Опубликовать", f"post_ok_{post_id}", icon="ok", style="success"),
            _ib("Отклонить", f"post_no_{post_id}", icon="no", style="danger"),
        ],
        [
            _ib("Перегенерировать", f"post_re_{post_id}", icon="refresh", style="primary"),
            _ib("Без фото", f"post_nophoto_{post_id}", icon="laptop", style="primary"),
        ],
        [_ib("Главная", "cn_back", icon="rocket")],
    ])

    media_list = _media_cache.get(post_id)
    if not media_list:
        db_bytes = await db.load_image_bytes(post_id)
        if db_bytes:
            media_list = [(db_bytes, "photo")]

    src_links = _media_source_urls.get(post_id, [])
    links_text = (" Источники фото:\n" + "\n".join(src_links)) if src_links else None

    clean_len = len(re.sub(r'<[^>]+>', '', text))
    has_caption = clean_len <= 1024

    # Track all sent message IDs so cn_back can delete them
    sent_ids: list[int] = []

    async def _send_text_with_kb(msg_text: str):
        """Send text+keyboard with fallback on custom emoji error."""
        try:
            m = await bot.send_message(chat_id, msg_text, parse_mode="HTML", reply_markup=kb)
            sent_ids.append(m.message_id)
        except Exception as ex:
            logger.warning("Send failed: %s", ex)
            no_ce = re.sub(r"<tg-emoji[^>]*>(.*?)</tg-emoji>", r"\1", msg_text)
            try:
                m = await bot.send_message(chat_id, no_ce, parse_mode="HTML", reply_markup=kb)
                sent_ids.append(m.message_id)
            except Exception:
                m = await bot.send_message(chat_id, re.sub(r'<[^>]+>', '', msg_text), reply_markup=kb)
                sent_ids.append(m.message_id)

    try:
        if not media_list:
            # Text only
            await _send_text_with_kb(text)
        elif len(media_list) == 1:
            data, mtype = media_list[0]
            fname = "news.mp4" if mtype == "video" else "news.jpg"
            send_fn = bot.send_video if mtype == "video" else bot.send_photo
            if has_caption:
                try:
                    m = await send_fn(
                        chat_id, BufferedInputFile(data, fname),
                        caption=text, parse_mode="HTML", reply_markup=kb,
                    )
                    sent_ids.append(m.message_id)
                except Exception as ex:
                    logger.warning("Media+caption failed: %s", ex)
                    no_ce = re.sub(r"<tg-emoji[^>]*>(.*?)</tg-emoji>", r"\1", text)
                    try:
                        m = await send_fn(chat_id, BufferedInputFile(data, fname),
                                          caption=no_ce, parse_mode="HTML", reply_markup=kb)
                        sent_ids.append(m.message_id)
                    except Exception:
                        m = await send_fn(chat_id, BufferedInputFile(data, fname))
                        sent_ids.append(m.message_id)
                        await _send_text_with_kb(text)
            else:
                m = await send_fn(chat_id, BufferedInputFile(data, fname))
                sent_ids.append(m.message_id)
                await _send_text_with_kb(text)
        else:
            album = []
            for i, (data, mtype) in enumerate(media_list[:10]):
                fname = "news.mp4" if mtype == "video" else "news.jpg"
                cap = (text if has_caption else None) if i == 0 else None
                parse = "HTML" if cap else None
                if mtype == "video":
                    album.append(InputMediaVideo(media=BufferedInputFile(data, fname), caption=cap, parse_mode=parse))
                else:
                    album.append(InputMediaPhoto(media=BufferedInputFile(data, fname), caption=cap, parse_mode=parse))
            msgs = await bot.send_media_group(chat_id, album)
            sent_ids.extend(m.message_id for m in msgs)
            if not has_caption:
                await _send_text_with_kb(text)
            else:
                m = await bot.send_message(chat_id, "⬆ Управление постом:", reply_markup=kb)
                sent_ids.append(m.message_id)

        if links_text:
            m = await bot.send_message(chat_id, links_text, parse_mode="HTML", disable_web_page_preview=True)
            sent_ids.append(m.message_id)
    except Exception as ex:
        logger.warning("Preview error: %s", ex)
        await _send_text_with_kb(text)

    if sent_ids:
        _preview_msg_ids[chat_id] = sent_ids


@router.callback_query(lambda c: c.data and c.data.startswith("post_ok_"))
async def cb_approve(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    post_id = int(call.data[8:])
    post = await db.get_post(post_id)
    if not post:
        await call.answer("Не найден")
        return

    channel = await _get_channel()
    if not channel:
        await call.answer("Канал не задан! /setchannel @name", show_alert=True)
        return

    await call.answer("⏳ Публикую...")

    msg_id = await _publish_post(post_id, channel)

    if msg_id is not None:
        link = _channel_post_link(channel, msg_id)
        try:
            btn = (
                _ib("Опубликовано — открыть", url=link, icon="ok", style="success")
                if link else
                _ib("Опубликовано", "noop", icon="ok", style="success")
            )
            await call.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [btn],
                    [_ib("Главная", "cn_back", icon="rocket")],
                ]
            ))
        except Exception:
            pass
    else:
        await call.message.answer(" Ошибка публикации")


@router.callback_query(lambda c: c.data and c.data.startswith("post_no_"))
async def cb_reject(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    post_id = int(call.data[8:])
    _media_cache.pop(post_id, None)
    _media_source_urls.pop(post_id, None)
    await db.reject_post(post_id)
    await call.answer("Отклонено")
    chat_id = call.from_user.id
    if len(_preview_msg_ids) > 100:
        for old_key in list(_preview_msg_ids.keys())[:-50]:
            _preview_msg_ids.pop(old_key, None)
    preview_ids = _preview_msg_ids.pop(chat_id, None)
    if preview_ids:
        for mid in preview_ids:
            try:
                await bot.delete_message(chat_id, mid)
            except Exception:
                pass
    else:
        try:
            await call.message.delete()
        except Exception:
            pass


@router.callback_query(lambda c: c.data and c.data.startswith("post_re_"))
async def cb_regen(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    await call.answer("⏳ Перегенерирую...")

    cached = await db.get_unparsed_news(12)
    news = [{"channel": c["source_channel"], "msg_id": c["source_msg_id"], "text": c["original_text"], "image_url": c.get("image_url"), "url": c.get("source_url") or ""} for c in cached]
    if not news:
        await call.message.answer(" Нет новостей")
        return

    prev_titles = await _get_all_prev_titles()
    post = await generate_post(news, previous_titles=prev_titles)
    if not post:
        await call.message.answer(" AI не смог")
        return

    relevant_news2 = _filter_relevant_news(post["title"], news)
    media_list, media_used_items = await get_source_media(
        relevant_news2, excluded_urls=_used_image_urls,
        query=post["title"], unsplash_key=UNSPLASH_ACCESS_KEY,
    )
    _track_media_urls(media_used_items)
    image_url = "bytes:pending" if media_list else None
    full_text = post["title"] + "\n\n" + post["body"]
    if post.get("analysis"):
        full_text += "\n\n" + post["analysis"]

    import json as _json
    poll_q = post.get("poll_question")
    poll_o = _json.dumps(post["poll_options"]) if post.get("poll_options") else None
    post_id = await db.save_post(post["title"], full_text, image_url, post.get("hashtags", ""), "", poll_q, poll_o)
    if media_list:
        _media_cache[post_id] = media_list
        _media_source_urls[post_id] = _build_media_source_links(media_used_items)

    try:
        await call.message.delete()
    except Exception:
        pass
    await _send_preview(call.from_user.id, post_id)


@router.callback_query(lambda c: c.data and c.data.startswith("post_nophoto_"))
async def cb_publish_nophoto(call: CallbackQuery):
    """Publish post without photo."""
    if not is_admin(call.from_user.id):
        return
    post_id = int(call.data[13:])
    channel = await _get_channel()
    if not channel:
        await call.answer("Канал не задан!", show_alert=True)
        return
    await call.answer("⏳ Публикую без фото...")
   
    _media_cache.pop(post_id, None)
  
    msg_id = await _publish_post(post_id, channel, force_no_image=True)
    if msg_id is not None:
        link = _channel_post_link(channel, msg_id)
        try:
            btn = (
                _ib("Опубликовано (без фото) — открыть", url=link, icon="ok", style="success")
                if link else
                _ib("Опубликовано (без фото)", "noop", icon="ok", style="success")
            )
            await call.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [btn],
                    [_ib("Главная", "cn_back", icon="rocket")],
                ]
            ))
        except Exception:
            pass
    else:
        await call.message.answer(" Ошибка публикации")


async def _publish_via_telethon(channel: str, text: str, media_list=None, poll_q=None, poll_opts=None, use_spoiler: bool = False) -> int | None:
    """Publish to channel via Telethon client with Premium account if available.
    Returns channel message ID on success, None on failure."""
    import telethon_client as tg
    client = await tg.get_best_client(ADMIN_IDS, channel=channel)
    if not client:
        return None

    try:
        entity = await client.get_entity(channel)

       
        tl_text, tl_entities = _telethon_parse_html(text)

        if media_list:
            import io
            bufs = []
            for data, mtype in media_list:
                buf = io.BytesIO(data)
                buf.name = "news.mp4" if mtype == "video" else "news.jpg"
                bufs.append(buf)
            clean_len = len(re.sub(r'<[^>]+>', '', text))
            has_caption = clean_len <= 1024
            if len(bufs) == 1:
                sent = await client.send_file(
                    entity, bufs[0],
                    caption=tl_text if has_caption else None,
                    formatting_entities=tl_entities if has_caption else None,
                    parse_mode=None,
                    force_document=False,
                    spoiler=use_spoiler,
                )
            else:
                sent = await client.send_file(
                    entity, bufs,
                    caption=tl_text if has_caption else None,
                    formatting_entities=tl_entities if has_caption else None,
                    parse_mode=None,
                    force_document=False,
                    spoiler=use_spoiler,
                )
            if not has_caption:
                sent = await client.send_message(
                    entity, tl_text,
                    formatting_entities=tl_entities,
                    parse_mode=None,
                )
        else:
            sent = await client.send_message(
                entity, tl_text,
                formatting_entities=tl_entities,
                parse_mode=None,
            )

      
        if isinstance(sent, list):
            channel_msg_id = sent[0].id if sent else 0
        else:
            channel_msg_id = sent.id if sent else 0

        # Send poll
        if poll_q and poll_opts:
            try:
                from telethon.tl.types import InputMediaPoll, Poll, PollAnswer
                import json as _j
                options = _j.loads(poll_opts) if isinstance(poll_opts, str) else poll_opts
                if options and len(options) >= 2:
                    answers = [PollAnswer(text=opt, option=bytes([i])) for i, opt in enumerate(options)]
                    poll = Poll(id=0, question=poll_q, answers=answers)
                    await client.send_message(entity, file=InputMediaPoll(poll=poll))
            except Exception as ex:
                logger.warning("Telethon poll error: %s", ex)

        return channel_msg_id
    except Exception as ex:
        logger.error("Telethon publish error: %s", ex)
        return None


async def _publish_post(post_id: int, channel: str, force_no_image: bool = False) -> int | None:
    """Publish post to channel. Try Telethon first (premium emoji), fallback to Bot API.
    Returns channel message ID on success, None on failure."""
    post = await db.get_post(post_id)
    if not post:
        return None

    text_premium = _clean_html(post["body"])
    if post.get("hashtags"):
        text_premium += f"\n\n{post['hashtags']}"

    media_list = None
    if not force_no_image:
        media_list = _media_cache.pop(post_id, None)
        if not media_list:
            db_bytes = await db.load_image_bytes(post_id)
            if db_bytes:
                media_list = [(db_bytes, "photo")]

    # Try Telethon first (premium emoji work)
    channel_msg_id = await _publish_via_telethon(
        channel, text_premium, media_list,
        post.get("poll_question"), post.get("poll_options"),
    )

    if channel_msg_id is None:
        # Bot API fallback — use entities= (not parse_mode) so custom emoji are preserved.
        # Telegram allows bots to post custom_emoji entities in channels they administer.
        try:
            bot_text, bot_entities = _html_to_bot_api(text_premium)
        except Exception as ex:
            logger.warning("_html_to_bot_api failed, stripping emoji: %s", ex)
            bot_text = _strip_custom_emoji(text_premium)
            bot_text = re.sub(r'<[^>]+>', '', bot_text)
            bot_entities = []

        # Truncate text+entities to 1024 chars so it always fits as caption
        CAP_LIMIT = 1024
        if len(bot_text) > CAP_LIMIT:
            bot_text = bot_text[:CAP_LIMIT].rsplit(None, 1)[0]  # break at word boundary
            bot_entities = [e for e in bot_entities if e.offset + e.length <= CAP_LIMIT]

        try:
            ents = bot_entities or None
            if media_list:
                if len(media_list) == 1:
                    data, mtype = media_list[0]
                    fname = "news.mp4" if mtype == "video" else "news.jpg"
                    send_fn = bot.send_video if mtype == "video" else bot.send_photo
                    sent = await send_fn(
                        channel, BufferedInputFile(data, fname),
                        caption=bot_text, caption_entities=ents,
                        parse_mode=None,
                    )
                    channel_msg_id = sent.message_id
                else:
                    album = []
                    for i, (data, mtype) in enumerate(media_list):
                        fname = "news.mp4" if mtype == "video" else "news.jpg"
                        cap = bot_text if i == 0 else None
                        cap_ents = ents if i == 0 else None
                        if mtype == "video":
                            album.append(InputMediaVideo(
                                media=BufferedInputFile(data, fname),
                                caption=cap, caption_entities=cap_ents,
                                parse_mode=None,
                            ))
                        else:
                            album.append(InputMediaPhoto(
                                media=BufferedInputFile(data, fname),
                                caption=cap, caption_entities=cap_ents,
                                parse_mode=None,
                            ))
                    sent_msgs = await bot.send_media_group(channel, album)
                    channel_msg_id = sent_msgs[0].message_id
            else:
                sent = await bot.send_message(
                    channel, bot_text, entities=ents, parse_mode=None)
                channel_msg_id = sent.message_id
        except Exception as ex:
            logger.error("Bot API publish error: %s", ex)
            return None

    global _last_posted_at
    await db.approve_post(post_id)
    await db.mark_posted(post_id, channel_msg_id)
    _last_posted_at = datetime.now(MSK)
    return channel_msg_id


@router.message(Command("feedback"))
async def cmd_feedback(message: Message):
    """Set AI feedback from audience observations."""
    if not is_admin(message.from_user.id):
        return
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        current = await db.get_setting("ai_feedback", "")
        await message.answer(
            f"Текущий фидбек для AI:\n{current or '(пусто)'}\n\n"
            f"Задать: /feedback Пиши короче, больше цифр, аудитория любит про ETF",
            parse_mode="HTML",
        )
        return
    await db.set_setting("ai_feedback", args[1])
    await message.answer(f" Фидбек сохранён: {args[1]}")


@router.message(Command("autopost"))
async def cmd_autopost(message: Message):
    if not is_admin(message.from_user.id):
        return
    current = await db.get_setting("autopost", "1")
    new_val = "0" if current == "1" else "1"
    await db.set_setting("autopost", new_val)
    status = "включён — посты публикуются автоматически" if new_val == "1" else "выключен — посты ждут одобрения"
    await message.answer(f"Автопостинг: {status}", parse_mode="HTML")


@router.callback_query(lambda c: c.data == "noop")
async def cb_noop(call: CallbackQuery):
    await call.answer()

async def parse_all_sources() -> list[dict]:
    """Parse Telegram channels + RSS feeds concurrently."""
    tg_task = asyncio.create_task(parse_channels())
    rss_task = asyncio.create_task(parse_rss())
    tg_news, rss_news = await asyncio.gather(tg_task, rss_task, return_exceptions=True)
    combined = []
    if isinstance(tg_news, list):
        combined.extend(tg_news)
    if isinstance(rss_news, list):
        combined.extend(rss_news)
    logger.info("Total from all sources: %d items (TG=%d, RSS=%d)",
                len(combined),
                len(tg_news) if isinstance(tg_news, list) else 0,
                len(rss_news) if isinstance(rss_news, list) else 0)
    return combined


async def background_parser():
    """Parse all sources every 3 hours in background to keep news fresh."""
    await asyncio.sleep(60) 
    while True:
        try:
            logger.info("Background parse started")
            news = await parse_all_sources()
            logger.info("Background parse done: %d news", len(news))
        except asyncio.CancelledError:
            break
        except Exception as ex:
            logger.error("Background parser: %s", ex)
        await asyncio.sleep(3 * 3600)  # every 3 hours


async def scheduled_generate():
    global _last_scheduled_slot
    while True:
        try:
            now = datetime.now(MSK)
            schedule = await _get_schedule()

            current_slot = (now.date(), now.hour)
            if now.hour in schedule and now.minute < 5 and current_slot != _last_scheduled_slot:
                _last_scheduled_slot = current_slot
                logger.info("Scheduled generation at %02d:00 МСК", now.hour)

                news = await parse_all_sources()
                if not news:
                    cached = await db.get_unparsed_news(12)
                    news = [{"channel": c["source_channel"], "msg_id": c["source_msg_id"], "text": c["original_text"], "image_url": c.get("image_url"), "url": c.get("source_url") or ""} for c in cached]

                if news:
                    used_ids = await db.get_used_source_ids()
                    fresh = [n for n in news if n["msg_id"] not in used_ids]
                    if not fresh:
                        fresh = news
                    prev_titles = await _get_all_prev_titles()
                    # Poll every 5th post
                    post_count = len(prev_titles)
                    include_poll = (post_count % 5 == 4)
                    post = await generate_post(fresh, previous_titles=prev_titles, include_poll=include_poll)
                    if post and _is_duplicate_title(post["title"], prev_titles):
                        logger.info("Scheduled: duplicate title skipped, checking pending posts")
                        post = None
                        autopost_check = await db.get_setting("autopost", "1")
                        channel_check = await _get_channel()
                        if autopost_check == "1" and channel_check:
                            pending = await db.get_pending_posts()
                            if pending:
                                pending_id = pending[0]["id"]
                                chan_msg_id = await _publish_post(pending_id, channel_check)
                                if chan_msg_id is not None:
                                    logger.info("Scheduled fallback: published pending #%d to %s (msg %d)", pending_id, channel_check, chan_msg_id)
                                    link = _channel_post_link(channel_check, chan_msg_id)
                                    for admin_id in ADMIN_IDS:
                                        try:
                                            kb_notify = InlineKeyboardMarkup(inline_keyboard=[[_ib("Открыть пост", url=link, icon="globe")]]) if link else None
                                            await bot.send_message(admin_id, f"{pe('ok')} Автопост (из очереди) #{pending_id}", parse_mode="HTML", reply_markup=kb_notify)
                                        except Exception:
                                            pass
                    if post:
                        relevant_fresh = _filter_relevant_news(post["title"], fresh)
                        media_list, media_used_items = await get_source_media(
                            relevant_fresh, excluded_urls=_used_image_urls,
                            query=post["title"], unsplash_key=UNSPLASH_ACCESS_KEY,
                        )
                        _track_media_urls(media_used_items)
                        image_url = "bytes:pending" if media_list else None
                        full_text = post["title"] + "\n\n" + post["body"]
                        if post.get("analysis"):
                            full_text += "\n\n" + post["analysis"]
                        import json as _json
                        src_ids = _json.dumps([n["msg_id"] for n in fresh[:10]])
                        poll_q = post.get("poll_question")
                        poll_o = _json.dumps(post["poll_options"]) if post.get("poll_options") else None
                        post_id = await db.save_post(post["title"], full_text, image_url, post.get("hashtags", ""), src_ids, poll_q, poll_o)
                        if media_list:
                            _media_cache[post_id] = media_list
                            _media_source_urls[post_id] = _build_media_source_links(media_used_items)

                        autopost = await db.get_setting("autopost", "1")
                        channel = await _get_channel()

                        if autopost == "1" and channel:
                            chan_msg_id = await _publish_post(post_id, channel)
                            if chan_msg_id is not None:
                                logger.info("Auto-posted #%d to %s (msg %d)", post_id, channel, chan_msg_id)
                            # Notify admins with link
                            link = _channel_post_link(channel, chan_msg_id) if chan_msg_id else ""
                            for admin_id in ADMIN_IDS:
                                try:
                                    status = f"{pe('ok')} Автопост опубликован" if chan_msg_id is not None else f"{pe('warn')} Ошибка публикации"
                                    text_notify = f"{status} #{post_id}\n\n{_clean_html(full_text[:300])}..."
                                    kb_notify = None
                                    if link:
                                        kb_notify = InlineKeyboardMarkup(inline_keyboard=[[
                                            _ib("Открыть пост", url=link, icon="globe")
                                        ]])
                                    await bot.send_message(admin_id, text_notify, parse_mode="HTML", reply_markup=kb_notify)
                                except Exception:
                                    pass
                        else:
                            for admin_id in ADMIN_IDS:
                                try:
                                    await _send_preview(admin_id, post_id)
                                except Exception:
                                    pass
                        logger.info("Generated post #%d (auto=%s)", post_id, autopost)

                # Daily DB cleanup
                global _last_cleanup
                if _last_cleanup is None or (now - _last_cleanup).total_seconds() > 86400:
                    await db.cleanup_old_news(keep_hours=48)
                    _last_cleanup = now

                await asyncio.sleep(600)
            else:
                await asyncio.sleep(30)
        except asyncio.CancelledError:
            break
        except Exception as ex:
            logger.error("Scheduler: %s", ex)
            await asyncio.sleep(60)

async def silence_monitor():
    """Alert admins if no post has been published for over 8 hours."""
    await asyncio.sleep(3600)  # first check after 1h
    alerted = False
    while True:
        try:
            if _last_posted_at is not None:
                hours_since = (datetime.now(MSK) - _last_posted_at).total_seconds() / 3600
                if hours_since > 8 and not alerted:
                    for admin_id in ADMIN_IDS:
                        try:
                            await bot.send_message(
                                admin_id,
                                f"{pe('warn')} Последний пост был {hours_since:.0f}ч назад — бот не постит!",
                                parse_mode="HTML",
                            )
                        except Exception:
                            pass
                    alerted = True
                elif hours_since <= 8:
                    alerted = False
        except asyncio.CancelledError:
            break
        except Exception as ex:
            logger.error("Silence monitor: %s", ex)
        await asyncio.sleep(1800)  # check every 30 min


async def main():
    global bot, _last_posted_at
    if not BOT_TOKEN or not ADMIN_IDS:
        raise RuntimeError("Set BOT_TOKEN and ADMIN_IDS in .env before starting")
    await db.init_db()

    # Restore last posted time from DB so monitor works after restart
    try:
        _last_posted_at = await db.get_last_posted_at()
    except Exception:
        pass

    import telethon_client as tg
    try:
        await tg.get_client()
    except Exception as ex:
        logger.warning("Telethon init failed (will use Bot API fallback): %s", ex)

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    asyncio.create_task(scheduled_generate())
    asyncio.create_task(background_parser())
    asyncio.create_task(silence_monitor())

    logger.info("CryptoNews publisher started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
