from __future__ import annotations

import asyncio
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot import Ctx, setup
from app.config import Config
from app.db import Database
from app.hh import HhClient
from app.notifier import Notifier
from app.poller import run_poller


def _setup_logging(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    file = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    file.setFormatter(formatter)
    root.addHandler(stream)
    root.addHandler(file)


async def _run() -> None:
    cfg = Config.from_env()
    log_path = Path(cfg.database_path).parent / "logs" / "bot.log"
    _setup_logging(log_path)
    logging.getLogger("app").info("log file %s", log_path)
    db = Database(cfg.database_path)
    await db.connect()
    hh = HhClient(min_interval=cfg.hh_min_interval)
    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    notifier = Notifier(bot, db)
    wake = asyncio.Event()
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(setup(Ctx(db=db, hh=hh, notifier=notifier, cfg=cfg, wake=wake)))
    poller = asyncio.create_task(run_poller(db, hh, notifier, cfg, wake))
    try:
        await dispatcher.start_polling(bot)
    finally:
        poller.cancel()
        try:
            await poller
        except asyncio.CancelledError:
            pass
        await hh.aclose()
        await db.close()
        await bot.session.close()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
