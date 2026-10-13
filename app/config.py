from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int, minimum: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = int(raw)
    if value < minimum:
        raise SystemExit(f"{name} must be >= {minimum}")
    return value


def _float(name: str, default: float, minimum: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = float(raw)
    if value < minimum:
        raise SystemExit(f"{name} must be >= {minimum}")
    return value


@dataclass(frozen=True)
class Config:
    bot_token: str
    database_path: str
    poll_interval: int
    hh_min_interval: float
    lookback_minutes: int
    catchup_minutes: int

    @classmethod
    def from_env(cls) -> Config:
        token = os.environ.get("BOT_TOKEN", "").strip()
        if not token:
            raise SystemExit("BOT_TOKEN is empty. Copy .env.example to .env and put the token from @BotFather.")
        return cls(
            bot_token=token,
            database_path=os.environ.get("DATABASE_PATH", "/data/bot.sqlite3"),
            poll_interval=_int("POLL_INTERVAL", 40, 15),
            hh_min_interval=_float("HH_MIN_INTERVAL", 1.0, 0.4),
            lookback_minutes=_int("LOOKBACK_MINUTES", 5, 1),
            catchup_minutes=_int("CATCHUP_MINUTES", 24 * 60, 5),
        )
