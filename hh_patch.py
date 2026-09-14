# -*- coding: utf-8 -*-
"""Правки поведения hh-applicant-tool. Общие для Telegram-бота и десктопной апки.

Патч 1 — стоп-слова матчим ТОЛЬКО по названию вакансии.
    Штатная реализация ищет совпадение в названии + сниппете + полном тексте
    описания со страницы вакансии. На практике это режет по живому: слова вроде
    «продаж», «монтаж», «продюсер» встречаются в описании почти любой вакансии
    из смежной области. 09.09.2026 так отсеялось 246 вакансий, из них 197
    были нормальными — «PR-менеджер», «Шеф-редактор» и подобные.
    Побочная польза: больше не грузим страницу каждой вакансии, прогон быстрее.

Патч 2 — при --dry-run не трогаем чёрный список на hh.ru.
    Штатно вакансия улетает в blacklist даже в холостом прогоне, то есть
    «посмотреть, что отсеется» необратимо меняет аккаунт.

Патч 3 — окно входа по размеру экрана (иначе капча уезжает за край).

Патч 4 — случайная пауза между запросами к API.
    Штатно интервал фиксированный (DEFAULT_DELAY = 0.345), то есть запросы
    идут с машинной регулярностью. Заменяем на случайную величину из диапазона.

Патч 5 — пинги не повторяются в рамках одного чата.
    Движок разворачивает шаблон случайно и может дважды прислать работодателю
    один и тот же текст. Смотрим историю переписки и выбираем то, чего там ещё
    не было.
"""
from __future__ import annotations

import logging
import random
import re
import sys

logger = logging.getLogger("hh_applicant_tool")

_applied = False

# Границы случайной паузы между запросами, секунды.
DELAY_RANGE = (0.8, 2.4)


def _is_excluded_by_name(self, vacancy) -> bool:
    """Стоп-слова только по названию вакансии."""
    pattern = getattr(self, "excluded_filter", None)
    if not pattern:
        return False
    name = (vacancy or {}).get("name") or ""
    if not name:
        return False
    try:
        rx = getattr(self, "_hh_patch_rx", None)
        if rx is None or rx.pattern != pattern:
            rx = re.compile(pattern, re.IGNORECASE)
            self._hh_patch_rx = rx
    except re.error as exc:
        logger.warning("Стоп-фильтр не компилируется (%s), пропускаю фильтрацию", exc)
        return False
    return bool(rx.search(name))


def apply_patches(memory_path=None) -> None:
    """memory_path — файл, куда писать уже отправленные пинги (по профилю)."""
    global _applied
    if _applied:
        return

    from hh_applicant_tool.operations import apply_vacancies as av

    # --- патч 1
    av.Operation._is_excluded = _is_excluded_by_name

    # --- патч 2: blacklist не трогаем в холостом прогоне
    original_run = av.Operation.run

    def run_with_dry_guard(self, tool, args):
        if getattr(args, "dry_run", False):
            client = tool.api_client
            if not getattr(client, "_hh_patch_dry", False):
                real_put = client.put

                def guarded_put(endpoint, *a, **kw):
                    if "blacklisted" in str(endpoint):
                        logger.info("dry-run: пропускаю чёрный список %s", endpoint)
                        return {}
                    return real_put(endpoint, *a, **kw)

                client.put = guarded_put
                client._hh_patch_dry = True
        return original_run(self, tool, args)

    av.Operation.run = run_with_dry_guard

    for fn, name in (
        (patch_auth_window, "окно входа"),
        (patch_api_delay, "случайная пауза"),
        (lambda: patch_ping_variety(memory_path), "разнообразие пингов"),
        (patch_sqlite_types, "безопасные типы sqlite"),
    ):
        try:
            fn()
        except Exception:
            logger.debug("не применился патч: %s", name, exc_info=True)

    _applied = True
    logger.debug("hh_patch: патчи применены")


# ------------------------------------------------------------- sqlite types

def patch_sqlite_types() -> None:
    """Патч 6 — безопасное сохранение в SQLite.
    
    Движок пытается записать словари/списки из HH API в SQLite напрямую
    (например, поле type у EmployerModel). Это вызывает InterfaceError и 
    ломает текущую транзакцию, порождая каскад ошибок (OperationalError).
    Перехватываем to_db и принудительно дампим несовместимые типы.
    """
    try:
        from hh_applicant_tool.storage.models.base import BaseModel
    except ImportError:
        return

    if getattr(BaseModel, "_hh_types_patched", False):
        return

    orig_to_db = BaseModel.to_db

    def patched_to_db(self):
        data = orig_to_db(self)
        for k, v in list(data.items()):
            if isinstance(v, (dict, list)):
                import json
                data[k] = json.dumps(v, ensure_ascii=False)
            elif hasattr(v, "isoformat"):
                data[k] = v.isoformat()
        return data

    BaseModel.to_db = patched_to_db
    BaseModel._hh_types_patched = True
    logger.debug("hh_patch: безопасные типы SQLite включены")


# ------------------------------------------------------- случайная пауза

def patch_api_delay(low: float | None = None, high: float | None = None) -> None:
    """Заменяет фиксированный интервал между запросами на случайный.

    В BaseClient.request пауза берётся из self.delay, если аргумент delay не
    передан. Подменяем сам метод: когда вызывающий код не указал паузу явно,
    подставляем случайную из диапазона. Явные значения (движок кое-где уже
    ставит random.uniform(1, 3)) не трогаем.
    """
    from hh_applicant_tool.api.client import BaseClient

    if getattr(BaseClient, "_hh_delay_patched", False):
        return

    lo, hi = (low, high) if (low and high) else DELAY_RANGE
    if lo > hi:
        lo, hi = hi, lo
    original = BaseClient.request

    def request(self, method, endpoint, params=None, delay=None,
                as_json=False, **kwargs):
        if delay is None:
            delay = random.uniform(lo, hi)
        return original(self, method, endpoint, params, delay, as_json, **kwargs)

    BaseClient.request = request
    BaseClient._hh_delay_patched = True
    logger.debug("hh_patch: пауза между запросами %.2f–%.2f с", lo, hi)


# --------------------------------------------------------- пинги без повторов

def patch_ping_variety(memory_path=None) -> None:
    """Пинг не должен дважды прийти в один чат одним и тем же текстом.

    В reply_employers текст выбирается строкой
        send_message = rand_text(self.reply_message) % placeholders
    внутри цикла по откликам. В этом же кадре лежат nid (идентификатор чата)
    и message_history (вся переписка). Подменяем rand_text на обёртку, которая
    достаёт их из кадра вызывающего и выбирает вариант, которого в переписке
    ещё не было. Если что-то пошло не так — работаем как штатная функция.
    """
    from hh_applicant_tool.operations import reply_employers as rm

    if getattr(rm, "_hh_ping_patched", False):
        return

    import letters

    if memory_path:
        letters.set_memory_path(memory_path)

    original = rm.rand_text

    def rand_text(template):
        history = nid = None
        try:
            frame = sys._getframe(1)
            history = frame.f_locals.get("message_history")
            nid = frame.f_locals.get("nid")
        except Exception:
            pass
        if history is None and nid is None:
            return original(template)
        try:
            return letters.pick_unique(template, history, key=nid)
        except Exception:
            logger.debug("pick_unique не сработал", exc_info=True)
            return original(template)

    rm.rand_text = rand_text
    rm._hh_ping_patched = True
    logger.debug("hh_patch: пинги без повторов включены")


# --------------------------------------------------------------- окно входа

def _screen_size() -> tuple[int, int]:
    """Размер основного экрана. При неудаче — консервативные значения."""
    import sys
    if sys.platform == "win32":
        try:
            import ctypes

            user32 = ctypes.windll.user32
            try:
                user32.SetProcessDPIAware()
            except Exception:
                pass
            return int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))
        except Exception:
            return 1920, 1080
    return 1920, 1080


def _auth_window_geometry() -> tuple[int, int]:
    """Ширина и высота окна входа, влезающие в экран с запасом."""
    _, screen_h = _screen_size()
    height = max(620, min(1000, screen_h - 160))
    return 520, height


def patch_auth_window() -> None:
    """Заставляет playwright открывать окно входа по размеру экрана."""
    from playwright.async_api import Browser, BrowserType

    if getattr(BrowserType, "_hh_window_patched", False):
        return

    width, height = _auth_window_geometry()
    orig_launch = BrowserType.launch
    orig_new_context = Browser.new_context

    async def launch(self, **kwargs):
        args = list(kwargs.get("args") or [])
        if not any(a.startswith("--window-size") for a in args):
            args.append(f"--window-size={width},{height}")
            args.append("--window-position=80,40")
        kwargs["args"] = args
        return await orig_launch(self, **kwargs)

    async def new_context(self, **kwargs):
        # user_agent, is_mobile и has_touch оставляем как есть —
        # от них зависит вёрстка hh и сам OAuth-редирект
        kwargs["viewport"] = {"width": width - 40, "height": height - 120}
        kwargs["screen"] = {"width": width, "height": height}
        kwargs["device_scale_factor"] = 1
        return await orig_new_context(self, **kwargs)

    BrowserType.launch = launch
    Browser.new_context = new_context
    BrowserType._hh_window_patched = True
    logger.debug("hh_patch: окно входа %sx%s", width, height)
