from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from app.catalog import BROWSE_PAGE, PUSH_BATCH
from app.format import short_button
from app.models import Vacancy


def _reply(rows: list[list[str]]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=item) for item in row] for row in rows],
        resize_keyboard=True,
        is_persistent=True,
    )


def main_kb(paused: bool) -> ReplyKeyboardMarkup:
    toggle = "▶️ Уведомления" if paused else "⏸ Пауза"
    return _reply([["🔍 Поиски", "🚫 Исключения"], [toggle, "❓ Помощь"]])


def searches_kb() -> ReplyKeyboardMarkup:
    return _reply([["➕ Добавить", "📋 Мои поиски"], ["🗑 Очистить все", "⬅️ В меню"]])


def cancel_kb() -> ReplyKeyboardMarkup:
    return _reply([["Отмена"]])


def mode_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Простой конструктор", callback_data="w:mode:s")],
            [InlineKeyboardButton(text="Свой запрос hh", callback_data="w:mode:r")],
            [InlineKeyboardButton(text="Отмена", callback_data="w:abort")],
        ]
    )


def skip_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Пропустить", callback_data="w:skip")],
            [InlineKeyboardButton(text="Отмена", callback_data="w:abort")],
        ]
    )


def raw_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Подставить пример React", callback_data="w:ex")],
            [InlineKeyboardButton(text="Отмена", callback_data="w:abort")],
        ]
    )


def area_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Москва", callback_data="w:r:1"),
                InlineKeyboardButton(text="Санкт-Петербург", callback_data="w:r:2"),
            ],
            [
                InlineKeyboardButton(text="Россия", callback_data="w:r:113"),
                InlineKeyboardButton(text="Любой регион", callback_data="w:r:0"),
            ],
            [InlineKeyboardButton(text="Указать город", callback_data="w:r:city")],
            [InlineKeyboardButton(text="Отмена", callback_data="w:abort")],
        ]
    )


def area_hits_kb(hits: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    rows = []
    for area_id, name in hits:
        label = name if len(name) <= 60 else name[:59] + "…"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"w:a:{area_id}")])
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="w:abort")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Любой", callback_data="w:f:any"),
                InlineKeyboardButton(text="Удалённо", callback_data="w:f:REMOTE"),
            ],
            [
                InlineKeyboardButton(text="Офис", callback_data="w:f:ON_SITE"),
                InlineKeyboardButton(text="Гибрид", callback_data="w:f:HYBRID"),
            ],
            [InlineKeyboardButton(text="Отмена", callback_data="w:abort")],
        ]
    )


def experience_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Любой", callback_data="w:x:any"),
                InlineKeyboardButton(text="Не стажёр", callback_data="w:x:nointern"),
            ],
            [
                InlineKeyboardButton(text="Без опыта", callback_data="w:x:none"),
                InlineKeyboardButton(text="1–3 года", callback_data="w:x:13"),
            ],
            [
                InlineKeyboardButton(text="3–6 лет", callback_data="w:x:36"),
                InlineKeyboardButton(text="Более 6", callback_data="w:x:6"),
            ],
            [InlineKeyboardButton(text="Отмена", callback_data="w:abort")],
        ]
    )


def salary_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Не важна", callback_data="w:p:0"),
                InlineKeyboardButton(text="от 100к", callback_data="w:p:100000"),
            ],
            [
                InlineKeyboardButton(text="от 150к", callback_data="w:p:150000"),
                InlineKeyboardButton(text="от 200к", callback_data="w:p:200000"),
            ],
            [
                InlineKeyboardButton(text="от 300к", callback_data="w:p:300000"),
                InlineKeyboardButton(text="Своя сумма", callback_data="w:p:ask"),
            ],
            [InlineKeyboardButton(text="Отмена", callback_data="w:abort")],
        ]
    )


def confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Сохранить", callback_data="w:save")],
            [InlineKeyboardButton(text="Отмена", callback_data="w:abort")],
        ]
    )


def search_actions_kb(search_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Все вакансии", callback_data=f"s:all:{search_id}:0"),
                InlineKeyboardButton(text="Удалить", callback_data=f"s:rm:{search_id}"),
            ]
        ]
    )


def confirm_delete_kb(search_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, удалить", callback_data=f"s:ok:{search_id}"),
                InlineKeyboardButton(text="Нет", callback_data=f"s:no:{search_id}"),
            ]
        ]
    )


def confirm_clear_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, очистить", callback_data="q:okclr"),
                InlineKeyboardButton(text="Нет", callback_data="s:x"),
            ]
        ]
    )


def vacancy_kb(vacancy: Vacancy) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="Открыть на hh.ru", url=vacancy.url)]]
    if len(vacancy.search_ids) == 1:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Все вакансии поиска",
                    callback_data=f"s:all:{vacancy.search_ids[0]}:0",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def queue_kb(remaining: int) -> InlineKeyboardMarkup:
    take = min(PUSH_BATCH, remaining)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=f"Ещё {take}", callback_data="q:more"),
                InlineKeyboardButton(text="Хватит", callback_data="q:stop"),
            ]
        ]
    )


def browse_kb(search_id: int, vacancies: list[Vacancy], page: int, pages: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    pair: list[InlineKeyboardButton] = []
    for offset, vacancy in enumerate(vacancies):
        pair.append(
            InlineKeyboardButton(
                text=short_button(page * BROWSE_PAGE + offset + 1, vacancy.name),
                url=vacancy.url,
            )
        )
        if len(pair) == 1:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"s:all:{search_id}:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{max(pages, 1)}", callback_data="s:nop"))
    if pages and page + 1 < pages:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"s:all:{search_id}:{page + 1}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton(text="Закрыть", callback_data="s:x")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def exclusions_kb(items: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    rows = []
    for exclusion_id, word in items:
        label = word if len(word) <= 28 else word[:27] + "…"
        rows.append(
            [InlineKeyboardButton(text=f"✕ {label}", callback_data=f"e:rm:{exclusion_id}")]
        )
    rows.append(
        [
            InlineKeyboardButton(text="Добавить", callback_data="e:add"),
            InlineKeyboardButton(text="Очистить", callback_data="e:clr"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_clear_exclusions_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, очистить", callback_data="e:okclr"),
                InlineKeyboardButton(text="Нет", callback_data="e:no"),
            ]
        ]
    )
