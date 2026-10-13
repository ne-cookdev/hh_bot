from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Search:
    id: int
    user_id: int
    name: str
    text_query: str
    area_id: str | None
    area_name: str | None
    work_format: str | None
    experience: str
    salary_from: int | None
    created_at: datetime
    last_polled_at: datetime | None
    chat_id: int | None = None
    notify_enabled: bool = True

    def experience_list(self) -> list[str]:
        if not self.experience:
            return []
        return [part for part in self.experience.split(",") if part]


@dataclass
class Vacancy:
    id: str
    name: str
    employer: str
    area: str
    salary_from: int | None
    salary_to: int | None
    salary_currency: str | None
    salary_gross: bool | None
    salary_mode: str | None
    url: str
    published_at: datetime
    experience: str | None
    work_formats: list[str]
    internship: bool
    area_id: str | None = None
    area_path: str = ""
    search_names: list[str] = field(default_factory=list)
    search_ids: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class QuerySpec:
    text: str
    area_id: str | None
    work_format: str | None
    experience: tuple[str, ...]
    salary_from: int | None

    def cache_key(self, page: int, per_page: int) -> str:
        return json.dumps(
            [self.text, self.area_id, self.work_format, list(self.experience), self.salary_from, page, per_page],
            ensure_ascii=False,
        )


@dataclass
class SearchResult:
    vacancies: list[Vacancy]
    page: int
    pages: int
