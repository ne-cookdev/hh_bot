"""hh.ru query language helpers.

The text box is the same language as the site search
(https://hh.ru/article/1175): AND / OR / NOT, quotes, parentheses,
NAME:, COMPANY_NAME:, DESCRIPTION:.

Region, schedule, experience and salary are separate filters, not part
of the text. Words from the account-wide exclusion list are appended
as AND NOT so one list covers every saved search.
"""

from __future__ import annotations

import re

_OR_SPLIT = re.compile(r"\s+OR\s+|[\n,;]+", re.IGNORECASE)
_PLAIN_TERM = re.compile(r"^[\w.*\-]+$", re.UNICODE)
_SALARY = re.compile(r"^(?:от)?(\d{1,8})(к|k)?$", re.IGNORECASE)

MAX_QUERY_LEN = 1500


def quote_term(term: str) -> str:
    term = term.strip().strip('"')
    if not term:
        raise ValueError("empty term")
    if _PLAIN_TERM.fullmatch(term):
        return term
    return '"' + term.replace('"', "") + '"'


def split_terms(raw: str) -> list[str]:
    parts = [part.strip() for part in _OR_SPLIT.split(raw or "")]
    return [part for part in parts if part]


def _extra_clause(raw: str) -> str:
    """Comma-separated parts are AND. `OR` inside one part stays OR."""
    parts = [part.strip() for part in re.split(r"[\n,;]+", raw or "") if part.strip()]
    clauses: list[str] = []
    for part in parts:
        options = split_terms(part)
        if len(options) > 1:
            clauses.append("(" + " OR ".join(quote_term(option) for option in options) + ")")
        else:
            clauses.append(quote_term(options[0]))
    return " AND ".join(clauses)


def compile_simple(title_words: str, extra_words: str) -> str:
    """Title words are OR-ed inside NAME:. Extra words are AND-ed anywhere."""
    titles = split_terms(title_words)
    chunks: list[str] = []
    if titles:
        named = " OR ".join(f"NAME:{quote_term(term)}" for term in titles)
        chunks.append(f"({named})" if len(titles) > 1 else named)
    extra = _extra_clause(extra_words)
    if extra:
        chunks.append(extra)
    if not chunks:
        raise ValueError("empty query")
    return " AND ".join(chunks)


def validate_query(query: str) -> str | None:
    query = " ".join((query or "").split())
    if not query:
        return "Запрос пустой."
    if len(query) > MAX_QUERY_LEN:
        return f"Слишком длинный запрос (максимум {MAX_QUERY_LEN} символов)."
    if query.count("(") != query.count(")"):
        return "Скобки не сходятся."
    if query.count('"') % 2:
        return "Кавычки не сходятся."
    return None


def normalize_query(query: str) -> str:
    return " ".join(query.split())


def apply_exclusions(query: str, words: list[str]) -> str:
    cleaned = []
    seen: set[str] = set()
    for word in words:
        token = " ".join(word.split())
        if not token:
            continue
        key = token.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(token)
    text = normalize_query(query)
    if not cleaned:
        return text
    parts = [f"({text})"]
    parts.extend(f"NOT {quote_term(word)}" for word in cleaned)
    return " AND ".join(parts)


def parse_exclusion_input(raw: str) -> list[str]:
    words: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[\n,;]+", raw or ""):
        word = part.strip().lstrip("-").strip().strip('"')
        word = " ".join(word.split())
        if not word or len(word) > 40 or word.casefold() in {"and", "or", "not"}:
            continue
        key = word.casefold()
        if key in seen:
            continue
        seen.add(key)
        words.append(word)
    return words


def parse_salary(raw: str) -> int | None:
    text = (raw or "").strip().lower().replace(" ", "").replace("\u00a0", "")
    text = text.replace("руб", "").replace("₽", "")
    match = _SALARY.fullmatch(text)
    if not match:
        return None
    value = int(match.group(1))
    if match.group(2):
        value *= 1000
    if value < 1000 or value > 10_000_000:
        return None
    return value
