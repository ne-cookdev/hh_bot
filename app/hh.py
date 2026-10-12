"""Read the public hh.ru vacancy search page.

Anonymous GET https://api.hh.ru/vacancies has returned 403 since April 2026
unless the caller is an approved employer application. The site search is the
same index and still answers without a token. Requests are serialized well
under 1 rps, identical queries are shared across users, and 429 pauses everyone.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import time
from datetime import datetime, timezone

import httpx

from app.models import QuerySpec, SearchResult, Vacancy

log = logging.getLogger("hh")

SEARCH_URL = "https://hh.ru/search/vacancy"
SUGGEST_URL = "https://api.hh.ru/suggests/areas"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
_LUX = 'id="HH-Lux-InitialState">'


class HhError(Exception):
    pass


class HhRateLimit(HhError):
    def __init__(self, retry_after: float):
        super().__init__(f"hh rate limit, retry after {retry_after:.0f}s")
        self.retry_after = retry_after


class _Limiter:
    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._lock = asyncio.Lock()
        self._next_at = 0.0
        self._cooldown_until = 0.0

    async def wait(self) -> None:
        async with self._lock:
            delay = max(self._next_at, self._cooldown_until) - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_at = time.monotonic() + self.min_interval

    async def penalize(self, seconds: float) -> None:
        async with self._lock:
            self._cooldown_until = max(self._cooldown_until, time.monotonic() + seconds)

    def cooldown_left(self) -> float:
        return max(0.0, self._cooldown_until - time.monotonic())


class HhClient:
    def __init__(self, min_interval: float = 1.0):
        self.limiter = _Limiter(min_interval)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=30.0),
            # ddos-guard holds the TLS handshake ~12s for this client. The
            # server then allows Keep-Alive: timeout=60. httpx drops idle
            # connections after 5s, so every poll opened a new handshake.
            limits=httpx.Limits(max_keepalive_connections=4, keepalive_expiry=50),
            follow_redirects=True,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            },
        )
        self._cache: dict[str, tuple[float, SearchResult]] = {}
        self._inflight: dict[str, asyncio.Task[SearchResult]] = {}
        self._gate = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def search(
        self,
        spec: QuerySpec,
        page: int,
        per_page: int,
        cache_ttl: float = 0,
    ) -> SearchResult:
        key = spec.cache_key(page, per_page)
        async with self._gate:
            now = time.monotonic()
            cached = self._cache.get(key)
            if cache_ttl and cached and cached[0] > now:
                return cached[1]
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(self._search(spec, page, per_page))
                self._inflight[key] = task
        try:
            result = await task
        finally:
            if self._inflight.get(key) is task and task.done():
                self._inflight.pop(key, None)
        if cache_ttl:
            self._cache[key] = (time.monotonic() + cache_ttl, result)
            if len(self._cache) > 200:
                expired = [item for item, (until, _) in self._cache.items() if until < time.monotonic()]
                for item in expired:
                    self._cache.pop(item, None)
        return result

    def cached(self, spec: QuerySpec, page: int, per_page: int) -> SearchResult | None:
        hit = self._cache.get(spec.cache_key(page, per_page))
        if hit is None or hit[0] <= time.monotonic():
            return None
        return hit[1]

    async def suggest_areas(self, text: str, limit: int = 6) -> list[tuple[str, str]]:
        await self.limiter.wait()
        response = await self._client.get(
            SUGGEST_URL,
            params={"text": text, "locale": "RU"},
            headers={"Accept": "application/json"},
        )
        if response.status_code == 429:
            retry_after = _retry_after(response)
            await self.limiter.penalize(retry_after)
            raise HhRateLimit(retry_after)
        if response.status_code != 200:
            raise HhError(f"area suggest http {response.status_code}")
        payload = response.json()
        hits: list[tuple[str, str]] = []
        for item in payload.get("items") or []:
            name = str(item.get("text") or "").strip()
            area_id = str(item.get("id") or "").strip()
            if not name or not area_id:
                continue
            parent = ((item.get("parent") or {}).get("text") or "").strip()
            if parent and parent.casefold() not in {"россия", "russia"}:
                name = f"{name} · {parent}"
            hits.append((area_id, name))
            if len(hits) >= limit:
                break
        return hits

    async def _search(self, spec: QuerySpec, page: int, per_page: int) -> SearchResult:
        params: list[tuple[str, str]] = [
            ("text", spec.text),
            ("order_by", "publication_time"),
            ("items_on_page", str(per_page)),
            ("page", str(page)),
            ("no_magic", "true"),
            ("L_save_area", "true"),
        ]
        if spec.area_id:
            params.append(("area", spec.area_id))
        if spec.work_format:
            params.append(("work_format", spec.work_format))
        for code in spec.experience:
            params.append(("experience", code))
        if spec.salary_from:
            params.append(("salary", str(spec.salary_from)))
            params.append(("currency_code", "RUR"))

        delay = 1.5
        last_error: Exception | None = None
        for attempt in range(3):
            await self.limiter.wait()
            started = time.monotonic()
            try:
                response = await self._client.get(SEARCH_URL, params=params, headers={"Referer": "https://hh.ru/"})
            except httpx.HTTPError as exc:
                last_error = exc
                log.warning(
                    "hh transport error attempt=%s/3 %s",
                    attempt + 1,
                    _error_chain(exc),
                    exc_info=exc if attempt == 2 else None,
                )
                await asyncio.sleep(delay)
                delay *= 2
                continue
            elapsed = time.monotonic() - started
            if response.status_code == 429:
                retry_after = _retry_after(response)
                await self.limiter.penalize(retry_after)
                log.warning("hh 429, pause %.0fs", retry_after)
                raise HhRateLimit(retry_after)
            if response.status_code in {403, 503}:
                await self.limiter.penalize(45)
                log.warning("hh %s, pause 45s", response.status_code)
                raise HhError(f"hh http {response.status_code}")
            if response.status_code >= 500:
                last_error = HhError(f"hh http {response.status_code}")
                log.warning("hh %s, retry", response.status_code)
                await asyncio.sleep(delay)
                delay *= 2
                continue
            if response.status_code != 200:
                raise HhError(f"hh http {response.status_code}")
            result = parse_search_html(response.text)
            log.info(
                "hh page=%s items=%s pages=%s %.2fs qlen=%s",
                page,
                len(result.vacancies),
                result.pages,
                elapsed,
                len(spec.text),
            )
            return result
        detail = _error_chain(last_error) if last_error is not None else "unknown"
        raise HhError(f"hh failed after retries: {detail}")


def _error_chain(exc: BaseException) -> str:
    parts: list[str] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(parts) < 6:
        seen.add(id(current))
        text = str(current).strip() or repr(current)
        parts.append(f"{type(current).__name__}: {text}")
        current = current.__cause__ or current.__context__
    return " <- ".join(parts)


def _retry_after(response: httpx.Response) -> float:
    raw = response.headers.get("Retry-After", "")
    try:
        return max(5.0, float(raw))
    except ValueError:
        return 30.0


def parse_search_html(page: str) -> SearchResult:
    marker = page.find(_LUX)
    if marker < 0:
        raise HhError("hh page has no search payload")
    start = page.find("{", marker)
    if start < 0:
        raise HhError("hh page payload is not json")
    try:
        data, _ = json.JSONDecoder().raw_decode(html.unescape(page[start:]))
    except json.JSONDecodeError as exc:
        raise HhError("hh page payload is broken") from exc
    result = data.get("vacancySearchResult") or {}
    raw_items = result.get("vacancies") or []
    vacancies = []
    for item in raw_items:
        parsed = _vacancy(item)
        if parsed is not None:
            vacancies.append(parsed)
    criteria = result.get("criteria") or {}
    current = int(criteria.get("page") or 0)
    pages = _total_pages(result.get("paging") or {}, current, len(vacancies))
    return SearchResult(vacancies=vacancies, page=current, pages=pages)


def _page_index(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _total_pages(paging: dict, current: int, vacancy_count: int) -> int:
    """hh drops lastPage once the last page is already inside paging.pages."""
    indexes: list[int] = []
    last = paging.get("lastPage")
    if isinstance(last, dict):
        index = _page_index(last.get("page"))
        if index is not None:
            indexes.append(index)
    for item in paging.get("pages") or []:
        if isinstance(item, dict):
            index = _page_index(item.get("page"))
            if index is not None:
                indexes.append(index)
    nxt = paging.get("next")
    if isinstance(nxt, dict) and not nxt.get("disabled"):
        index = _page_index(nxt.get("page"))
        if index is not None:
            indexes.append(index)
    if indexes:
        return max(max(indexes), current) + 1
    return current + 1 if vacancy_count else 0


def _vacancy(item: dict) -> Vacancy | None:
    if not isinstance(item, dict) or not item.get("vacancyId"):
        return None
    published = _published(item.get("publicationTime") or {})
    if published is None:
        return None
    company = item.get("company") or {}
    employer = (company.get("visibleName") or company.get("name") or "Работодатель скрыт").strip()
    area = item.get("area") or {}
    links = item.get("links") or {}
    url = links.get("desktop") or f"https://hh.ru/vacancy/{item['vacancyId']}"
    salary_from, salary_to, currency, gross, mode = _salary(item.get("compensation") or {})
    return Vacancy(
        id=str(item["vacancyId"]),
        name=str(item.get("name") or "Вакансия").strip(),
        employer=employer,
        area=str(area.get("name") or "").strip() or "—",
        salary_from=salary_from,
        salary_to=salary_to,
        salary_currency=currency,
        salary_gross=gross,
        salary_mode=mode,
        url=str(url),
        published_at=published,
        experience=(str(item["workExperience"]) if item.get("workExperience") else None),
        work_formats=_codes(item.get("workFormats"), "workFormatsElement"),
        internship=bool(item.get("internship")),
    )


def _published(raw: dict) -> datetime | None:
    if not isinstance(raw, dict):
        return None
    text = raw.get("$")
    if isinstance(text, str) and text:
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            moment = None
        else:
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            return moment
    stamp = raw.get("@timestamp")
    if stamp is None:
        return None
    return datetime.fromtimestamp(int(stamp), timezone.utc)


def _salary(comp: dict) -> tuple[int | None, int | None, str | None, bool | None, str | None]:
    if not comp or "noCompensation" in comp:
        return None, None, None, None, None
    return (
        _amount(comp.get("from")),
        _amount(comp.get("to")),
        (str(comp["currencyCode"]) if comp.get("currencyCode") else None),
        (bool(comp["gross"]) if comp.get("gross") is not None else None),
        (str(comp["mode"]) if comp.get("mode") else None),
    )


def _amount(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _codes(raw: object, key: str) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, str):
        return [raw]
    items = [raw] if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    found: list[str] = []
    for item in items:
        if isinstance(item, str):
            found.append(item)
        elif isinstance(item, dict):
            element = item.get(key)
            if isinstance(element, list):
                found.extend(str(part) for part in element if part)
            elif element:
                found.append(str(element))
    return found
