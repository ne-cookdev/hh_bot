from __future__ import annotations

import logging
from dataclasses import dataclass

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ErrorEvent, Message

from app.catalog import (
    BROWSE_CACHE_TTL,
    BROWSE_FETCH,
    BROWSE_PAGE,
    EXPERIENCE_PRESETS,
    MAX_EXCLUSIONS,
    MAX_SEARCHES,
    PUSH_BATCH,
    experience_label,
    work_format_label,
)
from app.config import Config
from app.db import Database
from app.format import browse_html, browse_window, esc, salary_filter_label, search_html
from app.hh import HhClient, HhError, HhRateLimit
from app.keyboards import (
    area_hits_kb,
    area_kb,
    browse_kb,
    cancel_kb,
    confirm_clear_exclusions_kb,
    confirm_clear_kb,
    confirm_delete_kb,
    confirm_kb,
    exclusions_kb,
    experience_kb,
    format_kb,
    main_kb,
    mode_kb,
    raw_kb,
    salary_kb,
    search_actions_kb,
    searches_kb,
    skip_kb,
)
from app.models import QuerySpec, Search
from app.notifier import Notifier
from app.query import (
    apply_exclusions,
    compile_simple,
    normalize_query,
    parse_exclusion_input,
    parse_salary,
    validate_query,
)

log = logging.getLogger("bot")

EXAMPLE = (
    "(NAME:frontend OR NAME:front-end OR NAME:фронтенд OR NAME:фронтэнд) "
    "AND React NOT стажер NOT intern NOT internship"
)

HELP = f"""\
<b>Что умеет бот</b>
Присылает новые вакансии с hh.ru. Уже присланные записаны в базу и после перезапуска не дублируются. За раз не больше {PUSH_BATCH}, остаток — кнопкой «Ещё» или следующим заходом.

<b>Поиски</b>
Их можно сохранить несколько. Одна и та же вакансия из разных поисков приходит одним сообщением.

Москва с любым форматом и «любой город, но только удалёнка» — это два отдельных поиска.

<b>Простой конструктор</b>
Слова в названии склеиваются через OR и ищутся только в заголовке. Дополнительные слова соединяются через AND и ищутся по всей вакансии.

<b>Свой запрос</b>
Тот же язык, что строка поиска на hh.ru.
<code>NAME:</code> заголовок, <code>COMPANY_NAME:</code> компания, <code>DESCRIPTION:</code> описание.
Операторы <code>AND</code> <code>OR</code> <code>NOT</code>, кавычки для фразы, скобки, <code>!слово</code> без синонимов.

Пример:
<code>{esc(EXAMPLE)}</code>

Регион, формат, опыт и зарплата задаются кнопками.

<b>Исключения</b>
Общий список. Дописывается ко всем поискам как <code>AND NOT слово</code>. Сюда удобно один раз положить стажер, intern, internship.

«Не стажёр» в опыте убирает вакансии с меткой «нет опыта». Слово «стажёр» в тексте само по себе так не выкидывается — для этого как раз исключения или NOT в запросе.
"""

AREAS = {
    "1": "Москва",
    "2": "Санкт-Петербург",
    "113": "Россия",
}


class AddSearch(StatesGroup):
    name = State()
    title = State()
    extra = State()
    raw = State()
    city = State()
    salary = State()
    filters = State()


class AddExclusion(StatesGroup):
    words = State()


@dataclass
class Ctx:
    db: Database
    hh: HhClient
    notifier: Notifier
    cfg: Config
    wake: object


def setup(ctx: Ctx) -> Router:
    router = Router()
    router.message.filter(F.chat.type == "private")
    router.callback_query.filter(F.message.chat.type == "private")

    @router.message.middleware()
    async def remember_user(handler, event: Message, data):
        if event.from_user is not None:
            await ctx.db.upsert_user(event.from_user.id, event.chat.id, event.from_user.username)
        return await handler(event, data)

    @router.callback_query.middleware()
    async def remember_click(handler, event: CallbackQuery, data):
        if event.from_user is not None and event.message is not None:
            await ctx.db.upsert_user(
                event.from_user.id,
                event.message.chat.id,
                event.from_user.username,
            )
        return await handler(event, data)

    async def menu(user_id: int):
        paused = not await ctx.db.notify_enabled(user_id)
        return main_kb(paused)

    async def show_exclusions(target: Message, user_id: int, edit: bool = False) -> None:
        items = await ctx.db.list_exclusions(user_id)
        if items:
            lines = "\n".join(f"• {esc(word)}" for _, word in items)
            text = f"<b>Исключения</b>\n{lines}\n\nЖми ✕ чтобы убрать слово. Они применяются ко всем поискам."
        else:
            text = (
                "<b>Исключения</b>\n"
                "Пока пусто. Слова отсюда дописываются к каждому поиску как NOT.\n"
                "Пример: <code>стажер, intern, internship</code>"
            )
        markup = exclusions_kb(items)
        if edit:
            try:
                await target.edit_text(text, reply_markup=markup)
                return
            except TelegramBadRequest:
                pass
        await target.answer(text, reply_markup=markup)

    async def show_search_card(message: Message, search: Search, user_id: int) -> None:
        count = len(await ctx.db.list_exclusions(user_id))
        await message.answer(search_html(search, count), reply_markup=search_actions_kb(search.id))

    async def restore_card(query: CallbackQuery, search_id: int) -> None:
        search = await ctx.db.get_search(query.from_user.id, search_id)
        if search is None or query.message is None:
            await query.answer("Поиск уже удалён")
            return
        count = len(await ctx.db.list_exclusions(query.from_user.id))
        await query.message.edit_text(search_html(search, count), reply_markup=search_actions_kb(search.id))

    def spec_for(search: Search, words: list[str]) -> QuerySpec:
        return QuerySpec(
            text=apply_exclusions(search.text_query, words),
            area_id=search.area_id,
            work_format=search.work_format,
            experience=tuple(search.experience_list()),
            salary_from=search.salary_from,
        )

    async def ask_area(message: Message) -> None:
        await message.answer("Регион?", reply_markup=area_kb())

    async def ask_format(query: CallbackQuery) -> None:
        await query.message.edit_text("Формат работы?", reply_markup=format_kb())

    async def ask_experience(query: CallbackQuery) -> None:
        await query.message.edit_text(
            "Опыт? «Не стажёр» оставляет 1–3, 3–6 и 6+.",
            reply_markup=experience_kb(),
        )

    async def ask_salary(query: CallbackQuery) -> None:
        await query.message.edit_text("Зарплата от? Вакансии без суммы hh тогда не покажет.", reply_markup=salary_kb())

    async def ask_confirm(query: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        words = [word for _, word in await ctx.db.list_exclusions(query.from_user.id)]
        area = data.get("area_name") or "любой регион"
        text = "\n".join(
            [
                f"<b>{esc(data['name'])}</b>",
                f"<code>{esc(data['text_query'])}</code>",
                " · ".join(
                    [
                        esc(area),
                        esc(work_format_label(data.get("work_format"))),
                        esc(experience_label(list(data.get("experience") or []))),
                        esc(salary_filter_label(data.get("salary_from"))),
                    ]
                ),
                (
                    "Исключения: " + esc(", ".join(words))
                    if words
                    else "Исключений нет — добавить можно в разделе «Исключения»."
                ),
                "",
                "Сохранить?",
            ]
        )
        await state.set_state(AddSearch.filters)
        await query.message.edit_text(text, reply_markup=confirm_kb())

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(HELP, reply_markup=await menu(message.from_user.id))

    @router.message(Command("help"))
    @router.message(F.text == "❓ Помощь")
    async def help_cmd(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(HELP, reply_markup=await menu(message.from_user.id))

    @router.message(Command("cancel"))
    @router.message(F.text == "Отмена")
    async def cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Ок.", reply_markup=await menu(message.from_user.id))

    @router.message(F.text == "⬅️ В меню")
    async def to_menu(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Меню.", reply_markup=await menu(message.from_user.id))

    @router.message(F.text == "🔍 Поиски")
    async def searches_menu(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            "Поиски. Новый запрос, список или полная очистка.",
            reply_markup=searches_kb(),
        )

    @router.message(F.text == "➕ Добавить")
    async def add_begin(message: Message, state: FSMContext) -> None:
        await state.set_state(AddSearch.name)
        await message.answer(
            "Как назвать поиск? Коротко, чтобы отличать в списке.\nПример: <code>React frontend</code>",
            reply_markup=cancel_kb(),
        )

    @router.message(F.text == "📋 Мои поиски")
    async def list_cmd(message: Message, state: FSMContext) -> None:
        await state.clear()
        rows = await ctx.db.list_searches(message.from_user.id)
        if not rows:
            await message.answer("Поисков нет. Жми «Добавить».", reply_markup=searches_kb())
            return
        await message.answer(f"Сохранено: {len(rows)}.", reply_markup=searches_kb())
        for search in rows:
            await show_search_card(message, search, message.from_user.id)

    @router.message(F.text == "🗑 Очистить все")
    async def clear_ask(message: Message, state: FSMContext) -> None:
        await state.clear()
        rows = await ctx.db.list_searches(message.from_user.id)
        if not rows:
            await message.answer("Удалять нечего.", reply_markup=searches_kb())
            return
        await message.answer(
            f"Удалить все поиски ({len(rows)})? Исключения останутся.",
            reply_markup=confirm_clear_kb(),
        )

    @router.message(F.text == "🚫 Исключения")
    async def exclusions_cmd(message: Message, state: FSMContext) -> None:
        await state.clear()
        await show_exclusions(message, message.from_user.id)

    @router.message(F.text.in_({"⏸ Пауза", "▶️ Уведомления"}))
    async def toggle_notify(message: Message, state: FSMContext) -> None:
        await state.clear()
        enabled = await ctx.db.notify_enabled(message.from_user.id)
        await ctx.db.set_notify(message.from_user.id, not enabled)
        if enabled:
            await message.answer(
                "Пауза. Новые вакансии копятся и не приходят, пока не включишь обратно.",
                reply_markup=main_kb(True),
            )
            return
        await message.answer("Снова присылаю.", reply_markup=main_kb(False))
        await ctx.notifier.push_user(message.from_user.id, message.chat.id, PUSH_BATCH)

    @router.message(AddSearch.name, F.text)
    async def take_name(message: Message, state: FSMContext) -> None:
        name = " ".join((message.text or "").split())
        if not name or len(name) > 40:
            await message.answer("Нужно имя до 40 символов.")
            return
        await state.update_data(name=name)
        await state.set_state(AddSearch.filters)
        await message.answer("Как задать запрос?", reply_markup=mode_kb())

    @router.callback_query(StateFilter(AddSearch), F.data == "w:abort")
    async def abort(query: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await query.answer()
        if query.message is None:
            return
        await query.message.edit_text("Отменено.")
        await query.message.answer("Меню.", reply_markup=await menu(query.from_user.id))

    @router.callback_query(StateFilter(AddSearch), F.data == "w:mode:s")
    async def mode_simple(query: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AddSearch.title)
        await query.answer()
        await query.message.edit_text(
            "Слова в <b>названии</b>. Несколько — через запятую или OR.\n"
            "Пример: <code>frontend, front-end, фронтенд, фронтэнд</code>",
            reply_markup=skip_kb(),
        )

    @router.callback_query(StateFilter(AddSearch), F.data == "w:mode:r")
    async def mode_raw(query: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AddSearch.raw)
        await query.answer()
        await query.message.edit_text(
            "Пришли запрос языком hh.ru.\n"
            f"Пример:\n<code>{esc(EXAMPLE)}</code>",
            reply_markup=raw_kb(),
        )

    @router.callback_query(StateFilter(AddSearch), F.data == "w:ex")
    async def use_example(query: CallbackQuery, state: FSMContext) -> None:
        await state.update_data(text_query=EXAMPLE)
        await state.set_state(AddSearch.filters)
        await query.answer("Подставил пример")
        await ask_area_edit(query)

    @router.callback_query(StateFilter(AddSearch), F.data == "w:skip")
    async def skip(query: CallbackQuery, state: FSMContext) -> None:
        current = await state.get_state()
        await query.answer()
        if current == AddSearch.title.state:
            await state.update_data(title_words="")
            await state.set_state(AddSearch.extra)
            await query.message.edit_text(
                "Слова в <b>любом месте</b> вакансии. Через запятую — AND, "
                "внутри пункта можно OR: <code>React OR Vue</code>.",
                reply_markup=skip_kb(),
            )
            return
        if current == AddSearch.extra.state:
            await state.update_data(extra_words="")
            if not await finish_simple(query, state):
                return
            await ask_area_edit(query)

    @router.message(AddSearch.title, F.text)
    async def take_title(message: Message, state: FSMContext) -> None:
        await state.update_data(title_words=message.text or "")
        await state.set_state(AddSearch.extra)
        await message.answer(
            "Слова в <b>любом месте</b> вакансии. Через запятую — AND, "
            "внутри пункта можно OR: <code>React OR Vue</code>. Можно пропустить.",
            reply_markup=skip_kb(),
        )

    @router.message(AddSearch.extra, F.text)
    async def take_extra(message: Message, state: FSMContext) -> None:
        await state.update_data(extra_words=message.text or "")
        data = await state.get_data()
        try:
            query = compile_simple(data.get("title_words") or "", data.get("extra_words") or "")
        except ValueError:
            await message.answer("Нужно хотя бы одно слово в названии или в тексте.")
            return
        error = validate_query(query)
        if error:
            await message.answer(error)
            return
        await state.update_data(text_query=query)
        await state.set_state(AddSearch.filters)
        await ask_area(message)

    @router.message(AddSearch.raw, F.text)
    async def take_raw(message: Message, state: FSMContext) -> None:
        query = normalize_query(message.text or "")
        error = validate_query(query)
        if error:
            await message.answer(error)
            return
        await state.update_data(text_query=query)
        await state.set_state(AddSearch.filters)
        await ask_area(message)

    @router.callback_query(StateFilter(AddSearch), F.data.startswith("w:r:"))
    async def pick_region(query: CallbackQuery, state: FSMContext) -> None:
        choice = query.data.split(":")[2]
        await query.answer()
        if choice == "city":
            await state.set_state(AddSearch.city)
            await query.message.edit_text("Напиши город. Покажу варианты.")
            return
        if choice == "0":
            await state.update_data(area_id=None, area_name=None)
        else:
            await state.update_data(area_id=choice, area_name=AREAS.get(choice, choice))
        await state.set_state(AddSearch.filters)
        await ask_format(query)

    @router.message(AddSearch.city, F.text)
    async def take_city(message: Message, state: FSMContext) -> None:
        text = " ".join((message.text or "").split())
        if len(text) < 2:
            await message.answer("Слишком коротко.")
            return
        try:
            hits = await ctx.hh.suggest_areas(text)
        except HhRateLimit as exc:
            await message.answer(f"hh просит подождать около {int(exc.retry_after)} сек.")
            return
        except HhError:
            log.exception("area suggest failed")
            await message.answer("Не удалось спросить города у hh. Попробуй ещё раз.")
            return
        if not hits:
            await message.answer("Ничего не нашёл. Напиши иначе, например «Казань».")
            return
        await state.update_data(area_choices={area_id: name for area_id, name in hits})
        await state.set_state(AddSearch.filters)
        await message.answer("Какой из этих?", reply_markup=area_hits_kb(hits))

    @router.callback_query(StateFilter(AddSearch), F.data.startswith("w:a:"))
    async def pick_city(query: CallbackQuery, state: FSMContext) -> None:
        area_id = query.data.split(":", 2)[2]
        data = await state.get_data()
        name = (data.get("area_choices") or {}).get(area_id, area_id)
        await state.update_data(area_id=area_id, area_name=name)
        await query.answer()
        await ask_format(query)

    @router.callback_query(StateFilter(AddSearch), F.data.startswith("w:f:"))
    async def pick_format(query: CallbackQuery, state: FSMContext) -> None:
        choice = query.data.split(":")[2]
        await state.update_data(work_format=None if choice == "any" else choice)
        await query.answer()
        await ask_experience(query)

    @router.callback_query(StateFilter(AddSearch), F.data.startswith("w:x:"))
    async def pick_experience(query: CallbackQuery, state: FSMContext) -> None:
        choice = query.data.split(":")[2]
        await state.update_data(experience=EXPERIENCE_PRESETS.get(choice, []))
        await query.answer()
        await ask_salary(query)

    @router.callback_query(StateFilter(AddSearch), F.data.startswith("w:p:"))
    async def pick_salary(query: CallbackQuery, state: FSMContext) -> None:
        choice = query.data.split(":")[2]
        await query.answer()
        if choice == "ask":
            await state.set_state(AddSearch.salary)
            await query.message.edit_text("Напиши сумму от. Можно <code>180000</code> или <code>180к</code>.")
            return
        await state.update_data(salary_from=None if choice == "0" else int(choice))
        await ask_confirm(query, state)

    @router.message(AddSearch.salary, F.text)
    async def take_salary(message: Message, state: FSMContext) -> None:
        amount = parse_salary(message.text or "")
        if amount is None:
            await message.answer("Не разобрал. Пример: <code>180000</code> или <code>180к</code>.")
            return
        await state.update_data(salary_from=amount)
        await state.set_state(AddSearch.filters)
        data = await state.get_data()
        words = [word for _, word in await ctx.db.list_exclusions(message.from_user.id)]
        area = data.get("area_name") or "любой регион"
        text = "\n".join(
            [
                f"<b>{esc(data['name'])}</b>",
                f"<code>{esc(data['text_query'])}</code>",
                " · ".join(
                    [
                        esc(area),
                        esc(work_format_label(data.get("work_format"))),
                        esc(experience_label(list(data.get("experience") or []))),
                        esc(salary_filter_label(amount)),
                    ]
                ),
                (
                    "Исключения: " + esc(", ".join(words))
                    if words
                    else "Исключений нет."
                ),
                "",
                "Сохранить?",
            ]
        )
        await message.answer(text, reply_markup=confirm_kb())

    @router.callback_query(StateFilter(AddSearch), F.data == "w:save")
    async def save(query: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        if data.get("saving"):
            await query.answer()
            return
        if "name" not in data or "text_query" not in data:
            await state.clear()
            await query.answer("Мастер сбросился, начни поиск заново", show_alert=True)
            return
        await state.update_data(saving=True)
        status = await ctx.db.add_search(
            query.from_user.id,
            data["name"],
            data["text_query"],
            data.get("area_id"),
            data.get("area_name"),
            data.get("work_format"),
            list(data.get("experience") or []),
            data.get("salary_from"),
        )
        await query.answer()
        if status == "duplicate":
            await state.update_data(saving=False)
            await query.message.answer("Поиск с таким именем уже есть. Начни заново и назови иначе.")
            return
        if status == "limit":
            await state.clear()
            await query.message.edit_text(f"Лимит {MAX_SEARCHES} поисков. Удали лишний.")
            return
        await state.clear()
        ctx.wake.set()
        await query.message.edit_text(
            "Сохранил. Уже висящие на сайте сам не дублирую — пришлю то, что вышло "
            "за последние минуты, и всё новое дальше. Обычно это меньше минуты.\n"
            "Полный список, включая старые, — кнопка «Все вакансии»."
        )
        rows = await ctx.db.list_searches(query.from_user.id)
        created = next((row for row in rows if row.name == data["name"]), None)
        await query.message.answer("Поиски.", reply_markup=searches_kb())
        if created is not None:
            await show_search_card(query.message, created, query.from_user.id)

    @router.message(AddSearch.filters)
    async def filters_text(message: Message) -> None:
        await message.answer("Жми кнопку под сообщением или «Отмена».")

    @router.callback_query(F.data.startswith("s:all:"))
    async def browse(query: CallbackQuery) -> None:
        if query.message is None:
            await query.answer()
            return
        parts = (query.data or "").split(":")
        search_id = int(parts[2])
        page = max(0, int(parts[3]))
        search = await ctx.db.get_search(query.from_user.id, search_id)
        if search is None:
            await query.answer("Поиск удалён")
            return
        await query.answer()
        edit = _is_browse(query.message)
        words = [word for _, word in await ctx.db.list_exclusions(query.from_user.id)]
        spec = spec_for(search, words)
        hh_page = page // (BROWSE_FETCH // BROWSE_PAGE)
        result = ctx.hh.cached(spec, hh_page, BROWSE_FETCH)
        status = query.message
        if result is None:
            if edit:
                try:
                    await query.message.edit_text("Открываю hh…")
                except TelegramBadRequest:
                    edit = False
            status = query.message if edit else await query.message.answer("Открываю hh…")
            try:
                result = await ctx.hh.search(
                    spec,
                    page=hh_page,
                    per_page=BROWSE_FETCH,
                    cache_ttl=BROWSE_CACHE_TTL,
                )
            except HhRateLimit as exc:
                await status.edit_text(f"hh просит подождать около {int(exc.retry_after)} сек, чтобы не словить бан.")
                return
            except HhError:
                log.exception("browse failed")
                await status.edit_text("hh сейчас не ответил. Попробуй через минуту.")
                return
        shown, pages = browse_window(
            result.vacancies,
            page,
            page_size=BROWSE_PAGE,
            fetch_size=BROWSE_FETCH,
            hh_pages=result.pages,
        )
        if page > 0 and not shown:
            text, markup = "Дальше пусто.", browse_kb(search.id, [], page, pages)
        else:
            text = browse_html(search, shown, page, pages, BROWSE_PAGE)
            while len(text) > 3900 and len(shown) > 1:
                shown = shown[:-1]
                text = browse_html(search, shown, page, pages, BROWSE_PAGE)
            markup = browse_kb(search.id, shown, page, pages)
        if edit or status is not query.message:
            await status.edit_text(text, reply_markup=markup)
        else:
            await query.message.answer(text, reply_markup=markup)

    @router.callback_query(F.data == "s:nop")
    async def nop(query: CallbackQuery) -> None:
        await query.answer()

    @router.callback_query(F.data == "s:x")
    async def close(query: CallbackQuery) -> None:
        await query.answer()
        try:
            await query.message.delete()
        except TelegramBadRequest:
            pass

    @router.callback_query(F.data.startswith("s:rm:"))
    async def ask_delete(query: CallbackQuery) -> None:
        search_id = int(query.data.split(":")[2])
        search = await ctx.db.get_search(query.from_user.id, search_id)
        if search is None:
            await query.answer("Уже удалён")
            return
        await query.answer()
        await query.message.edit_text(
            f"Удалить поиск <b>{esc(search.name)}</b>?",
            reply_markup=confirm_delete_kb(search_id),
        )

    @router.callback_query(F.data.startswith("s:no:"))
    async def keep_search(query: CallbackQuery) -> None:
        await query.answer()
        await restore_card(query, int(query.data.split(":")[2]))

    @router.callback_query(F.data.startswith("s:ok:"))
    async def do_delete(query: CallbackQuery) -> None:
        search_id = int(query.data.split(":")[2])
        removed = await ctx.db.delete_search(query.from_user.id, search_id)
        await query.answer("Удалил" if removed else "Уже нет")
        await query.message.edit_text("Поиск удалён." if removed else "Этого поиска уже нет.")

    @router.callback_query(F.data == "q:okclr")
    async def do_clear(query: CallbackQuery) -> None:
        count = await ctx.db.delete_all_searches(query.from_user.id)
        await query.answer()
        await query.message.edit_text(f"Удалил поисков: {count}.")

    @router.callback_query(F.data == "q:more")
    async def more(query: CallbackQuery) -> None:
        await query.answer()
        sent = await ctx.notifier.push_user(query.from_user.id, query.message.chat.id, PUSH_BATCH)
        if not sent:
            await query.message.edit_text("Очередь пустая.")

    @router.callback_query(F.data == "q:stop")
    async def stop_queue(query: CallbackQuery) -> None:
        dropped = await ctx.db.skip_queued(query.from_user.id)
        await query.answer()
        await query.message.edit_text(
            f"Дальше сам не пришлю ({dropped}). Уже вышедшие смотри через «Все вакансии» в поиске."
        )

    @router.callback_query(F.data == "e:add")
    async def excl_add(query: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AddExclusion.words)
        await query.answer()
        await query.message.answer(
            "Пришли слова через запятую или с новой строки.\n"
            "Пример: <code>стажер, intern, internship</code>",
            reply_markup=cancel_kb(),
        )

    @router.message(AddExclusion.words, F.text)
    async def excl_save(message: Message, state: FSMContext) -> None:
        words = parse_exclusion_input(message.text or "")
        if not words:
            await message.answer("Не увидел слов. Пример: <code>стажер, intern</code>")
            return
        added = await ctx.db.add_exclusions(message.from_user.id, words)
        await state.clear()
        extra = ""
        if added < len(words):
            extra = f"\nЧасть не добавил: дубли или лимит {MAX_EXCLUSIONS}."
        await message.answer(f"Добавил: {added}.{extra}", reply_markup=await menu(message.from_user.id))
        await show_exclusions(message, message.from_user.id)

    @router.callback_query(F.data.startswith("e:rm:"))
    async def excl_rm(query: CallbackQuery) -> None:
        exclusion_id = int(query.data.split(":")[2])
        await ctx.db.delete_exclusion(query.from_user.id, exclusion_id)
        await query.answer("Убрал")
        await show_exclusions(query.message, query.from_user.id, edit=True)

    @router.callback_query(F.data == "e:clr")
    async def excl_clear_ask(query: CallbackQuery) -> None:
        await query.answer()
        await query.message.edit_text("Очистить все исключения?", reply_markup=confirm_clear_exclusions_kb())

    @router.callback_query(F.data == "e:okclr")
    async def excl_clear(query: CallbackQuery) -> None:
        await ctx.db.clear_exclusions(query.from_user.id)
        await query.answer("Очистил")
        await show_exclusions(query.message, query.from_user.id, edit=True)

    @router.callback_query(F.data == "e:no")
    async def excl_keep(query: CallbackQuery) -> None:
        await query.answer()
        await show_exclusions(query.message, query.from_user.id, edit=True)

    @router.message()
    async def fallback(message: Message) -> None:
        await message.answer("Жми кнопки внизу. Если забыл синтаксис — «Помощь».", reply_markup=await menu(message.from_user.id))

    @router.errors()
    async def on_error(event: ErrorEvent) -> None:
        log.exception("update failed", exc_info=event.exception)

    return router


async def ask_area_edit(query: CallbackQuery) -> None:
    await query.message.edit_text("Регион?", reply_markup=area_kb())


async def finish_simple(query: CallbackQuery, state: FSMContext) -> bool:
    data = await state.get_data()
    try:
        text = compile_simple(data.get("title_words") or "", data.get("extra_words") or "")
    except ValueError:
        await query.message.answer("Нужно хотя бы одно слово. Напиши его или нажми «Отмена».")
        return False
    error = validate_query(text)
    if error:
        await query.message.answer(error)
        return False
    await state.update_data(text_query=text)
    await state.set_state(AddSearch.filters)
    return True


def _is_browse(message: Message) -> bool:
    markup = message.reply_markup
    if markup is None:
        return False
    for row in markup.inline_keyboard:
        for button in row:
            if button.callback_data == "s:nop":
                return True
    return False
