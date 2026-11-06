"""Run this script once to generate a new Telethon SESSION_STRING."""
import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession

from config import API_ID, API_HASH

async def main():
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.start()
    session = client.session.save()
    print("\n=== NEW SESSION_STRING ===")
    print(session)
    print("==========================\n")
    await client.disconnect()

asyncio.run(main())
