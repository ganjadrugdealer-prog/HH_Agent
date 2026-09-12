# -*- coding: utf-8 -*-
"""Слой-адаптер между приложением и движком hh_applicant_tool.

Это ЕДИНСТВЕННЫЙ модуль, который знает внутренности движка: как называются
операции, какие у них флаги, как их запускать. Весь остальной код приложения
обращается только сюда — словарями с человеческими ключами.

Зачем: автор движка регулярно переименовывает флаги. Раньше это ломало
приложение молча, уже после сборки exe. Теперь check() вызывается в селф-тесте
и падает со списком расхождений ДО того, как соберётся дистрибутив.
"""
from __future__ import annotations

import argparse
import io
import logging
import re
import threading
from contextlib import redirect_stdout
from importlib import import_module
from typing import Any, Callable

logger = logging.getLogger("hh_agent.engine")

ENGINE_PACKAGE = "hh_applicant_tool"
ENGINE_EXPECTED = "1.8.28"

_current_cancel_event = None

def cancel_run() -> bool:
    """Останавливает текущую операцию движка, если она есть."""
    if _current_cancel_event:
        _current_cancel_event.set()
        return True
    return False

# Логическое имя операции -> модуль движка.
MODULES = {
    "apply": "apply_vacancies",
    "bump": "update_resumes",
    "ping": "reply_employers",
    "clear": "clear_negotiations",
    "authorize": "authorize",
    "whoami": "whoami",
    "test_session": "test_session",
    "refresh_token": "refresh_token",
    "clear_skipped": "clear_skipped",
    "list_resumes": "list_resumes",
}

# Ключ приложения -> (флаг движка, тип).
# Типы: value  — «--flag значение»
#       list   — «--flag знач1 знач2»
#       bool   — argparse.BooleanOptionalAction, есть парная форма --no-flag
#       switch — store_true, парной формы нет
OPTIONS: dict[str, dict[str, tuple[str, str]]] = {
    "apply": {
        "resume_id": ("--resume-id", "value"),
        "search": ("--search", "value"),
        "letter_file": ("--letter-file", "value"),
        "force_message": ("--force-message", "bool"),
        "excluded_filter": ("--excluded-filter", "value"),
        "max_responses": ("--max-responses", "value"),
        "total_pages": ("--total-pages", "value"),
        "per_page": ("--per-page", "value"),
        "skip_tests": ("--skip-tests", "bool"),
        "send_email": ("--send-email", "bool"),
        "dry_run": ("--dry-run", "bool"),
        "order_by": ("--order-by", "value"),
        "experience": ("--experience", "value"),
        "schedule": ("--schedule", "value"),
        "work_format": ("--work-format", "list"),
        "employment": ("--employment", "list"),
        "area": ("--area", "list"),
        "metro": ("--metro", "list"),
        "professional_role": ("--professional-role", "list"),
        "industry": ("--industry", "list"),
        "employer_id": ("--employer-id", "list"),
        "excluded_employer_id": ("--excluded-employer-id", "list"),
        "currency": ("--currency", "value"),
        "salary": ("--salary", "value"),
        "only_with_salary": ("--only-with-salary", "bool"),
        "label": ("--label", "list"),
        "period": ("--period", "value"),
        "premium": ("--premium", "bool"),
        "date_from": ("--date-from", "value"),
        "date_to": ("--date-to", "value"),
        "no_magic": ("--no-magic", "switch"),
        "search_field": ("--search-field", "list"),
        "top_lat": ("--top-lat", "value"),
        "bottom_lat": ("--bottom-lat", "value"),
        "left_lng": ("--left-lng", "value"),
        "right_lng": ("--right-lng", "value"),
        "sort_point_lat": ("--sort-point-lat", "value"),
        "sort_point_lng": ("--sort-point-lng", "value"),
    },
    "ping": {
        "resume_id": ("--resume-id", "value"),
        "message": ("--reply-message", "value"),
        "period": ("--period", "value"),
        "max_pages": ("--max-pages", "value"),
        "only_invitations": ("--only-invitations", "bool"),
        "dry_run": ("--dry-run", "bool"),
    },
    "clear": {
        "blacklist": ("--blacklist-discard", "bool"),
        "older_than": ("--older-than", "value"),
        "delete_chat": ("--delete-chat", "switch"),
        "block_ats": ("--block-ats", "switch"),
        "dry_run": ("--dry-run", "switch"),
    },
    "bump": {},
}

# Без этих флагов приложение работать не сможет — проверяем в селф-тесте.
REQUIRED = {
    "apply": [
        "--resume-id", "--letter-file", "--force-message", "--excluded-filter",
        "--max-responses", "--total-pages", "--per-page", "--dry-run",
        "--skip-tests", "--experience", "--work-format", "--schedule",
        "--area", "--salary", "--only-with-salary", "--period", "--order-by",
    ],
    "ping": [
        "--resume-id", "--reply-message", "--period", "--max-pages",
        "--only-invitations", "--dry-run",
    ],
    "clear": ["--blacklist-discard", "--older-than", "--dry-run"],
    "bump": [],
}

# Настройки запуска по умолчанию: один прогон — один день работы.
# max_responses = 200 совпадает с суточным лимитом откликов на HH; если HH
# поменяет лимит, движок всё равно остановится сам и напишет про лимит.
# period намеренно НЕ задан: движок хранит уже увиденные вакансии в своей базе
# и второй раз на них не откликнется, поэтому сужать окно публикации незачем —
# это только урезало бы выдачу после долгого перерыва.
DAILY_DEFAULTS: dict[str, Any] = {
    "total_pages": 20,
    "per_page": 100,
    "max_responses": 200,
    "force_message": True,
    "skip_tests": True,
}

# Паузa между запросами к API — случайная в этих границах (см. hh_patch).
API_DELAY_RANGE = (0.8, 2.4)


# ------------------------------------------------------------------ argv


def build_argv(kind: str, options: dict[str, Any] | None) -> list[str]:
    """Словарь приложения -> argv для операции движка."""
    spec = OPTIONS.get(kind)
    if spec is None:
        raise KeyError(f"неизвестная операция {kind!r}")
    argv: list[str] = []
    unknown: list[str] = []
    for key, value in (options or {}).items():
        if key not in spec:
            unknown.append(key)
            continue
        flag, mode = spec[key]
        if value is None or value == "":
            continue
        if mode in ("bool", "switch"):
            if value:
                argv.append(flag)
            elif mode == "bool":
                argv.append(flag.replace("--", "--no-", 1))
            continue
        if mode == "list":
            items = value if isinstance(value, (list, tuple)) else [value]
            items = [
                str(i.get("id", "")) if isinstance(i, dict) else str(i)
                for i in items
            ]
            items = [i for i in items if i]
            if items:
                argv.append(flag)
                argv.extend(items)
            continue
        if isinstance(value, float) and value == int(value):
            value = int(value)
        argv.extend([flag, str(value)])
    if unknown:
        logger.warning("не знаю параметров: %s", ", ".join(sorted(unknown)))
    return argv


def apply_params(options: dict[str, Any] | None) -> dict[str, Any]:
    """Параметры отклика с подставленными умолчаниями."""
    result = dict(DAILY_DEFAULTS)
    result.update(options or {})
    return {k: v for k, v in result.items() if k in OPTIONS["apply"]}


# --------------------------------------------------------------- проверка


def version() -> str:
    """Версия движка. Пустая строка, если метаданные не попали в сборку."""
    for dist in ("hh-applicant-tool", "hh_applicant_tool"):
        try:
            from importlib.metadata import version as _v

            return _v(dist)
        except Exception:
            continue
    return ""


def _parser_for(module_name: str) -> tuple[Any, argparse.ArgumentParser]:
    mod = import_module(f"{ENGINE_PACKAGE}.operations.{module_name}")
    op = mod.Operation()
    parser = argparse.ArgumentParser(add_help=False)
    op.setup_parser(parser)
    return mod, parser


def _flags(parser: argparse.ArgumentParser) -> set[str]:
    out: set[str] = set()
    for action in parser._actions:
        out.update(action.option_strings)
    return out


def check() -> dict[str, Any]:
    """Совместим ли установленный движок с этим приложением."""
    problems: list[str] = []
    ver = version()
    if ver and ver != ENGINE_EXPECTED:
        problems.append(
            f"версия движка {ver}, приложение писалось под {ENGINE_EXPECTED} — "
            "перепроверь флаги"
        )

    for kind, module_name in MODULES.items():
        try:
            mod, parser = _parser_for(module_name)
        except Exception as exc:
            problems.append(f"{kind}: не импортируется {module_name} ({exc})")
            continue
        if not hasattr(mod, "Operation"):
            problems.append(f"{kind}: в {module_name} нет класса Operation")
            continue
        have = _flags(parser)
        for flag in REQUIRED.get(kind, []):
            if flag not in have:
                problems.append(f"{kind}: пропал флаг {flag}")
        for key, (flag, _mode) in OPTIONS.get(kind, {}).items():
            if flag not in have:
                problems.append(
                    f"{kind}: параметр {key} ссылается на несуществующий {flag}"
                )

    # то, чем пользуемся напрямую, минуя операции
    for path, attr in (
        ("utils.string", "rand_text"),
        ("api.client", "ApiClient"),
        ("ui.api", "Api"),
        ("main", "main"),
    ):
        try:
            mod = import_module(f"{ENGINE_PACKAGE}.{path}")
            if not hasattr(mod, attr):
                problems.append(f"в {path} нет {attr}")
        except Exception as exc:
            problems.append(f"не импортируется {path} ({exc})")

    return {"ok": not problems, "version": ver or "неизвестна",
            "expected": ENGINE_EXPECTED, "problems": problems}


# ---------------------------------------------------------------- запуск

_ANSI = re.compile(r"\x1B\[[0-9;]*[mK]")


def run(
    tool: Any,
    kind: str,
    options: dict[str, Any] | None = None,
    on_line: Callable[[str], None] | None = None,
) -> tuple[str, int]:
    """Синхронно выполнить операцию движка и вернуть (вывод, код).

    Ловим и print, и логи: часть операций сообщает важное только через logger.
    """
    module_name = MODULES.get(kind)
    if not module_name:
        return (f"неизвестная операция {kind}", 1)

    argv = build_argv(kind, options) if kind in OPTIONS else list(options or [])
    buf = io.StringIO()

    class _Sink(io.StringIO):
        def write(self_inner, s: str) -> int:
            buf.write(s)
            if on_line:
                for line in s.splitlines():
                    line = _ANSI.sub("", line).strip()
                    if line:
                        try:
                            on_line(line)
                        except Exception:
                            pass
            return len(s)

    class _Handler(logging.Handler):
        def emit(self_inner, record: logging.LogRecord) -> None:
            try:
                text = self_inner.format(record)
            except Exception:
                return
            buf.write(text + "\n")
            if on_line:
                try:
                    on_line(_ANSI.sub("", text).strip())
                except Exception:
                    pass

    handler = _Handler(logging.INFO)
    pkg_logger = logging.getLogger(ENGINE_PACKAGE)
    pkg_logger.addHandler(handler)

    code = 0
    try:
        mod = import_module(f"{ENGINE_PACKAGE}.operations.{module_name}")
        op = mod.Operation()
        parser = argparse.ArgumentParser(add_help=False)
        op.setup_parser(parser)
        ns_cls = getattr(mod, "Namespace", argparse.Namespace)
        try:
            args = parser.parse_args(argv, namespace=ns_cls())
        except SystemExit:
            return ("Движок не принял параметры: " + " ".join(argv), 2)
        cancel = threading.Event()
        args._cancel_event = cancel
        op._cancel_event = cancel
        global _current_cancel_event
        _current_cancel_event = cancel
        with redirect_stdout(_Sink()):
            try:
                op.run(tool, args)
            except TypeError:
                op.run(tool)
    except Exception as exc:
        code = 1
        buf.write(f"\nОшибка: {exc}")
        logger.exception("операция %s", kind)
    finally:
        _current_cancel_event = None
        pkg_logger.removeHandler(handler)

    return (_ANSI.sub("", buf.getvalue()).strip(), code)
