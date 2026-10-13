from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.config import Config
from app.db import Database
from app.hh import HhClient, HhError, HhRateLimit, matches_area
from app.models import QuerySpec, Search
from app.notifier import Notifier
from app.query import apply_exclusions

log = logging.getLogger("poller")

_PAGE = 20
_MAX_PAGES = 3


def cutoff_for(search: Search, now: datetime, cfg: Config) -> datetime:
    """How old a vacancy can be and still be new to this search.

    hh often shows a vacancy in search later than publicationTime, and the bot
    may have been down. Id dedup stops a resend, so the window stays wide:
    not earlier than the search itself, and not older than catchup.
    """
    since_created = search.created_at - timedelta(minutes=cfg.lookback_minutes)
    earliest = now - timedelta(minutes=cfg.catchup_minutes)
    return max(since_created, earliest)


async def run_poller(
    db: Database,
    hh: HhClient,
    notifier: Notifier,
    cfg: Config,
    wake: asyncio.Event,
) -> None:
    await asyncio.sleep(2)
    while True:
        started = asyncio.get_running_loop().time()
        try:
            await poll_once(db, hh, notifier, cfg)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("poll failed")
        elapsed = asyncio.get_running_loop().time() - started
        timeout = max(0.0, cfg.poll_interval - elapsed)
        try:
            await asyncio.wait_for(wake.wait(), timeout)
        except TimeoutError:
            pass
        else:
            wake.clear()


async def poll_once(db: Database, hh: HhClient, notifier: Notifier, cfg: Config) -> None:
    searches = await db.searches_for_poll()
    if not searches:
        return
    exclusions = await db.exclusion_words()
    now = datetime.now(timezone.utc)
    groups: dict[str, list[Search]] = {}
    specs: dict[str, QuerySpec] = {}
    for search in searches:
        spec = QuerySpec(
            text=apply_exclusions(search.text_query, exclusions.get(search.user_id, [])),
            area_id=search.area_id,
            work_format=search.work_format,
            experience=tuple(search.experience_list()),
            salary_from=search.salary_from,
        )
        groups.setdefault(spec.cache_key(0, 0), []).append(search)
        specs[spec.cache_key(0, 0)] = spec

    fresh = 0
    for key, group in groups.items():
        spec = specs[key]
        earliest = min(cutoff_for(search, now, cfg) for search in group)
        try:
            vacancies = await _collect(hh, spec, earliest)
        except HhRateLimit:
            log.warning("stop poll cycle, hh asked to slow down")
            break
        except HhError as exc:
            log.warning("search failed: %s", exc)
            continue
        for search in group:
            border = cutoff_for(search, now, cfg)
            for vacancy in vacancies:
                if not matches_area(vacancy, search.area_id):
                    continue
                if vacancy.published_at < border:
                    continue
                if await db.enqueue(search.user_id, search.id, vacancy):
                    fresh += 1
        await db.touch_polled([search.id for search in group], now)
    await notifier.flush()
    log.info("poll searches=%s unique=%s new=%s", len(searches), len(groups), fresh)


async def _collect(hh: HhClient, spec: QuerySpec, cutoff: datetime) -> list:
    found = []
    for page in range(_MAX_PAGES):
        result = await hh.search(spec, page=page, per_page=_PAGE, cache_ttl=0)
        if not result.vacancies:
            break
        found.extend(result.vacancies)
        newest = max(item.published_at for item in result.vacancies)
        # An old card on an otherwise fresh page must not hide the next page.
        if newest < cutoff or len(result.vacancies) < _PAGE or page + 1 >= result.pages:
            break
    return found
