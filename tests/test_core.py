import ast
import asyncio
import hashlib
import subprocess
import sys
import os
from pathlib import Path

import bot
import rss_parser


def test_rss_id_is_stable_across_python_processes():
    results = [subprocess.check_output([sys.executable, '-c', 'from rss_parser import news_id; print(news_id("https://example.com/article"))'], stdin=subprocess.DEVNULL, stderr=subprocess.PIPE, env={**os.environ, 'PYTHONHASHSEED': seed}, cwd=Path(rss_parser.__file__).parent, text=True).strip() for seed in ['1', '222']]
    assert results[0] == results[1]
    assert rss_parser.news_id('https://example.com/other') != int(results[0])
    assert 0 <= int(results[0]) < 2**48


def test_non_admin_callback_is_rejected():
    from types import SimpleNamespace
    from aiogram.types import CallbackQuery, User
    bot.ADMIN_IDS[:] = [123]
    async def check(uid):
        event = CallbackQuery(id='demo', from_user=User(id=uid, is_bot=False, first_name='Test'), chat_instance='demo', data='cn_toggle_auto')
        return await bot.router.callback_query.check_root_filters(event)
    assert asyncio.run(check(999))[0] is False
    assert asyncio.run(check(123))[0] is True


def test_title_duplicate_heuristic():
    assert bot._is_duplicate_title('Bitcoin приток капитала', ['Bitcoin рекордный приток'])
    assert not bot._is_duplicate_title('Solana обновление сети', ['Bitcoin ETF новый рекорд'])


def test_channel_link_formats():
    assert bot._channel_post_link('@demo', 42) == 'https://t.me/demo/42'
    assert bot._channel_post_link('-10012345', 42) == 'https://t.me/c/12345/42'
    assert bot._channel_post_link('', 0) == ''


def test_rss_text_and_media():
    assert rss_parser._clean('<p>Hello</p>  world') == 'Hello world'
    assert rss_parser._entry_image({'media_content':[{'url':'https://example.com/video.mp4','type':'video/mp4'}]}) == 'https://example.com/video.mp4'
    assert rss_parser._entry_text({'title':'Title','summary':'Title. Details'}) == 'Title. Details'


def test_plain_interface_and_truncation():
    assert bot.pe('rocket') == ''
    assert len(bot._safe_truncate('a' * 2000, 1000)) <= 1000
    keyboard = bot._main_kb().model_dump()
    assert keyboard['inline_keyboard'][0][0]['text'] == 'Сгенерировать пост'
