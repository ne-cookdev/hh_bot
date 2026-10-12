from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone

from app.catalog import (
    CURRENCY_SIGNS,
    EXPERIENCE_LABELS,
    SALARY_MODE_LABELS,
    WORK_FORMAT_LABELS,
    experience_label,
    work_format_label,
)
from app.models import Search, Vacancy

MSK = timezone(timedelta(hours=3))
_MONTHS = "янв фев мар апр мая июн июл авг сен окт ноя дек".split()


def esc(value: str | None) -> str:
    return html.escape(value or "", quote=False)


def money(amount: int) -> str:
    return f"{amount:,}".replace(",", " ")


def format_salary(vacancy: Vacancy) -> str:
    if vacancy.salary_from is None and vacancy.salary_to is None:
        return "зарплата не указана"
    sign = CURRENCY_SIGNS.get(vacancy.salary_currency or "", vacancy.salary_currency or "")
    if vacancy.salary_from is not None and vacancy.salary_to is not None:
        body = f"{money(vacancy.salary_from)}–{money(vacancy.salary_to)} {sign}".strip()
    elif vacancy.salary_from is not None:
        body = f"от {money(vacancy.salary_from)} {sign}".strip()
    else:
        body = f"до {money(vacancy.salary_to or 0)} {sign}".strip()
    tail = []
    if vacancy.salary_gross is True:
        tail.append("до вычета")
    elif vacancy.salary_gross is False:
        tail.append("на руки")
    mode = SALARY_MODE_LABELS.get(vacancy.salary_mode or "")
    if mode:
        tail.append(mode)
    if tail:
        return body + " · " + ", ".join(tail)
    return body


def format_when(moment: datetime) -> str:
    local = moment.astimezone(MSK)
    return f"{local.day} {_MONTHS[local.month - 1]}, {local:%H:%M}"


def format_formats(codes: list[str]) -> str:
    if not codes:
        return ""
    order = ["REMOTE", "HYBRID", "ON_SITE", "FIELD_WORK"]
    ranked = sorted(codes, key=lambda code: order.index(code) if code in order else 99)
    return " · ".join(WORK_FORMAT_LABELS.get(code, code) for code in ranked)


def vacancy_html(vacancy: Vacancy) -> str:
    lines = [f"<b>{esc(vacancy.name)}</b>", esc(vacancy.employer)]
    place = esc(vacancy.area)
    formats = format_formats(vacancy.work_formats)
    if formats:
        place = f"{place} · {esc(formats)}"
    lines.append(place)
    lines.append(esc(format_salary(vacancy)))
    bits = []
    if vacancy.experience:
        bits.append(EXPERIENCE_LABELS.get(vacancy.experience, vacancy.experience))
    if vacancy.internship:
        bits.append("стажировка")
    bits.append(format_when(vacancy.published_at))
    lines.append(esc(" · ".join(bits)))
    if vacancy.search_names:
        lines.append("поиск: " + esc(" · ".join(vacancy.search_names)))
    return "\n".join(lines)


def salary_filter_label(amount: int | None) -> str:
    if not amount:
        return "не важна"
    return f"от {money(amount)} ₽"


def search_html(search: Search, exclusion_count: int) -> str:
    lines = [
        f"<b>{esc(search.name)}</b>",
        f"<code>{esc(search.text_query)}</code>",
        " · ".join(
            [
                esc(search.area_name or "любой регион"),
                esc(work_format_label(search.work_format)),
                esc(experience_label(search.experience_list())),
                esc(salary_filter_label(search.salary_from)),
            ]
        ),
    ]
    if exclusion_count:
        lines.append(f"исключения аккаунта: {exclusion_count}")
    return "\n".join(lines)


def browse_window(
    vacancies: list[Vacancy],
    page: int,
    *,
    page_size: int,
    fetch_size: int,
    hh_pages: int,
) -> tuple[list[Vacancy], int]:
    """One hh response of `fetch_size` covers several telegram pages of `page_size`."""
    per = fetch_size // page_size
    hh_page = page // per
    local = page % per
    start = local * page_size
    end = start + page_size
    if local == per - 1:
        end = max(end, len(vacancies))
    shown = vacancies[start:end] if start < len(vacancies) else []
    if hh_pages <= hh_page + 1 or not shown:
        filled = (len(vacancies) + page_size - 1) // page_size if vacancies else 0
        pages = hh_page * per + filled
    else:
        pages = hh_pages * per
    if shown:
        pages = max(pages, page + 1)
    elif page == 0:
        pages = max(pages, 1)
    return shown, pages


def browse_html(
    search: Search,
    vacancies: list[Vacancy],
    page: int,
    pages: int,
    page_size: int,
) -> str:
    header = (
        f"<b>{esc(search.name)}</b>\n"
        f"сначала новые · стр. {page + 1} из {max(pages, 1)}"
    )
    if not vacancies:
        return header + "\n\nПо этому запросу пусто."
    blocks = [header, ""]
    for index, vacancy in enumerate(vacancies, start=page * page_size + 1):
        place = esc(vacancy.area)
        formats = format_formats(vacancy.work_formats)
        if formats:
            place += " · " + esc(formats)
        blocks.append(
            "\n".join(
                [
                    f"{index}. <b>{esc(vacancy.name)}</b>",
                    f"{esc(vacancy.employer)} · {place}",
                    esc(format_salary(vacancy)) + " · " + esc(format_when(vacancy.published_at)),
                ]
            )
        )
    return "\n\n".join(blocks)


def short_button(index: int, name: str) -> str:
    label = f"{index}. {name}"
    if len(label) <= 60:
        return label
    return label[:59] + "…"
