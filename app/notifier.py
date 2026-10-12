from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import LinkPreviewOptions

from app.catalog import PUSH_BATCH
from app.db import Database
from app.format import vacancy_html
from app.keyboards import queue_kb, vacancy_kb

log = logging.getLogger("notifier")

_PREVIEW_OFF = LinkPreviewOptions(is_disabled=True)


class Notifier:
    def __init__(self, bot: Bot, db: Database):
        self.bot = bot
        self.db = db
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def flush(self) -> None:
        for user_id, chat_id in await self.db.users_with_queue():
            await self.push_user(user_id, chat_id, PUSH_BATCH)

    async def push_user(self, user_id: int, chat_id: int, limit: int) -> int:
        async with self._locks[user_id]:
            if not await self.db.notify_enabled(user_id):
                return 0
            batch = await self.db.take_queued(user_id, limit)
            if not batch:
                return 0
            # Chat reads top to bottom, so the newest vacancy ends up closest to the input.
            ordered = list(reversed(batch))
            sent = 0
            for vacancy in ordered:
                try:
                    await self._send(chat_id, vacancy_html(vacancy), vacancy_kb(vacancy))
                except TelegramForbiddenError:
                    log.info("user %s blocked the bot, pause notifications", user_id)
                    await self.db.set_notify(user_id, False)
                    break
                except TelegramBadRequest as exc:
                    log.warning("telegram rejected vacancy %s: %s", vacancy.id, exc)
                    await self.db.mark_pushed(user_id, vacancy.id)
                    continue
                await self.db.mark_pushed(user_id, vacancy.id)
                sent += 1
                await asyncio.sleep(0.35)
            left = await self.db.queue_count(user_id)
            if sent and left:
                await self._send(
                    chat_id,
                    f"В очереди ещё {left}. Сам пришлю до {PUSH_BATCH} следующим заходом.",
                    queue_kb(left),
                )
            if sent:
                log.info("push user=%s sent=%s left=%s", user_id, sent, left)
            return sent

    async def _send(self, chat_id: int, text: str, markup) -> None:
        last: TelegramRetryAfter | None = None
        for _ in range(3):
            try:
                await self.bot.send_message(
                    chat_id,
                    text,
                    reply_markup=markup,
                    link_preview_options=_PREVIEW_OFF,
                )
                return
            except TelegramRetryAfter as exc:
                last = exc
                await asyncio.sleep(exc.retry_after + 0.5)
        if last is not None:
            raise last
