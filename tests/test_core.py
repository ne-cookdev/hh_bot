import html
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import Config
from app.db import Database
from app.format import browse_html, browse_window, vacancy_html
from app.hh import matches_area, parse_search_html
from app.models import Search, SearchResult, Vacancy
from app.poller import _PAGE, cutoff_for, poll_once
from app.query import (
    apply_exclusions,
    compile_simple,
    parse_exclusion_input,
    parse_salary,
    validate_query,
)


class QueryTests(unittest.TestCase):
    def test_simple_title_and_extra(self):
        query = compile_simple("frontend, front-end, фронтенд", "React")
        self.assertEqual(
            query,
            "(NAME:frontend OR NAME:front-end OR NAME:фронтенд) AND React",
        )

    def test_phrase_is_quoted(self):
        self.assertEqual(compile_simple("front end", ""), 'NAME:"front end"')

    def test_extra_or_stays_or(self):
        self.assertEqual(
            compile_simple("frontend", "React OR Vue, TypeScript"),
            "NAME:frontend AND (React OR Vue) AND TypeScript",
        )

    def test_exclusions_wrap_query(self):
        text = apply_exclusions("React", ["стажер", "intern", "стажер"])
        self.assertEqual(text, "(React) AND NOT стажер AND NOT intern")

    def test_exclusion_phrase(self):
        self.assertEqual(
            apply_exclusions("React", ["junior developer"]),
            '(React) AND NOT "junior developer"',
        )

    def test_reject_broken_parens(self):
        self.assertIsNotNone(validate_query("(React"))

    def test_salary(self):
        self.assertEqual(parse_salary("180к"), 180000)
        self.assertEqual(parse_salary("от 200 000"), 200000)
        self.assertIsNone(parse_salary("много"))

    def test_exclusion_input(self):
        self.assertEqual(
            parse_exclusion_input("стажер, intern\nAND, internship"),
            ["стажер", "intern", "internship"],
        )


class ParseTests(unittest.TestCase):
    def test_lux_payload(self):
        vacancy = {
            "vacancyId": 42,
            "name": "Frontend <React>",
            "company": {"visibleName": "Рога & копыта"},
            "compensation": {
                "from": 200000,
                "to": 300000,
                "currencyCode": "RUR",
                "gross": True,
                "mode": "MONTH",
            },
            "publicationTime": {"$": "2026-10-11T19:16:51.671+03:00"},
            "area": {"@id": 1, "name": "Москва", "path": ".113.232.1."},
            "links": {"desktop": "https://hh.ru/vacancy/42"},
            "workExperience": "between1And3",
            "workFormats": [{"workFormatsElement": ["REMOTE", "HYBRID"]}],
            "internship": False,
        }
        payload = {
            "vacancySearchResult": {
                "vacancies": [vacancy, {"name": "ad"}],
                "paging": {"lastPage": {"page": 3}},
                "criteria": {"page": 0},
            }
        }
        page = (
            '<template id="HH-Lux-InitialState">'
            + html.escape(json.dumps(payload, ensure_ascii=False))
            + "</template>"
        )
        result = parse_search_html(page)
        self.assertEqual(result.pages, 4)
        self.assertEqual(len(result.vacancies), 1)
        item = result.vacancies[0]
        self.assertEqual(item.id, "42")
        self.assertEqual(item.salary_from, 200000)
        self.assertEqual(item.work_formats, ["REMOTE", "HYBRID"])
        self.assertEqual(item.employer, "Рога & копыта")
        self.assertEqual(item.area_id, "1")
        self.assertTrue(matches_area(item, "1"))
        foreign = Vacancy(
            id="138398932",
            name="Lead",
            employer="SmartAdvanced",
            area="Екатеринбург",
            salary_from=None,
            salary_to=None,
            salary_currency=None,
            salary_gross=None,
            salary_mode=None,
            url="https://hh.ru/vacancy/138398932",
            published_at=datetime.now(timezone.utc),
            experience=None,
            work_formats=[],
            internship=False,
            area_id="3",
            area_path=".113.223.1261.3.",
        )
        self.assertFalse(matches_area(foreign, "1"))
        self.assertTrue(matches_area(foreign, None))
        text = vacancy_html(item)
        self.assertIn("Frontend &lt;React&gt;", text)
        self.assertIn("Рога &amp; копыта", text)
        self.assertIn("200 000–300 000 ₽", text)

    def test_pages_when_last_page_is_in_the_list(self):
        payload = {
            "vacancySearchResult": {
                "vacancies": [{"vacancyId": 1, "name": "Dev", "links": {"desktop": "https://hh.ru/vacancy/1"}}],
                "paging": {
                    "pages": [
                        {"text": "4", "page": 3, "selected": True},
                        {"text": "7", "page": 6, "selected": False},
                    ],
                    "lastPage": None,
                    "next": {"page": 4, "disabled": False},
                },
                "criteria": {"page": 3},
            }
        }
        page = (
            '<template id="HH-Lux-InitialState">'
            + html.escape(json.dumps(payload))
            + "</template>"
        )
        result = parse_search_html(page)
        self.assertEqual(result.page, 3)
        self.assertEqual(result.pages, 7)

    def test_browse_numbering(self):
        search = Search(
            id=1,
            user_id=1,
            name="React",
            text_query="React",
            area_id="1",
            area_name="Москва",
            work_format=None,
            experience="",
            salary_from=None,
            created_at=datetime.now(timezone.utc),
            last_polled_at=None,
        )
        vacancy = Vacancy(
            id="1",
            name="Dev",
            employer="Co",
            area="Москва",
            salary_from=None,
            salary_to=None,
            salary_currency=None,
            salary_gross=None,
            salary_mode=None,
            url="https://hh.ru/vacancy/1",
            published_at=datetime.now(timezone.utc),
            experience=None,
            work_formats=[],
            internship=False,
        )
        text = browse_html(search, [vacancy], page=1, pages=3, page_size=10)
        self.assertIn("11. <b>Dev</b>", text)
        self.assertIn("стр. 2 из 3", text)

    def test_browse_window_slices_one_hh_page(self):
        items = list(range(67))
        shown, pages = browse_window(items, 3, page_size=10, fetch_size=100, hh_pages=1)
        self.assertEqual(shown, list(range(30, 40)))
        self.assertEqual(pages, 7)
        shown, pages = browse_window(items, 6, page_size=10, fetch_size=100, hh_pages=1)
        self.assertEqual(shown, list(range(60, 67)))
        self.assertEqual(pages, 7)
        chunk = list(range(100, 200))
        shown, pages = browse_window(chunk, 10, page_size=10, fetch_size=100, hh_pages=3)
        self.assertEqual(shown, list(range(100, 110)))
        self.assertEqual(pages, 30)


class DbTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.tmp.name) / "t.sqlite3"))
        await self.db.connect()
        await self.db.upsert_user(1, 1, "pavel")

    async def asyncTearDown(self):
        await self.db.close()
        self.tmp.cleanup()

    def _vacancy(self, vid: str) -> Vacancy:
        return Vacancy(
            id=vid,
            name="Frontend",
            employer="Co",
            area="Москва",
            salary_from=100000,
            salary_to=None,
            salary_currency="RUR",
            salary_gross=True,
            salary_mode="MONTH",
            url=f"https://hh.ru/vacancy/{vid}",
            published_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            experience="between1And3",
            work_formats=["REMOTE"],
            internship=False,
        )

    async def test_dedup_across_searches(self):
        self.assertEqual(
            await self.db.add_search(1, "a", "React", "1", "Москва", "REMOTE", [], None),
            "ok",
        )
        self.assertEqual(
            await self.db.add_search(1, "b", "frontend", None, None, None, ["between1And3"], None),
            "ok",
        )
        self.assertEqual(
            await self.db.add_search(1, "a", "other", None, None, None, [], None),
            "duplicate",
        )
        searches = await self.db.list_searches(1)
        vacancy = self._vacancy("99")
        self.assertTrue(await self.db.enqueue(1, searches[0].id, vacancy))
        self.assertFalse(await self.db.enqueue(1, searches[1].id, vacancy))
        queued = await self.db.take_queued(1, 15)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].search_names, ["a", "b"])
        await self.db.mark_pushed(1, "99")
        self.assertEqual(await self.db.take_queued(1, 15), [])
        self.assertFalse(await self.db.enqueue(1, searches[0].id, vacancy))

    async def test_delete_search_drops_only_its_queue(self):
        await self.db.add_search(1, "a", "React", None, None, None, [], None)
        await self.db.add_search(1, "b", "Go", None, None, None, [], None)
        searches = await self.db.list_searches(1)
        only_a = self._vacancy("1")
        both = self._vacancy("2")
        await self.db.enqueue(1, searches[0].id, only_a)
        await self.db.enqueue(1, searches[0].id, both)
        await self.db.enqueue(1, searches[1].id, both)
        await self.db.delete_search(1, searches[0].id)
        left = await self.db.take_queued(1, 15)
        self.assertEqual([item.id for item in left], ["2"])
        self.assertEqual(left[0].search_names, ["b"])

    async def test_wal_database_reopens_as_delete(self):
        await self.db.close()
        raw = sqlite3.connect(self.db.path)
        raw.execute("PRAGMA journal_mode=WAL")
        raw.execute("CREATE TABLE probe (id INTEGER)")
        raw.execute("INSERT INTO probe VALUES (7)")
        raw.commit()
        raw.close()
        await self.db.connect()
        cursor = await self.db.conn.execute("PRAGMA journal_mode")
        mode = (await cursor.fetchone())[0]
        self.assertEqual(str(mode).lower(), "delete")
        cursor = await self.db.conn.execute("SELECT id FROM probe")
        self.assertEqual((await cursor.fetchone())["id"], 7)
        self.assertFalse(Path(self.db.path + "-wal").exists())
        self.assertFalse(Path(self.db.path + "-shm").exists())


class _Hh:
    def __init__(self, pages: dict[int, list[Vacancy]]):
        self.pages = pages
        self.calls: list[int] = []

    async def search(self, spec, page: int, per_page: int, cache_ttl: float = 0):
        self.calls.append(page)
        return SearchResult(vacancies=self.pages.get(page, []), page=page, pages=len(self.pages))


class _Notifier:
    async def flush(self) -> None:
        return None


def _card(vid: str, published: datetime, area: str, area_id: str, path: str) -> Vacancy:
    return Vacancy(
        id=vid,
        name=vid,
        employer="Co",
        area=area,
        salary_from=None,
        salary_to=None,
        salary_currency=None,
        salary_gross=None,
        salary_mode=None,
        url=f"https://hh.ru/vacancy/{vid}",
        published_at=published,
        experience="between1And3",
        work_formats=[],
        internship=False,
        area_id=area_id,
        area_path=path,
    )


class PollerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.tmp.name) / "t.sqlite3"))
        await self.db.connect()
        await self.db.upsert_user(1, 1, "pavel")
        self.now = datetime(2026, 10, 13, 14, 20, tzinfo=timezone.utc)
        self.cfg = Config(
            bot_token="x",
            database_path=self.db.path,
            poll_interval=40,
            hh_min_interval=1,
            lookback_minutes=5,
            catchup_minutes=24 * 60,
        )

    async def asyncTearDown(self):
        await self.db.close()
        self.tmp.cleanup()

    async def test_cutoff_keeps_the_night_after_downtime(self):
        search = Search(
            id=1,
            user_id=1,
            name="Backend Python",
            text_query="Python",
            area_id="1",
            area_name="Москва",
            work_format=None,
            experience="",
            salary_from=None,
            created_at=datetime(2026, 10, 12, 11, 57, tzinfo=timezone.utc),
            last_polled_at=datetime(2026, 10, 12, 17, 2, tzinfo=timezone.utc),
        )
        border = cutoff_for(search, self.now, self.cfg)
        self.assertLess(border, datetime(2026, 10, 12, 19, 12, tzinfo=timezone.utc))
        fresh = Search(
            id=2,
            user_id=1,
            name="new",
            text_query="Python",
            area_id="1",
            area_name="Москва",
            work_format=None,
            experience="",
            salary_from=None,
            created_at=self.now,
            last_polled_at=None,
        )
        self.assertGreater(cutoff_for(fresh, self.now, self.cfg), self.now - timedelta(hours=1))

    async def test_new_vacancies_arrive_after_downtime(self):
        await self.db.add_search(
            1, "Backend Python", "Python", "1", "Москва", None, ["between1And3"], None
        )
        await self.db.conn.execute(
            "UPDATE searches SET created_at = ?, last_polled_at = ?",
            ("2026-10-12T11:57:53.000000+00:00", "2026-10-12T17:02:00.000000+00:00"),
        )
        await self.db.conn.commit()
        searches = await self.db.list_searches(1)
        already = _card("already", self.now - timedelta(hours=3), "Москва", "1", ".113.232.1.")
        self.assertTrue(await self.db.enqueue(1, searches[0].id, already))
        await self.db.mark_pushed(1, "already")

        old = [
            _card(f"old{i}", self.now - timedelta(hours=30), "Москва", "1", ".113.232.1.")
            for i in range(_PAGE - 2)
        ]
        page0 = [
            *old,
            _card("ekb", self.now - timedelta(hours=1), "Екатеринбург", "3", ".113.223.1261.3."),
            _card("just-now", self.now - timedelta(minutes=2), "Москва", "1", ".113.232.1."),
        ]
        page1 = [
            _card("overnight", self.now - timedelta(hours=10), "Москва", "1", ".113.232.1."),
            already,
        ]
        hh = _Hh({0: page0, 1: page1})
        await poll_once(self.db, hh, _Notifier(), self.cfg)
        queued = await self.db.take_queued(1, 15)
        self.assertEqual([item.id for item in queued], ["just-now", "overnight"])
        self.assertEqual(hh.calls, [0, 1])


if __name__ == "__main__":
    unittest.main()
