import os
from dotenv import load_dotenv

load_dotenv()

# Telegram Bot
BOT_TOKEN = os.getenv("BOT_TOKEN", '')
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", '').split(",") if x.strip().isdigit()]
CHANNEL_ID = os.getenv("CHANNEL_ID", "")
PREFERRED_SENDER_ID = int(os.getenv("PREFERRED_SENDER_ID", '0'))

API_ID = int(os.getenv("API_ID", '0'))
API_HASH = os.getenv("API_HASH", '')
SESSION_STRING = os.getenv("SESSION_STRING", "")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY", '')

DATABASE_URL = os.getenv("DATABASE_URL", '')

SOURCE_CHANNELS = [
    # Топ новостники (рус)
    "https://t.me/ForkLog",
    "https://t.me/incrypted",
    "https://t.me/CoinPostNews",
    "https://t.me/BeInCrypto_ru",
    "https://t.me/RBCCrypto",
    "https://t.me/coinkeeper",
    "https://t.me/CryptoBoomer",
    # Новостники (eng)
    "https://t.me/tu_crypto_news_en",
    "https://t.me/tu_crypto_ideas",
    "https://t.me/bitcoin",
    "https://t.me/crypto",
    # Аналитика и ончейн
    "https://t.me/whale_alert_io",
    "https://t.me/lookonchain",
    "https://t.me/Defiscamcheck",
    # Доп каналы
    "https://t.me/ton_blockchain",
    "https://t.me/CryptoInvestorsClub",
    "https://t.me/blocknewsru",
]

DEFAULT_POST_HOURS = [7, 13, 16, 19, 21]

PARSE_LIMIT = 20
