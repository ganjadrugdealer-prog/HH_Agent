# -*- coding: utf-8 -*-
"""Сопроводительные письма и пинги: спинтакс, проверка, примеры, память по чатам.

Как это работает в движке (hh_applicant_tool):
  1. текст шаблона прогоняется через rand_text (utils/string.py) — она заменяет
     каждый блок {а|б|в} на случайный вариант, включая вложенные;
  2. результат подставляется в оператор `%` вместе со словарём плейсхолдеров.

Отсюда два железных правила, которые здесь и проверяются ДО запуска рассылки:
  * количество { и } должно совпадать, иначе блок останется в тексте письма;
  * одиночный символ % ломает отправку с ошибкой формата — нужно либо %%,
    либо известная подстановка вида %(first_name)s.
"""
from __future__ import annotations

import json
import random
import re
from pathlib import Path
from typing import Any, Iterable

# Плейсхолдеры, которые движок реально кладёт в словарь подстановки.
# Источник: operations/apply_vacancies.py и operations/reply_employers.py.
LETTER_PLACEHOLDERS = (
    "first_name",
    "last_name",
    "email",
    "phone",
    "resume_hash",
    "resume_title",
    "resume_url",
    "vacancy_name",
    "employer_name",
)
PING_PLACEHOLDERS = (
    "first_name",
    "last_name",
    "email",
    "phone",
    "vacancy_name",
    "employer_name",
    "resume_title",
)

# Значения для предпросмотра — на HH они не уходят.
DEMO_VALUES = {
    "first_name": "Анна",
    "last_name": "Иванова",
    "email": "anna@example.com",
    "phone": "+7 900 000-00-00",
    "resume_hash": "a1b2c3d4e5",
    "resume_title": "Редактор",
    "resume_url": "https://hh.ru/resume/a1b2c3d4e5",
    "vacancy_name": "Контент-редактор",
    "employer_name": "ООО «Пример»",
}

VARIANTS_CAP = 10 ** 12  # дальше считать бессмысленно


class SpintaxError(ValueError):
    pass


# --------------------------------------------------------------- разворот

# Тот же шаблон, что у движка: пустой блок {} он не трогает, и мы тоже.
_GROUP_RE = re.compile(r"\{([^{}]+)\}")


def expand(text: str, rnd: Any = None) -> str:
    """Развернуть спинтакс так же, как это сделает движок."""
    rnd = rnd or random
    s = str(text)
    for _ in range(64):
        new = _GROUP_RE.sub(lambda m: rnd.choice(m.group(1).split("|")), s)
        if new == s:
            return s
        s = new
    return s


def _norm(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s).replace(" ", " ")).strip().lower()


def _shorten(s: str, limit: int = 60) -> str:
    s = re.sub(r"\s+", " ", str(s)).strip()
    return s if len(s) <= limit else s[:limit] + "…"


# ------------------------------------------------------ подсчёт вариантов


def count_variants(text: str) -> int:
    """Сколько разных писем может получиться из шаблона.

    Считается честно: последовательность блоков перемножается,
    варианты внутри блока складываются, вложенность учитывается.
    """
    s = str(text)
    total, pos = _count(s, 0, 0)
    if pos != len(s):
        raise SpintaxError("Лишняя закрывающая скобка «}»")
    return total


def _count(s: str, i: int, depth: int) -> tuple[int, int]:
    seq = 1        # произведение подряд идущих кусков
    alts: list[int] = []   # накопленные варианты, разделённые |
    while i < len(s):
        ch = s[i]
        if ch == "{":
            sub, i = _count(s, i + 1, depth + 1)
            seq = min(seq * sub, VARIANTS_CAP)
        elif ch == "}":
            if depth == 0:
                return sum(alts) + seq, i
            alts.append(seq)
            return min(sum(alts), VARIANTS_CAP), i + 1
        elif ch == "|" and depth > 0:
            alts.append(seq)
            seq = 1
            i += 1
        else:
            i += 1
    if depth > 0:
        raise SpintaxError("Не закрыта фигурная скобка «{»")
    return min(sum(alts) + seq if alts else seq, VARIANTS_CAP), i


# ---------------------------------------------------------------- проверка


def validate(text: str, kind: str = "letter") -> dict[str, Any]:
    """Полная проверка шаблона. errors блокируют запуск, warnings — нет."""
    text = str(text or "")
    stripped = text.strip()
    errors: list[str] = []
    warnings: list[str] = []

    if not stripped:
        return {
            "ok": False,
            "errors": ["Шаблон пустой"],
            "warnings": [],
            "variants": 0,
            "groups": 0,
            "length": 0,
        }

    # 1. скобки
    stack: list[int] = []
    line = 1
    for ch in text:
        if ch == "\n":
            line += 1
        elif ch == "{":
            stack.append(line)
        elif ch == "}":
            if stack:
                stack.pop()
            else:
                errors.append(f"Лишняя закрывающая скобка «}}», строка {line}")
    for ln in stack:
        errors.append(f"Не закрыта фигурная скобка «{{», строка {ln}")

    # 2. проценты
    allowed = LETTER_PLACEHOLDERS if kind == "letter" else PING_PLACEHOLDERS
    i = 0
    bare_percent = False
    while True:
        i = text.find("%", i)
        if i < 0:
            break
        rest = text[i:]
        if rest.startswith("%%"):
            i += 2
            continue
        m = re.match(r"%\((\w+)\)s", rest)
        if m:
            if m.group(1) not in allowed:
                errors.append(
                    f"Подстановка %({m.group(1)})s не поддерживается. "
                    f"Доступны: {', '.join(allowed)}"
                )
            i += m.end()
            continue
        bare_percent = True
        i += 1
    if bare_percent:
        errors.append(
            "Символ «%» сам по себе ломает отправку. "
            "Если нужен процент — напиши «%%»."
        )

    # 3. подозрительные блоки
    groups = 0
    for m in _GROUP_RE.finditer(text):
        groups += 1
        parts = m.group(1).split("|")
        if len(parts) == 1:
            warnings.append(
                f"Блок «{_shorten(m.group(0))}» без вариантов — потерялся «|»?"
            )
        if any(not p.strip() for p in parts):
            warnings.append(
                f"В блоке «{_shorten(m.group(0))}» есть пустой вариант"
            )

    # 4. варианты
    variants = 0
    if not errors:
        try:
            variants = count_variants(text)
        except SpintaxError as exc:
            errors.append(str(exc))
    if not errors:
        if variants <= 1:
            warnings.append(
                "Ни одного блока {а|б} — все получат абсолютно одинаковый текст"
            )
        elif variants < 20:
            warnings.append(
                f"Всего {variants} вариантов. Добавь блоков, иначе тексты "
                "начнут повторяться."
            )

    # 5. длина
    if len(stripped) > 2000:
        warnings.append(
            f"{len(stripped)} символов. Длинное письмо редко дочитывают."
        )

    # dedupe, порядок сохраняем
    errors = list(dict.fromkeys(errors))
    warnings = list(dict.fromkeys(warnings))

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "variants": variants,
        "groups": groups,
        "length": len(stripped),
    }


def samples(text: str, n: int = 3, kind: str = "letter", seed: Any = None) -> list[str]:
    """Несколько разных развёрнутых вариантов — для кнопки «Показать примеры»."""
    rnd = random.Random(seed)
    values = {
        k: DEMO_VALUES.get(k, "")
        for k in (LETTER_PLACEHOLDERS if kind == "letter" else PING_PLACEHOLDERS)
    }
    out: list[str] = []
    seen: set[str] = set()
    for _ in range(max(n, 1) * 60):
        if len(out) >= n:
            break
        try:
            s = expand(text, rnd) % values
        except Exception as exc:
            return [f"Шаблон не разворачивается: {exc}"]
        key = _norm(s)
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


# ------------------------------------------- память по чатам (для пингов)

_memory_path: Path | None = None
_memory_cache: dict[str, list[str]] | None = None


def set_memory_path(path: Any) -> None:
    """Куда складывать, что уже отправлено в каждый чат."""
    global _memory_path, _memory_cache
    _memory_path = Path(path) if path else None
    _memory_cache = None


def _memory() -> dict[str, list[str]]:
    global _memory_cache
    if _memory_cache is None:
        _memory_cache = {}
        if _memory_path and _memory_path.exists():
            try:
                data = json.loads(_memory_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    _memory_cache = {
                        str(k): [str(x) for x in v][-60:]
                        for k, v in data.items()
                        if isinstance(v, list)
                    }
            except Exception:
                _memory_cache = {}
    return _memory_cache


def _memory_add(key: str, value: str) -> None:
    mem = _memory()
    bucket = mem.setdefault(str(key), [])
    bucket.append(value)
    del bucket[:-60]
    if not _memory_path:
        return
    try:
        _memory_path.parent.mkdir(parents=True, exist_ok=True)
        _memory_path.write_text(
            json.dumps(mem, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


_MY_LINE = re.compile(r"^\[[^\]]*\]\s*Я:\s*")


def _already_sent(history: Iterable[Any] | None) -> set[str]:
    """Мои прошлые сообщения из истории чата, как её собирает движок."""
    out: set[str] = set()
    for line in history or []:
        m = _MY_LINE.match(str(line))
        if m:
            out.add(_norm(str(line)[m.end():]))
    return out


def pick_unique(
    template: str,
    history: Iterable[Any] | None = None,
    key: Any = None,
    tries: int = 300,
) -> str:
    """Развернуть шаблон так, чтобы текст не повторял уже отправленный в этот чат."""
    used = _already_sent(history)
    if key is not None:
        used |= set(_memory().get(str(key), []))
    fallback = None
    for _ in range(max(tries, 1)):
        s = expand(template)
        n = _norm(s)
        if fallback is None:
            fallback = s
        if n and n not in used:
            if key is not None:
                _memory_add(str(key), n)
            return s
    return fallback if fallback is not None else str(template)


# ------------------------------------------------------------- шаблоны

# Стартовый шаблон письма. Формулировки нарочно без рода — одинаково
# годятся мужчине и женщине. Показывает все приёмы: блоки, подстановки.
DEFAULT_LETTER = """{Здравствуйте|Добрый день}{! М|, м}еня зовут %(first_name)s.

{Пишу по вакансии|Откликаюсь на вакансию|Интересует вакансия} \
«%(vacancy_name)s». {Мой опыт|То, чем я занимаюсь,} \
{подходит|хорошо ложится} под задачи из описания.

{Подробности в резюме|Резюме с деталями|Всё подробно в резюме}: %(resume_url)s

{Расскажу подробнее|Отвечу на вопросы|Покажу примеры работ} \
{в удобное вам время|при созвоне|в переписке}.

{С уважением|Спасибо за внимание}, %(first_name)s
%(phone)s"""

# Пинг — короткое напоминание о себе. Вариантов намеренно много:
# в один чат уходит несколько пингов, и они не должны совпасть.
DEFAULT_PING = """{Здравствуйте|Добрый день|Приветствую}{! Н|, н}а связи \
%(first_name)s {по вакансии|по отклику на вакансию} «%(vacancy_name)s».

{Подскажите|Уточню|Есть вопрос}{,|:} \
{вакансия ещё актуальна|позиция всё ещё открыта|нужен ли вам ещё специалист}? \
{Если да|Если актуально} — \
{обсужу детали|созвонюсь|отвечу на любые вопросы} \
{в любое удобное время|когда вам будет удобно|на этой неделе}.

{Спасибо|Заранее спасибо|Благодарю} {за ответ|за уделённое время}{!|.}
{С уважением|Хорошего дня}, %(first_name)s"""
