import aiohttp
import json
import logging
import os
import random
import re
from config import GEMINI_API_KEY

GEMINI_HTTP_PROXY = os.getenv("GEMINI_HTTP_PROXY", "")
GEMINI_SOCKS5_PROXY = os.getenv("GEMINI_SOCKS5_PROXY", "")

logger = logging.getLogger(__name__)

def _fix_markdown(text: str) -> str:
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'__(.+?)__', r'\1', text)
    return text


def _add_spacing(text: str) -> str:
    text = re.sub(r'([^\n])(\n?▸)', r'\1\n\n▸', text)
    # Add newline before  only if NOT inside a tg-emoji tag
    text = re.sub(r'([^\n>])(<tg-emoji[^>]*>)', r'\1\n\n\2', text)
    text = re.sub(r'([^\n])(#\w)', r'\1\n\n\2', text)
    sentences = re.split(r'(?<=[.!?])\s+', text)
    if len(sentences) > 4:
        result = []
        for i, s in enumerate(sentences):
            result.append(s)
            if i > 0 and i < len(sentences) - 1 and (i + 1) % 2 == 0 and '\n' not in s:
                result.append('')
        text = '\n'.join(result)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text


def _apply_premium_emoji(text: str) -> str:
    return _fix_markdown(text)


_ANGLES = [
    "Расскажи как историю — завязка, кульминация, вывод.",
    "Акцент на цифрах и данных.",
    "Сравни с прошлым событием или традиционным рынком.",
    "Короткий пост — 3-4 ударных предложения.",
    "Покажи две стороны — кто выиграл, кто проиграл.",
    "Начни с итога, потом объясни предысторию.",
    "Хронология — что произошло по шагам.",
    "Подай как инсайд — стало известно, выяснилось.",
    "Покажи последствия — что будет дальше.",
]

SYSTEM_PROMPT = """Ты — редактор криптовалютного новостного канала.

Сегодняшняя дата: {today}

ПРАВИЛА:
- Только правда. Только из предоставленных новостей. Конкретные цифры.
- ТОЛЬКО свежие новости (за последние 24 часа). Старое — пропусти.
- ОДНА тема на пост. Не смешивай.
- ДЛИНА: 350–500 символов чистого текста (без HTML-тегов). Не короче, не длиннее.
- УНИКАЛЬНОСТЬ: ни одного предложения из источников дословно. Полностью перефразируй, добавь собственный контекст и оценку.

ЯЗЫК:
- Простой и понятный, но серьёзный. Как объясняешь другу который разбирается в теме.
- БЕЗ восклицательных призывов ("Невероятно!", "Это меняет всё!")
- Просто факты, контекст и короткий вывод. Спокойно и уверенно.

СТРУКТУРА:
1. Краткий заголовок без эмодзи
2. КРЮЧОК — 1-2 коротких фразы, сразу объясняющих ПОЧЕМУ это важно прямо сейчас
3. Суть в 1 предложение курсивом
4. Детали — 3-4 коротких предложения
5.  Вывод — новая мысль (НЕ повторяй детали) + короткий вопрос или своё мнение для читателей
6. Хештеги

ВАЖНО: поле "analysis" в JSON — это только вывод (п.5). Если вывод уже есть в теле (body) — оставь analysis пустой строкой "".

ФОРМАТИРОВАНИЕ:
- Ключевые цифры, суммы, названия монет/проектов — всегда жирным: $95 000, Bitcoin, +15%, $3 млрд
- Короткие абзацы, без воды, максимум конкретики
- Весь пост читается за 10–15 секунд

ХЭШТЕГИ (ровно 3–4 штуки):
- Первый — конкретная монета или проект: #BTC, #ETH, #Solana, #TON (не #крипто, не #crypto)
- Остальные — конкретная тема: #ETF, #SEC, #DeFi, #майнинг, #регуляция, #стейблкоин
- Только релевантные. Не добавляй общие теги ради объёма.

ПОДАЧА: {angle}

ФОРМАТ: Telegram HTML (, , <code>, <blockquote>, <a href="...">, ▸)
Не используй эмодзи. 
Между абзацами — пустая строка (\\n\\n).
Аналитику начинай с .

ГИПЕРССЫЛКИ (по желанию, не обязательно):
- Только если в новости есть реальный URL документа, отчёта, решения
- Формат: <a href="https://...">текст ссылки</a>
- Не придумывай ссылки. 0–2 на пост максимум.

СКРЫТЫЙ ТЕКСТ (примерно в половине постов):
- Тег: <tg-spoiler>текст</tg-spoiler> — читатель кликает чтобы увидеть
- Прячь: прогноз цены, неожиданный вывод, инсайдерскую деталь, "что будет дальше"
- Примеры:
   <tg-spoiler>Следующая цель BTC — $118 000 до конца квартала.</tg-spoiler>
  <tg-spoiler>Реальная причина падения — кит слил 8 000 BTC за 2 часа.</tg-spoiler>
- Ровно 1 спойлер на пост. Обычно внутри или вместо вывода.

{feedback}

ПОЛЕ image_search — описание картинки по теме (английские слова):
- Отражает суть новости конкретно: биржа, майнинг-ферма, зал суда, монета крупным планом
- Без людей, без рук, без лиц
- null если картинка не нужна

JSON ответ (без markdown обёртки):
{{"title":"эмодзи заголовок","body":"текст с HTML и \\n\\n","analysis":" вывод","hashtags":"#BTC #ETF #регуляция","image_search":"2-4 keywords english or null","importance":8,"poll_question":"вопрос или null","poll_options":["Вариант1","Вариант2"] или null}}"""


GEMINI_PROXY = "https://generativelanguage.googleapis.com"
GEMINI_TEXT_MODELS = [
    f"{GEMINI_PROXY}/v1beta/models/gemini-2.5-flash:generateContent",
    f"{GEMINI_PROXY}/v1beta/models/gemini-2.5-flash-lite:generateContent",
    f"{GEMINI_PROXY}/v1beta/models/gemini-flash-latest:generateContent",
]
GEMINI_TEXT_URL = GEMINI_TEXT_MODELS[0]
GEMINI_IMAGE_URL = f"{GEMINI_PROXY}/v1beta/models/gemini-2.0-flash-exp-image-generation:generateContent"


async def generate_post(news_items: list[dict], previous_titles: list[str] = None, include_poll: bool = False) -> dict | None:
    if not GEMINI_API_KEY:
        logger.error("No GEMINI_API_KEY")
        return None

    shuffled = list(news_items)
    random.shuffle(shuffled)

    digest = ""
    for i, item in enumerate(shuffled[:15], 1):
        text = item["text"][:300]
        source = item.get("channel", "?")
        digest += f"\n[{i}] {source}: {text}\n"

    extra = ""
    if previous_titles:
        clean = [re.sub(r'<[^>]+>', '', t).strip()[:60] for t in previous_titles[-20:] if t.strip()]
        extra = (
            f"\n\nУЖЕ ОПУБЛИКОВАНО (ЗАПРЕЩЕНО повторять эту тему или монету):\n"
            + "\n".join(f"- {t}" for t in clean if t)
            + "\n→ Выбери ДРУГУЮ тему, другую монету, другое событие. Не похожее ни на одно из списка выше."
        )

    angle = random.choice(_ANGLES)

    feedback_text = ""
    try:
        from database import get_setting
        fb = await get_setting("ai_feedback")
        if fb:
            feedback_text = f"ФИДБЕК: {fb}"
    except Exception:
        pass

    from datetime import datetime, timezone, timedelta
    today = datetime.now(timezone(timedelta(hours=3))).strftime("%d.%m.%Y")
    poll_instruction = ""
    if include_poll:
        poll_instruction = "\n\nДОБАВЬ ОПРОС: придумай интересный вопрос по теме поста с 2-3 вариантами ответа."
    else:
        poll_instruction = "\n\nОпрос НЕ нужен — поставь poll_question: null, poll_options: null."

    prompt = SYSTEM_PROMPT.format(angle=angle, feedback=feedback_text, today=today)

    try:
        payload = {
            "contents": [
                {"role": "user", "parts": [{"text": f"{prompt}{poll_instruction}\n\nНовости:{extra}\n{digest}"}]}
            ],
            "generationConfig": {
                "temperature": 0.9,
                "maxOutputTokens": 4000,
                "responseMimeType": "application/json",
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }

        import asyncio
        data = None
        if GEMINI_SOCKS5_PROXY:
            from aiohttp_socks import ProxyConnector
            _connector = ProxyConnector.from_url(GEMINI_SOCKS5_PROXY)
        else:
            _connector = None
        async with aiohttp.ClientSession(connector=_connector) as session:
            for model_url in GEMINI_TEXT_MODELS:
                for attempt in range(3):
                    async with session.post(
                        f"{model_url}?key={GEMINI_API_KEY}",
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=60),
                        proxy=GEMINI_HTTP_PROXY or None,
                    ) as resp:
                        if resp.status in (429, 503, 500):
                            wait = 5 + attempt * 8
                            logger.warning("Gemini %d, waiting %ds (model %s attempt %d/3)",
                                           resp.status, wait, model_url.split("/")[-1].split(":")[0], attempt + 1)
                            await asyncio.sleep(wait)
                            continue
                        if resp.status != 200:
                            error = await resp.text()
                            logger.error("Gemini %d: %s", resp.status, error[:200])
                            break
                        data = await resp.json()
                        break
                if data:
                    break
        if not data:
            return None

        raw = data["candidates"][0]["content"]["parts"][0]["text"]

        raw = re.sub(r'^```(?:json)?\s*', '', raw.strip(), flags=re.IGNORECASE)
        raw = re.sub(r'\s*```$', '', raw.strip())

        json_match = re.search(r'\{[\s\S]*\}', raw)
        if not json_match:
            logger.error("No JSON in response: %s", raw[:200])
            return None

        json_str = json_match.group()

        result = None
        try:
            result = json.loads(json_str)
        except json.JSONDecodeError:
            fixed = re.sub(
                r'("(?:[^"\\]|\\.)*")',
                lambda m: m.group(0).replace('\n', '\\n').replace('\r', '').replace('\t', '\\t'),
                json_str,
            )
            try:
                result = json.loads(fixed)
            except json.JSONDecodeError as ex2:
                logger.error("JSON error after fix: %s | raw: %s", ex2, json_str[:300])
                return None

        for key in ("title", "body", "analysis"):
            if result.get(key):
                result[key] = _apply_premium_emoji(result[key])
        if result.get("body"):
            result["body"] = _add_spacing(result["body"])
        if result.get("analysis") and result.get("body"):
            def _norm(t):
                t = re.sub(r'<[^>]+>', '', t)  
                t = re.sub(r'\s+', ' ', t)    
                return t.strip().lower()
            norm_analysis = _norm(result["analysis"])
            norm_body = _norm(result["body"])
            if norm_analysis and (
                norm_analysis in norm_body
                or norm_body.endswith(norm_analysis[-60:])  
            ):
                result["analysis"] = ""
            elif norm_analysis:
                last_para = norm_body.split('\n\n')[-1] if '\n\n' in norm_body else norm_body[-200:]
                words_a = set(norm_analysis.split())
                words_p = set(last_para.split())
                if words_a and len(words_a & words_p) / len(words_a) > 0.75:
                    result["analysis"] = ""

        logger.info("Generated: %s", re.sub(r'<[^>]+>', '', result.get("title", "?"))[:60])
        return result

    except Exception as ex:
        logger.error("Gemini error: %s", ex)
        return None


async def generate_image(prompt: str) -> bytes | None:
    """Generate image using Gemini Imagen via NL proxy."""
    if not GEMINI_API_KEY:
        return None

    try:
        payload = {
            "contents": [
                {"role": "user", "parts": [{"text": f"Generate this image: {prompt}"}]}
            ],
            "generationConfig": {
                "responseModalities": ["TEXT", "IMAGE"],
            },
        }

        if GEMINI_SOCKS5_PROXY:
            from aiohttp_socks import ProxyConnector
            _img_connector = ProxyConnector.from_url(GEMINI_SOCKS5_PROXY)
        else:
            _img_connector = None
        async with aiohttp.ClientSession(connector=_img_connector) as session:
            async with session.post(
                f"{GEMINI_IMAGE_URL}?key={GEMINI_API_KEY}",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=45),
                proxy=GEMINI_HTTP_PROXY or None,
            ) as resp:
                if resp.status != 200:
                    logger.warning("Gemini image %d", resp.status)
                    return None
                data = await resp.json()

        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        for part in parts:
            if "inlineData" in part:
                import base64
                img_bytes = base64.b64decode(part["inlineData"]["data"])
                logger.info("Gemini image: %dKB", len(img_bytes) // 1024)
                return img_bytes

        return None
    except Exception as ex:
        logger.warning("Gemini image error: %s", ex)
        return None
