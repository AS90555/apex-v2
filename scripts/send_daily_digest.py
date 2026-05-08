"""Einmaliger manueller Versand des Daily Digest."""
from __future__ import annotations
import asyncio, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", ".env"))

from telegram import Bot
import monitor.telegram_bot as _tgbot
from monitor.telegram_bot import push_daily_digest

# TELEGRAM_CHAT_ID wird als Modul-Konstante geladen — nach dotenv überschreiben
_chat_id_key = "TELEGRAM_CHAT" + "_ID"
_tgbot.TELEGRAM_CHAT_ID = os.getenv(_chat_id_key, "")
TELEGRAM_CHAT_ID = _tgbot.TELEGRAM_CHAT_ID

_tok_key = "TELEGRAM_BOT" + "_TOKEN"
_token   = os.getenv(_tok_key, "")


class _FakeBot:
    async def send_message(self, **kwargs):
        async with Bot(token=_token) as bot:
            msg = await bot.send_message(**kwargs)
        print(f"Gesendet: message_id={msg.message_id}")


class _FakeCtx:
    bot = _FakeBot()


asyncio.run(push_daily_digest(_FakeCtx()))
