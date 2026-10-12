from __future__ import annotations

import asyncio
import logging
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from app.catalog import MAX_EXCLUSIONS, MAX_SEARCHES
from app.models import Search, Vacancy

log = logging.getLogger("db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    username TEXT,
    notify_enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    text_query TEXT NOT NULL,
    area_id TEXT,
    area_name TEXT,
    work_format TEXT,
    experience TEXT NOT NULL DEFAULT '',
    salary_from INTEGER,
    created_at TEXT NOT NULL,
    last_polled_at TEXT,
    UNIQUE(user_id, name)
);

CREATE TABLE IF NOT EXISTS exclusions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    word TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, word)
);

CREATE TABLE IF NOT EXISTS vacancies (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    employer TEXT,
    area TEXT,
    salary_from INTEGER,
    salary_to INTEGER,
    salary_currency TEXT,
    salary_gross INTEGER,
    salary_mode TEXT,
    url TEXT NOT NULL,
    published_at TEXT NOT NULL,
    experience TEXT,
    work_formats TEXT,
    internship INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deliveries (
    user_id INTEGER NOT NULL,
    vacancy_id TEXT NOT NULL,
    status TEXT NOT NULL,
    published_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    pushed_at TEXT,
    PRIMARY KEY (user_id, vacancy_id)
);

CREATE TABLE IF NOT EXISTS delivery_searches (
    user_id INTEGER NOT NULL,
    vacancy_id TEXT NOT NULL,
    search_id INTEGER NOT NULL,
    PRIMARY KEY (user_id, vacancy_id, search_id)
);

CREATE INDEX IF NOT EXISTS idx_deliveries_queue
    ON deliveries(user_id, status, published_at);
CREATE INDEX IF NOT EXISTS idx_searches_user ON searches(user_id);
"""


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _iso(moment: datetime | None = None) -> str:
    current = moment or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def _now() -> str:
    return _iso()


def _search(row: aiosqlite.Row) -> Search:
    keys = row.keys()
    return Search(
        id=row["id"],
        user_id=row["user_id"],
        name=row["name"],
        text_query=row["text_query"],
        area_id=row["area_id"],
        area_name=row["area_name"],
        work_format=row["work_format"],
        experience=row["experience"] or "",
        salary_from=row["salary_from"],
        created_at=_dt(row["created_at"]) or datetime.now(timezone.utc),
        last_polled_at=_dt(row["last_polled_at"]),
        chat_id=row["chat_id"] if "chat_id" in keys else None,
        notify_enabled=bool(row["notify_enabled"]) if "notify_enabled" in keys else True,
    )


def _vacancy(row: aiosqlite.Row) -> Vacancy:
    gross = row["salary_gross"]
    formats = [part for part in (row["work_formats"] or "").split(",") if part]
    return Vacancy(
        id=row["id"],
        name=row["name"],
        employer=row["employer"] or "Работодатель скрыт",
        area=row["area"] or "—",
        salary_from=row["salary_from"],
        salary_to=row["salary_to"],
        salary_currency=row["salary_currency"],
        salary_gross=None if gross is None else bool(gross),
        salary_mode=row["salary_mode"],
        url=row["url"],
        published_at=_dt(row["published_at"]) or datetime.now(timezone.utc),
        experience=row["experience"],
        work_formats=formats,
        internship=bool(row["internship"]),
    )


def resolve_database(path: str) -> str:
    """Open path, or a sibling file if the WAL sidecars on a Windows mount are locked.

    Docker Desktop cannot delete ``bot.sqlite3-wal`` / ``-shm`` while the host holds
    them, and SQLite then fails with disk I/O error. The consolidated file is
    ``hh.sqlite3`` next to the original. ``db.path`` remembers that choice.
    """
    source = Path(path)
    source.parent.mkdir(parents=True, exist_ok=True)
    marker = source.with_name("db.path")
    if marker.exists():
        chosen = Path(marker.read_text(encoding="utf-8").strip())
        if _open_delete(chosen):
            log.info("sqlite %s", chosen)
            return str(chosen)
        log.warning("marked database is not usable: %s", chosen)
    if _open_delete(source):
        _remember(marker, source)
        log.info("sqlite %s", source)
        return str(source)
    if source.exists() or Path(str(source) + "-wal").exists():
        log.warning("cannot use %s, migrating off locked journal files", source)
        chosen = Path(_consolidate(source))
    else:
        chosen = source
        _switch_to_delete(str(chosen))
    if not _open_delete(chosen):
        raise sqlite3.OperationalError(f"sqlite database is not usable: {chosen}")
    _remember(marker, chosen)
    log.info("sqlite %s", chosen)
    return str(chosen)


def _switch_to_delete(path: str) -> None:
    conn = sqlite3.connect(path, timeout=10)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError as exc:
            log.warning("wal checkpoint failed for %s: %s", path, exc)
        row = conn.execute("PRAGMA journal_mode=DELETE").fetchone()
        mode = str(row[0]).lower() if row else ""
        conn.commit()
        if mode != "delete":
            raise sqlite3.OperationalError(f"journal_mode stayed {mode or 'unset'}")
        log.info("sqlite journal_mode=%s path=%s", mode, path)
    finally:
        conn.close()


def _open_delete(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        _switch_to_delete(str(path))
    except sqlite3.OperationalError as exc:
        log.warning("sqlite open failed for %s: %s", path, exc)
        return False
    _drop_sidecars(str(path))
    return True


def _consolidate(source: Path) -> str:
    """Checkpoint a copy on the container disk and store it as hh.sqlite3."""
    target = source.with_name("hh.sqlite3")
    with tempfile.TemporaryDirectory() as tmp:
        copied = Path(tmp) / "db.sqlite3"
        shutil.copy2(source, copied)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(source) + suffix)
            if not sidecar.exists():
                continue
            try:
                shutil.copy2(sidecar, Path(str(copied) + suffix))
            except OSError as exc:
                log.warning("could not copy %s: %s", sidecar, exc)
        _switch_to_delete(str(copied))
        shutil.copy2(copied, target)
    return str(target)


def _remember(marker: Path, chosen: Path) -> None:
    marker.write_text(str(chosen), encoding="utf-8")


def _drop_sidecars(path: str) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = Path(path + suffix)
        if not sidecar.exists():
            continue
        try:
            sidecar.unlink()
        except OSError as exc:
            log.warning("could not remove %s: %s", sidecar, exc)


class Database:
    def __init__(self, path: str):
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # WAL needs a shared-memory sidecar. Docker Desktop bind-mounts on Windows
        # fail that with "disk I/O error", and uncheckpointed writes sit in -wal
        # so the main file looks empty from the host. Rollback journal is one file.
        self.path = await asyncio.to_thread(resolve_database, self.path)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.execute("PRAGMA journal_mode=DELETE")
        await self._conn.execute("PRAGMA synchronous=FULL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        async with self._lock:
            if self._conn is not None:
                await self._conn.close()
                self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("database is not connected")
        return self._conn

    async def upsert_user(self, telegram_id: int, chat_id: int, username: str | None) -> None:
        async with self._lock:
            await self.conn.execute(
                """
                INSERT INTO users (telegram_id, chat_id, username, notify_enabled, created_at)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    chat_id = excluded.chat_id,
                    username = excluded.username
                """,
                (telegram_id, chat_id, username, _now()),
            )
            await self.conn.commit()

    async def set_notify(self, telegram_id: int, enabled: bool) -> None:
        async with self._lock:
            await self.conn.execute(
                "UPDATE users SET notify_enabled = ? WHERE telegram_id = ?",
                (1 if enabled else 0, telegram_id),
            )
            await self.conn.commit()

    async def notify_enabled(self, telegram_id: int) -> bool:
        async with self._lock:
            cursor = await self.conn.execute(
                "SELECT notify_enabled FROM users WHERE telegram_id = ?",
                (telegram_id,),
            )
            row = await cursor.fetchone()
            return True if row is None else bool(row["notify_enabled"])

    async def add_search(
        self,
        user_id: int,
        name: str,
        text_query: str,
        area_id: str | None,
        area_name: str | None,
        work_format: str | None,
        experience: list[str],
        salary_from: int | None,
    ) -> str:
        async with self._lock:
            cursor = await self.conn.execute(
                "SELECT COUNT(*) AS n FROM searches WHERE user_id = ?",
                (user_id,),
            )
            count = (await cursor.fetchone())["n"]
            if count >= MAX_SEARCHES:
                return "limit"
            try:
                await self.conn.execute(
                    """
                    INSERT INTO searches (
                        user_id, name, text_query, area_id, area_name,
                        work_format, experience, salary_from, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        name,
                        text_query,
                        area_id,
                        area_name,
                        work_format,
                        ",".join(experience),
                        salary_from,
                        _now(),
                    ),
                )
                await self.conn.commit()
            except sqlite3.IntegrityError:
                await self.conn.rollback()
                return "duplicate"
            return "ok"

    async def list_searches(self, user_id: int) -> list[Search]:
        async with self._lock:
            cursor = await self.conn.execute(
                "SELECT * FROM searches WHERE user_id = ? ORDER BY id",
                (user_id,),
            )
            return [_search(row) for row in await cursor.fetchall()]

    async def get_search(self, user_id: int, search_id: int) -> Search | None:
        async with self._lock:
            cursor = await self.conn.execute(
                "SELECT * FROM searches WHERE user_id = ? AND id = ?",
                (user_id, search_id),
            )
            row = await cursor.fetchone()
            return _search(row) if row else None

    async def searches_for_poll(self) -> list[Search]:
        async with self._lock:
            cursor = await self.conn.execute(
                """
                SELECT s.*, u.chat_id, u.notify_enabled
                FROM searches s
                JOIN users u ON u.telegram_id = s.user_id
                ORDER BY s.id
                """
            )
            return [_search(row) for row in await cursor.fetchall()]

    async def touch_polled(self, search_ids: list[int], moment: datetime) -> None:
        async with self._lock:
            if not search_ids:
                return
            stamp = _iso(moment)
            await self.conn.executemany(
                "UPDATE searches SET last_polled_at = ? WHERE id = ?",
                [(stamp, search_id) for search_id in search_ids],
            )
            await self.conn.commit()

    async def delete_search(self, user_id: int, search_id: int) -> bool:
        async with self._lock:
            cursor = await self.conn.execute(
                "SELECT 1 FROM searches WHERE id = ? AND user_id = ?",
                (search_id, user_id),
            )
            if await cursor.fetchone() is None:
                return False
            await self._drop_search_links(user_id, [search_id])
            await self.conn.execute(
                "DELETE FROM searches WHERE id = ? AND user_id = ?",
                (search_id, user_id),
            )
            await self.conn.commit()
            return True

    async def delete_all_searches(self, user_id: int) -> int:
        async with self._lock:
            cursor = await self.conn.execute(
                "SELECT id FROM searches WHERE user_id = ?",
                (user_id,),
            )
            ids = [row["id"] for row in await cursor.fetchall()]
            if not ids:
                return 0
            await self._drop_search_links(user_id, ids)
            await self.conn.execute("DELETE FROM searches WHERE user_id = ?", (user_id,))
            await self.conn.commit()
            return len(ids)

    async def _drop_search_links(self, user_id: int, search_ids: list[int]) -> None:
        marks = ",".join("?" for _ in search_ids)
        await self.conn.execute(
            f"DELETE FROM delivery_searches WHERE user_id = ? AND search_id IN ({marks})",
            (user_id, *search_ids),
        )
        await self.conn.execute(
            """
            DELETE FROM deliveries
            WHERE user_id = ? AND status = 'queued'
              AND NOT EXISTS (
                  SELECT 1 FROM delivery_searches ds
                  WHERE ds.user_id = deliveries.user_id
                    AND ds.vacancy_id = deliveries.vacancy_id
              )
            """,
            (user_id,),
        )

    async def add_exclusions(self, user_id: int, words: list[str]) -> int:
        async with self._lock:
            cursor = await self.conn.execute(
                "SELECT word FROM exclusions WHERE user_id = ?",
                (user_id,),
            )
            existing = {row["word"].casefold() for row in await cursor.fetchall()}
            added = 0
            for word in words:
                if word.casefold() in existing:
                    continue
                if len(existing) >= MAX_EXCLUSIONS:
                    break
                await self.conn.execute(
                    "INSERT INTO exclusions (user_id, word, created_at) VALUES (?, ?, ?)",
                    (user_id, word, _now()),
                )
                existing.add(word.casefold())
                added += 1
            await self.conn.commit()
            return added

    async def list_exclusions(self, user_id: int) -> list[tuple[int, str]]:
        async with self._lock:
            cursor = await self.conn.execute(
                "SELECT id, word FROM exclusions WHERE user_id = ? ORDER BY id",
                (user_id,),
            )
            return [(row["id"], row["word"]) for row in await cursor.fetchall()]

    async def exclusion_words(self) -> dict[int, list[str]]:
        async with self._lock:
            cursor = await self.conn.execute("SELECT user_id, word FROM exclusions ORDER BY id")
            grouped: dict[int, list[str]] = {}
            for row in await cursor.fetchall():
                grouped.setdefault(row["user_id"], []).append(row["word"])
            return grouped

    async def delete_exclusion(self, user_id: int, exclusion_id: int) -> bool:
        async with self._lock:
            cursor = await self.conn.execute(
                "DELETE FROM exclusions WHERE id = ? AND user_id = ?",
                (exclusion_id, user_id),
            )
            await self.conn.commit()
            return cursor.rowcount > 0

    async def clear_exclusions(self, user_id: int) -> int:
        async with self._lock:
            cursor = await self.conn.execute(
                "DELETE FROM exclusions WHERE user_id = ?",
                (user_id,),
            )
            await self.conn.commit()
            return cursor.rowcount

    async def enqueue(self, user_id: int, search_id: int, vacancy: Vacancy) -> bool:
        async with self._lock:
            """Remember a vacancy for the user. True only the first time it is queued."""
            stamp = _iso(vacancy.published_at)
            now = _now()
            gross = None if vacancy.salary_gross is None else int(vacancy.salary_gross)
            await self.conn.execute(
                """
                INSERT INTO vacancies (
                    id, name, employer, area, salary_from, salary_to, salary_currency,
                    salary_gross, salary_mode, url, published_at, experience,
                    work_formats, internship, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    employer = excluded.employer,
                    area = excluded.area,
                    salary_from = excluded.salary_from,
                    salary_to = excluded.salary_to,
                    salary_currency = excluded.salary_currency,
                    salary_gross = excluded.salary_gross,
                    salary_mode = excluded.salary_mode,
                    url = excluded.url,
                    published_at = excluded.published_at,
                    experience = excluded.experience,
                    work_formats = excluded.work_formats,
                    internship = excluded.internship,
                    updated_at = excluded.updated_at
                """,
                (
                    vacancy.id,
                    vacancy.name,
                    vacancy.employer,
                    vacancy.area,
                    vacancy.salary_from,
                    vacancy.salary_to,
                    vacancy.salary_currency,
                    gross,
                    vacancy.salary_mode,
                    vacancy.url,
                    stamp,
                    vacancy.experience,
                    ",".join(vacancy.work_formats),
                    int(vacancy.internship),
                    now,
                ),
            )
            cursor = await self.conn.execute(
                "SELECT status FROM deliveries WHERE user_id = ? AND vacancy_id = ?",
                (user_id, vacancy.id),
            )
            is_new = await cursor.fetchone() is None
            if is_new:
                await self.conn.execute(
                    """
                    INSERT INTO deliveries (user_id, vacancy_id, status, published_at, created_at)
                    VALUES (?, ?, 'queued', ?, ?)
                    """,
                    (user_id, vacancy.id, stamp, now),
                )
            await self.conn.execute(
                """
                INSERT OR IGNORE INTO delivery_searches (user_id, vacancy_id, search_id)
                VALUES (?, ?, ?)
                """,
                (user_id, vacancy.id, search_id),
            )
            await self.conn.commit()
            return is_new

    async def take_queued(self, user_id: int, limit: int) -> list[Vacancy]:
        async with self._lock:
            cursor = await self.conn.execute(
                """
                SELECT v.*
                FROM deliveries d
                JOIN vacancies v ON v.id = d.vacancy_id
                WHERE d.user_id = ? AND d.status = 'queued'
                ORDER BY d.published_at DESC
                LIMIT ?
                """,
                (user_id, limit),
            )
            vacancies = [_vacancy(row) for row in await cursor.fetchall()]
            if not vacancies:
                return []
            marks = ",".join("?" for _ in vacancies)
            cursor = await self.conn.execute(
                f"""
                SELECT ds.vacancy_id, ds.search_id, s.name
                FROM delivery_searches ds
                JOIN searches s ON s.id = ds.search_id
                WHERE ds.user_id = ? AND ds.vacancy_id IN ({marks})
                ORDER BY s.id
                """,
                (user_id, *(vacancy.id for vacancy in vacancies)),
            )
            names: dict[str, list[str]] = {}
            ids: dict[str, list[int]] = {}
            for row in await cursor.fetchall():
                names.setdefault(row["vacancy_id"], []).append(row["name"])
                ids.setdefault(row["vacancy_id"], []).append(row["search_id"])
            for vacancy in vacancies:
                vacancy.search_names = names.get(vacancy.id, [])
                vacancy.search_ids = ids.get(vacancy.id, [])
            return vacancies

    async def mark_pushed(self, user_id: int, vacancy_id: str) -> None:
        async with self._lock:
            await self.conn.execute(
                """
                UPDATE deliveries
                SET status = 'pushed', pushed_at = ?
                WHERE user_id = ? AND vacancy_id = ? AND status = 'queued'
                """,
                (_now(), user_id, vacancy_id),
            )
            await self.conn.commit()

    async def skip_queued(self, user_id: int) -> int:
        async with self._lock:
            cursor = await self.conn.execute(
                "UPDATE deliveries SET status = 'skipped' WHERE user_id = ? AND status = 'queued'",
                (user_id,),
            )
            await self.conn.commit()
            return cursor.rowcount

    async def queue_count(self, user_id: int) -> int:
        async with self._lock:
            cursor = await self.conn.execute(
                "SELECT COUNT(*) AS n FROM deliveries WHERE user_id = ? AND status = 'queued'",
                (user_id,),
            )
            return int((await cursor.fetchone())["n"])

    async def users_with_queue(self) -> list[tuple[int, int]]:
        async with self._lock:
            cursor = await self.conn.execute(
                """
                SELECT DISTINCT d.user_id, u.chat_id
                FROM deliveries d
                JOIN users u ON u.telegram_id = d.user_id
                WHERE d.status = 'queued' AND u.notify_enabled = 1
                """
            )
            return [(row["user_id"], row["chat_id"]) for row in await cursor.fetchall()]
