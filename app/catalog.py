"""Shared labels for hh.ru filter codes."""

PUSH_BATCH = 15
BROWSE_PAGE = 10
BROWSE_FETCH = 100
BROWSE_CACHE_TTL = 180
MAX_SEARCHES = 30
MAX_EXCLUSIONS = 40

WORK_FORMAT_LABELS = {
    "REMOTE": "удалённо",
    "HYBRID": "гибрид",
    "ON_SITE": "в офисе",
    "FIELD_WORK": "разъезд",
}

EXPERIENCE_LABELS = {
    "noExperience": "без опыта",
    "between1And3": "1–3 года",
    "between3And6": "3–6 лет",
    "moreThan6": "более 6 лет",
}

# hh.ru experience ids. "nointern" drops noExperience (стажёрские вакансии с этой меткой).
EXPERIENCE_PRESETS: dict[str, list[str]] = {
    "any": [],
    "nointern": ["between1And3", "between3And6", "moreThan6"],
    "none": ["noExperience"],
    "13": ["between1And3"],
    "36": ["between3And6"],
    "6": ["moreThan6"],
}

PRESET_LABELS = {
    "any": "любой",
    "nointern": "не стажёр",
    "none": "без опыта",
    "13": "1–3 года",
    "36": "3–6 лет",
    "6": "более 6 лет",
}

SALARY_MODE_LABELS = {
    "MONTH": "в месяц",
    "HOUR": "в час",
    "SHIFT": "за смену",
    "FLY_IN_FLY_OUT": "за вахту",
}

CURRENCY_SIGNS = {
    "RUR": "₽",
    "RUB": "₽",
    "USD": "$",
    "EUR": "€",
    "KZT": "₸",
    "BYR": "Br",
    "UAH": "₴",
}


def experience_label(codes: list[str]) -> str:
    if not codes:
        return "любой"
    if codes == EXPERIENCE_PRESETS["nointern"]:
        return "не стажёр (от 1 года)"
    return ", ".join(EXPERIENCE_LABELS.get(code, code) for code in codes)


def work_format_label(code: str | None) -> str:
    if not code:
        return "любой"
    return WORK_FORMAT_LABELS.get(code, code)
