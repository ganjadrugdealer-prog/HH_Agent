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

    Заодно холостой прогон не шлёт писем на email и показывает в ленте,
    на что откликнулся бы: штатно он молчит, и минутами кажется, что
    ничего не происходит.

Патч 3 — окно входа по размеру экрана (иначе капча уезжает за край).
    Там же страница входа не ждёт рекламных счётчиков hh.ru (patch_auth_page_load).

Патч 4 — случайная пауза между запросами к API.
    Штатно интервал фиксированный (DEFAULT_DELAY = 0.345), то есть запросы
    идут с машинной регулярностью. Заменяем на случайную величину из диапазона.
    Там же пауза между самими откликами (patch_application_gap): на 6 откликах
    за 25 секунд hh.ru требует капчу.

Патч 5 — пинги не повторяются в рамках одного чата.
    Движок разворачивает шаблон случайно и может дважды прислать работодателю
    один и тот же текст. Смотрим историю переписки и выбираем то, чего там ещё
    не было.

Патч 8 — капча: перехват в клиенте API (перенесён из hh-applicant-tool 2.x)
    и окно для ввода руками (patch_manual_captcha). Рассылка ждёт и повторяет
    запрос; с ключом OpenAI капча при отклике решается штатно.
"""
from __future__ import annotations

import json
import logging
import random
import re
import sys
import threading
import time
from urllib.parse import urlsplit

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

    for fn, name in (
        (patch_excluded_by_name, "стоп-слова по названию"),
        (patch_dry_run_blacklist, "чёрный список в dry-run"),
        (patch_auth_window, "окно входа"),
        (patch_auth_page_load, "загрузка страницы входа"),
        (patch_manual_captcha, "капча руками"),
        (patch_api_delay, "случайная пауза"),
        (patch_application_gap, "пауза между откликами"),
        (lambda: patch_ping_variety(memory_path), "разнообразие пингов"),
        (patch_sqlite_types, "безопасные типы sqlite"),
    ):
        try:
            fn()
        except Exception:
            # Раньше было debug — сломанный патч уходил в тишину.
            logger.warning("не применился патч: %s", name, exc_info=True)

    _applied = True
    logger.debug("hh_patch: патчи применены")


def patch_excluded_by_name() -> None:
    """Патч 1 — стоп-слова только по названию вакансии."""
    from hh_applicant_tool.operations import apply_vacancies as av

    av.Operation._is_excluded = _is_excluded_by_name


DRY_RUN_MARK = "🧪 Подошла бы вакансия"
_TRY_APPLY = "Пробуем откликнуться на вакансию"


class _DryRunEcho(logging.Filter):
    """Отладочное «Пробуем откликнуться» ядра → видимая строка в ленте.

    print внутри прогона перехватывается штатным Api и уходит в ленту,
    как и собственные строки ядра «📨 Отправили отклик».
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if str(record.msg).startswith(_TRY_APPLY) and record.args:
            args = record.args if isinstance(record.args, tuple) else (record.args,)
            print(DRY_RUN_MARK, args[0])
        return True


def patch_dry_run_blacklist() -> None:
    """Патч 2 — в холостом прогоне не трогаем чёрный список на hh.ru."""
    from hh_applicant_tool.operations import apply_vacancies as av

    if getattr(av.Operation, "_hh_dry_patched", False):
        return

    original_run = av.Operation.run

    def run_with_dry_guard(self, tool, args):
        if not getattr(args, "dry_run", False):
            return original_run(self, tool, args)
        # Клиент API у движка один на весь процесс (cached_property), поэтому
        # подмену put снимаем после прогона. Раньше она оставалась навсегда,
        # и после первого холостого прогона боевые тоже не трогали ЧС.
        client = tool.api_client
        own = client.__dict__
        had_own, prev = "put" in own, own.get("put")
        real_put = client.put

        def guarded_put(endpoint, *a, **kw):
            if "blacklisted" in str(endpoint):
                logger.info("dry-run: пропускаю чёрный список %s", endpoint)
                return {}
            return real_put(endpoint, *a, **kw)

        client.put = guarded_put
        # письма на email ядро отправляет без оглядки на --dry-run
        args.send_email = False
        echo = _DryRunEcho()
        av.logger.addFilter(echo)
        try:
            return original_run(self, tool, args)
        finally:
            av.logger.removeFilter(echo)
            if had_own:
                client.put = prev
            else:
                own.pop("put", None)

    av.Operation.run = run_with_dry_guard
    av.Operation._hh_dry_patched = True


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
            if isinstance(v, (dict, list, tuple, set)):
                data[k] = json.dumps(
                    list(v) if isinstance(v, (tuple, set)) else v,
                    ensure_ascii=False,
                )
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


# ------------------------------------------------- пауза между откликами

# Пауза между откликами, секунды. 17.10.2026 hh.ru потребовал капчу после
# 6 откликов за 25 секунд. ponytail: точного порога hh не публикует,
# границы подбираются на практике; 200 откликов при ~30 с — около 1,5–2 часов.
APPLICATION_GAP = (20.0, 45.0)

# Событие отмены текущей рассылки — по потоку, где она идёт.
_gap_local = threading.local()
# Когда и на что ушёл последний отклик.
_gap_last = {"t": 0.0, "key": None}


def patch_application_gap() -> None:
    """Отклики не чаще раза в APPLICATION_GAP секунд.

    Штатная пауза перед откликом — 1–3 с, и это минимальный зазор от любого
    предыдущего запроса, а перед откликом всегда идёт ещё GET работодателя,
    так что отклики летят каждые 3–4 с. Ждём от конца прошлого отклика —
    и обычного (POST /negotiations в API), и с тестом (форма на сайте).
    Повтор того же отклика — после капчи или обновления токена — не ждёт.

    «Стоп» во время паузы: отклик не уходит, движок получает ошибку, пишет
    её в журнал и на следующей вакансии видит отмену.
    """
    from hh_applicant_tool.api import errors
    from hh_applicant_tool.api.client import BaseClient
    from hh_applicant_tool.operations import apply_vacancies as av

    if getattr(BaseClient, "_hh_gap_patched", False):
        return

    class Cancelled(errors.ClientError):
        def __init__(self) -> None:
            super().__init__(None, {})

        @property
        def message(self) -> str:
            return "Рассылку остановили, отклик не отправлен"

    def paced(key, send):
        if key != _gap_last["key"]:
            wait = _gap_last["t"] + random.uniform(*APPLICATION_GAP) - time.monotonic()
            if wait > 0:
                logger.info("Пауза перед откликом %.0f с", wait)
                cancel = getattr(_gap_local, "cancel", None)
                if cancel is None:
                    time.sleep(wait)
                elif cancel.wait(wait):
                    raise Cancelled()
        try:
            return send()
        finally:
            _gap_last.update(t=time.monotonic(), key=key)

    orig_request = BaseClient.request
    orig_test = av.Operation._solve_vacancy_test
    orig_run = av.Operation.run

    # Сигнатура как у BaseClient.request: патч 4 и ApiClient зовут позиционно.
    def request(self, method, endpoint, params=None, delay=None,
                as_json=False, **kwargs):
        def send():
            return orig_request(self, method, endpoint, params, delay,
                                as_json, **kwargs)

        if method != "POST" or endpoint != "/negotiations":
            return send()
        p = params or {}
        return paced((p.get("vacancy_id"), p.get("resume_id")), send)

    def solve_vacancy_test(self, vacancy_id, resume_hash, *args, **kwargs):
        return paced((vacancy_id, resume_hash), lambda: orig_test(
            self, vacancy_id, resume_hash, *args, **kwargs))

    def run(self, tool, *args, **kwargs):
        _gap_local.cancel = getattr(self, "_cancel_event", None)
        try:
            return orig_run(self, tool, *args, **kwargs)
        finally:
            _gap_local.cancel = None

    BaseClient.request = request
    av.Operation._solve_vacancy_test = solve_vacancy_test
    av.Operation.run = run
    BaseClient._hh_gap_patched = True
    logger.debug("hh_patch: пауза между откликами %.0f–%.0f с", *APPLICATION_GAP)


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


# ------------------------------------------------- загрузка страницы входа

# Чего ждать при открытии страницы входа вместо полной загрузки.
AUTH_WAIT_UNTIL = "domcontentloaded"


def patch_auth_page_load() -> None:
    """Патч 7 — страница входа не ждёт рекламных счётчиков.

    Движок открывает OAuth-страницу с wait_until="load" и таймаутом 60 с.
    На hh.ru висят сторонние пиксели (skcrtxr.com, weborama), которые 12.10.2026
    не отвечали вовсе: событие load приходило через 46 с, а с VPN — никогда,
    и вход падал с «Timeout 60000ms exceeded». Ждём только DOM: форма уже
    на месте, а в ручном режиме дальше ждёт сам человек.
    Заодно с VPN окно больше не закрывается молча — человек видит страницу
    hh.ru «VPN мешает работе сайта».
    """
    from playwright.async_api import Page

    if getattr(Page, "_hh_goto_patched", False):
        return

    orig_goto = Page.goto

    async def goto(self, url, **kwargs):
        if "/oauth/authorize" in str(url) and kwargs.get("wait_until") in (None, "load"):
            kwargs["wait_until"] = AUTH_WAIT_UNTIL
        return await orig_goto(self, url, **kwargs)

    Page.goto = goto
    Page._hh_goto_patched = True
    logger.debug("hh_patch: страница входа ждёт %s", AUTH_WAIT_UNTIL)


# ------------------------------------------------------------ капча руками
#
# Из hh-applicant-tool 2.x перенесён перехват в ApiClient.request: капча
# ловится на любом запросе рассылки, запрос повторяется после решения, а не
# роняет прогон на второй капче подряд.
#
# Само окно — как раньше: обычный браузер с куками аккаунта. Способ 2.x
# (чистая сессия, User-Agent Android WebView, backurl на hh://) в настоящем
# браузере не работает: 17.10.2026 hh на живом аккаунте отвечал «Неверный
# текст» на верно введённую капчу — UA Android не сходится с остальными
# признаками десктопного Chromium. 2.x ходит через requests, где их нет.

# Сколько ждать, пока человек введёт капчу, секунды.
CAPTCHA_WAIT_S = 600
# Пауза после принятой капчи перед повтором запроса (как в 2.x).
CAPTCHA_COOLDOWN_S = 5.0
# Сколько капч подряд решаем на один запрос, прежде чем сдаться (как в 2.x).
MAX_CAPTCHA_ROUNDS = 5
_CAPTCHA_PATH = "/account/captcha"


def _has_captcha_ai(tool) -> bool:
    section = (tool.config.get("openai_captcha") or {}) if tool.config else {}
    return bool(section.get("api_key") and section.get("base_url"))


def _session_cookies_for_browser(session) -> list[dict]:
    """Веб-куки hh.ru из сессии движка — чтобы капча открылась под аккаунтом."""
    return [{"name": c.name, "value": c.value, "domain": c.domain,
             "path": c.path or "/", "secure": bool(c.secure)}
            for c in session.cookies if "hh.ru" in (c.domain or "")]


def _store_browser_cookies(session, cookies: list[dict]) -> None:
    """Куки окна обратно в сессию движка.

    Банка кук движка — HHOnlyCookieJar (http.cookiejar), у неё нет .set из
    requests, только set_cookie: штатный код и v1.2.5 падали здесь с
    «'HHOnlyCookieJar' object has no attribute 'set'» сразу после капчи.
    """
    from requests.cookies import create_cookie

    for c in cookies:
        session.cookies.set_cookie(create_cookie(
            c["name"], c["value"], domain=c.get("domain", ""),
            path=c.get("path", "/"), secure=bool(c.get("secure"))))


def _is_captcha_accepted(method: str, url: str, status: int, location: str) -> bool:
    """Ответ на отправку капчи: 302 не на страницу капчи или 203 — принята.

    Неверный ответ — 403, а при отправке формой — 302 обратно на failurl
    (/account/captcha?state=...).
    """
    if method != "POST" or urlsplit(url).path != _CAPTCHA_PATH:
        return False
    return status == 203 or (status == 302 and _CAPTCHA_PATH not in location)


async def _manual_captcha(captcha_url: str, session, proxy_url: str | None = None,
                          cancel=None) -> bool:
    """Показывает капчу в окне браузера и ждёт, пока человек её введёт.

    Успех — hh принял ответ (_is_captcha_accepted) или страница ушла с
    /account/captcha. Неверный ответ hh показывает новой картинкой на том же
    адресе — ждём дальше. Закрыли окно, остановили рассылку или вышло
    время — False. После успеха куки окна уходят в сессию движка.
    """
    import asyncio
    from playwright.async_api import async_playwright

    args = [f"--proxy-server={proxy_url}"] if proxy_url else []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, args=args)
        try:
            context = await browser.new_context()
            cookies = _session_cookies_for_browser(session)
            if cookies:
                try:
                    await context.add_cookies(cookies)
                except Exception:
                    logger.debug("hh_patch: куки в окно капчи не легли", exc_info=True)
            page = await context.new_page()
            closed = asyncio.Event()
            accepted = asyncio.Event()
            page.on("close", lambda _: closed.set())
            browser.on("disconnected", lambda _: closed.set())

            def on_response(response) -> None:
                req = response.request
                if req.method == "POST" and urlsplit(response.url).path == _CAPTCHA_PATH:
                    # по журналу видно, принял hh ответ или отбил
                    logger.info("hh ответил на капчу: %s", response.status)
                location = response.headers.get("location", "")
                if _is_captcha_accepted(req.method, response.url,
                                        response.status, location):
                    accepted.set()

            page.on("response", on_response)

            await page.goto(captcha_url, timeout=60000, wait_until=AUTH_WAIT_UNTIL)
            logger.warning("🧩 Введите капчу в открывшемся окне hh.ru — рассылка ждёт")

            loop = asyncio.get_running_loop()
            deadline = loop.time() + CAPTCHA_WAIT_S
            while loop.time() < deadline:
                if accepted.is_set() or _CAPTCHA_PATH not in page.url:
                    try:
                        await page.wait_for_load_state(AUTH_WAIT_UNTIL, timeout=10000)
                    except Exception:
                        pass
                    _store_browser_cookies(session, await context.cookies())
                    logger.info("Капча пройдена, продолжаю рассылку")
                    return True
                if closed.is_set():
                    logger.warning("Окно капчи закрыто, капча не пройдена")
                    return False
                if cancel is not None and cancel.is_set():
                    logger.warning("Рассылку остановили, окно капчи закрываю")
                    return False
                await asyncio.sleep(0.5)
            logger.warning("Капчу не ввели за %s мин", CAPTCHA_WAIT_S // 60)
            return False
        finally:
            try:
                await browser.close()
            except Exception:
                pass


def patch_manual_captcha() -> None:
    """Патч 8 — капча ловится в клиенте API на время рассылки откликов.

    Как в 2.x: CaptchaRequired на любом запросе -> окно -> пауза -> тот же
    запрос заново, до MAX_CAPTCHA_ROUNDS капч подряд. Штатно 1.8.28 ловил
    капчу только при отклике, повторял его один раз и на второй капче подряд
    обрывал прогон.

    Перехват включён только в потоке, где идёт рассылка, и только без ключа
    OpenAI: запросы интерфейса и Telegram-бота, пинги и поднятие резюме
    падают на капче сразу, как раньше, а с ключом капчу при отклике, как
    раньше, решает штатный AI-решатель движка.

    Окно закрыли, не открылось, вышло время или нажали «Стоп» — рассылка
    останавливается через событие отмены операции, а не открывает окно на
    каждой следующей вакансии.
    """
    import asyncio

    from hh_applicant_tool.api import errors
    from hh_applicant_tool.api.client import ApiClient
    from hh_applicant_tool.operations import apply_vacancies as av

    if getattr(ApiClient, "_hh_captcha_patched", False):
        return

    class CaptchaNotSolved(errors.ClientError):
        """Не CaptchaRequired: обработчик капчи движка его не трогает, а
        обработчик ApiError на вакансии просто пишет message в журнал."""

        @property
        def message(self) -> str:
            return "Капча не пройдена, рассылка остановлена"

    orig_request = ApiClient.request
    orig_run = av.Operation.run

    def request(self, *args, **kwargs):
        for attempt in range(MAX_CAPTCHA_ROUNDS + 1):
            try:
                return orig_request(self, *args, **kwargs)
            except errors.CaptchaRequired as ex:
                armed = self._hh_captcha
                if not armed or armed[0] != threading.get_ident():
                    raise
                cancel = armed[1]
                if cancel.is_set() or attempt == MAX_CAPTCHA_ROUNDS:
                    cancel.set()
                    raise CaptchaNotSolved(ex._response, ex.data) from ex
                logger.warning("Требуется капча: %s", ex.captcha_url)
                proxy_url = (self.proxies or {}).get("https")
                try:
                    solved = asyncio.run(
                        _manual_captcha(ex.captcha_url, self.session, proxy_url, cancel))
                except Exception:
                    logger.exception("Окно капчи не открылось")
                    solved = False
                # пауза перед повтором; «Стоп» во время неё тоже считается
                if not solved or cancel.wait(CAPTCHA_COOLDOWN_S):
                    cancel.set()
                    raise CaptchaNotSolved(ex._response, ex.data) from ex

    def run(self, tool, *args, **kwargs):
        if _has_captcha_ai(tool):
            return orig_run(self, tool, *args, **kwargs)
        cancel = getattr(self, "_cancel_event", None)
        if cancel is None:
            cancel = self._cancel_event = threading.Event()
        tool.api_client._hh_captcha = (threading.get_ident(), cancel)
        try:
            return orig_run(self, tool, *args, **kwargs)
        except CaptchaNotSolved as ex:
            # капча на поиске вакансий — вне обработчика ошибок вакансии
            logger.warning(ex)
        finally:
            tool.api_client.__dict__.pop("_hh_captcha", None)

    ApiClient._hh_captcha = None
    ApiClient.request = request
    av.Operation.run = run
    ApiClient._hh_captcha_patched = True
    logger.debug("hh_patch: перехват капчи включён")
