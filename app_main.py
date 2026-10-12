# -*- coding: utf-8 -*-
"""HH Agent — точка входа для сборки в exe.

Окно одно. При первом запуске в нём открывается мастер настройки, дальше —
штатный интерфейс hh-applicant-tool. Данные (config.json, база, куки, логи)
лежат в подпапке data рядом с exe, приложение портативное.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

APP_NAME = "HH Agent"
APP_VERSION = "1.2.5"


def _pin_browsers_path() -> None:
    """Держим браузеры playwright в одном месте на всю систему.

    В собранном виде playwright по умолчанию смотрит внутрь распакованного
    _MEIPASS — а он удаляется при выходе. Прибиваем путь явно, до импорта
    playwright, иначе переменная уже не подействует.
    """
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        return
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        path = Path(base) / "ms-playwright"
    else:
        path = Path.home() / ".cache" / "ms-playwright"
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(path)


_pin_browsers_path()


def app_dir() -> Path:
    """Папка, где лежит exe (или скрипт при обычном запуске)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def home_dir() -> Path:
    """Куда приложению можно писать: data, selftest.log, crash.log.

    На Windows и при запуске из исходников — рядом с exe/скриптом,
    приложение портативное. На macOS рядом с исполняемым файлом — это
    внутренность HH-Agent.app: при обновлении бандл заменяется вместе с
    профилями и токенами, а запущенный из .dmg или из «Загрузок» (App
    Translocation) он вообще только для чтения. Поэтому на маке —
    ~/Library/Application Support/HH_Agent, рядом с ядром.
    """
    if sys.platform == "darwin" and getattr(sys, "frozen", False):
        d = Path.home() / "Library" / "Application Support" / "HH_Agent"
        d.mkdir(parents=True, exist_ok=True)
        return d
    return app_dir()


def resource_dir() -> Path:
    """Наши ресурсы: в сборке это распакованный _MEIPASS."""
    base = getattr(sys, "_MEIPASS", None)
    return (Path(base) if base else Path(__file__).resolve().parent) / "ui"


def data_root() -> Path:
    """Корень данных приложения (общий для всех профилей)."""
    d = home_dir() / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def active_profile_dir() -> Path:
    """Папка активного профиля. Создаёт профиль по умолчанию при первом запуске."""
    import profiles

    root = data_root()
    state = profiles.ensure(root)
    return profiles.profile_dir(root, state["active"])


def restart_env() -> dict:
    """Окружение для нового экземпляра приложения.

    Без PYINSTALLER_RESET_ENVIRONMENT дочерний onefile-процесс считает себя
    частью текущего и берёт его распакованный _MEIPASS. Текущий процесс
    сразу закрывается и удаляет эту папку — новый падает с
    «Path .../_MEIxxxx does not exist», и после установки ядра приложение
    просто исчезает. Воспроизведено на macOS.
    """
    env = dict(os.environ)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def restart_app() -> None:
    """Перезапускает приложение (после установки ядра и смены профиля)."""
    try:
        if getattr(sys, "frozen", False):
            cmd = [sys.executable]
        else:
            cmd = [sys.executable, str(Path(__file__).resolve())]
        subprocess.Popen(cmd, cwd=str(home_dir()), close_fds=True,
                         env=restart_env())
    except Exception:
        import traceback

        traceback.print_exc()
    try:
        import webview

        for w in list(getattr(webview, "windows", [])):
            try:
                w.destroy()
            except Exception:
                pass
    except Exception:
        pass


# --------------------------------------------------------------------- окно


def create_window(tool, *, debug: bool = False) -> None:
    """Замена штатной hh_applicant_tool.ui.create_window.

    Интерфейс автора движка больше не открывается: у приложения свой,
    из пяти экранов. Утилита к этому моменту уже подняла конфиг, базу
    и логгер — нам остаётся выбрать стартовую страницу и подсунуть свой API.
    """
    import webview

    import hh_patch

    from app_api import SETUP_KEY, AppApi, chromium_installed

    hh_patch.apply_patches(
        memory_path=Path(tool.config_path) / "ping_memory.json"
    )

    main_url = (resource_dir() / "app.html").as_uri()
    wizard_url = (resource_dir() / "wizard.html").as_uri()

    setup_done = bool((tool.config.get(SETUP_KEY) or {}).get("completed"))
    ready = bool(tool.config.get("token") or {}) and chromium_installed()
    first_run = not (setup_done or ready)

    api = AppApi(tool, main_url, resource_dir(), data_root=data_root(),
                 restart=restart_app)

    start_url = wizard_url if first_run else main_url
    if "--wizard" in sys.argv[1:]:
        start_url = wizard_url

    window = webview.create_window(
        title=f"{APP_NAME} {APP_VERSION}",
        url=start_url,
        js_api=api,
        width=1180,
        height=820,
        min_size=(940, 620),
        text_select=True,
    )
    api.set_window(window)

    if "--demo-run" in sys.argv[1:]:
        import threading

        threading.Thread(target=demo_worker, args=(api, tool), daemon=True).start()

    try:
        import telegram_bot
        telegram_bot.start_bot_thread(api, tool)
    except Exception:
        import logging
        logging.getLogger("hh_agent").exception("Failed to start telegram bot thread")

    webview.start(debug=debug, http_server=True)


DEMO_VACANCIES = [
    (901, "PR-менеджер в технологический холдинг", 180000, 220000, "Москва", 0),
    (902, "Шеф-редактор спецпроектов", 250000, 250000, "Москва", 1),
    (903, "Контент-продюсер YouTube", 120000, None, "Санкт-Петербург", 0),
    (904, "Менеджер по продажам B2B", 90000, 150000, "Москва", 0),
    (905, "Старший специалист по коммуникациям", 200000, None, "Москва", 1),
    (906, "Редактор корпоративных медиа", None, 160000, "Казань", 1),
    (907, "Стажёр отдела маркетинга", 40000, None, "Москва", 0),
]

DEMO_SCRIPT = [
    (0.3, "🚀 Начинаю рассылку откликов для резюме: Старший PR-менеджер"),
    (0.6, "📨 Отправили отклик на вакансию https://hh.ru/vacancy/901"),
    (0.7, "Вакансия попала под фильтр: https://hh.ru/vacancy/904"),
    (0.4, "Вакансия добавлена в черный список: https://hh.ru/vacancy/904"),
    (0.6, "📨 Отправили отклик на вакансию https://hh.ru/vacancy/902"),
    (0.5, "🧠 AI (light) посчитал неподходящей https://hh.ru/vacancy/903"),
    (0.5, "⏩ Вакансия уже отклонена ранее https://hh.ru/vacancy/906"),
    (0.6, "📨 Отправили отклик на вакансию с тестом https://hh.ru/vacancy/905"),
    (0.4, "Вакансия попала под фильтр: https://hh.ru/vacancy/907"),
    (0.5, "⛔ Пришел отказ от https://hh.ru/vacancy/906"),
    (0.4, "[E] Произошла непредвиденная ошибка: Авторизация истекла требуется новая!"),
    (0.5, "✅️ Закончили рассылку для резюме: Старший PR-менеджер. Отправлено: 3"),
    (0.4, "📝 Отклики на вакансии разосланы!"),
]


def demo_seed(tool) -> None:
    """Наливает несколько вакансий в свою же базу, чтобы карточки были с названиями."""
    try:
        for vid, name, sfrom, sto, area, remote in DEMO_VACANCIES:
            tool.db.execute(
                "INSERT OR REPLACE INTO vacancies"
                " (id, name, salary_from, salary_to, currency, area_name, remote,"
                "  alternate_url)"
                " VALUES (?,?,?,?,'RUR',?,?,?)",
                (vid, name, sfrom, sto, area, remote,
                 f"https://hh.ru/vacancy/{vid}"),
            )
        tool.db.commit()
    except Exception:
        import traceback

        traceback.print_exc()


def demo_worker(api, tool) -> None:
    """Прогон-пустышка: показывает ленту и отчёт, никуда не ходит."""
    import time

    time.sleep(2.0)
    demo_seed(tool)
    api.monitor.open_window(api, {"demo": True})
    time.sleep(2.0)
    for pause, line in DEMO_SCRIPT:
        api.monitor.feed(line)
        time.sleep(pause)
    time.sleep(0.5)
    api.monitor.finish("ok")


# ------------------------------------------------------------ служебные режимы


def install_browser_mode() -> int:
    """Скачивает chromium. Запускается дочерним процессом из мастера."""
    from runpy import run_module

    orig = sys.argv
    sys.argv = ["playwright", "install", "chromium"]
    try:
        run_module("playwright", run_name="__main__")
    except SystemExit as exc:
        return int(exc.code or 0)
    finally:
        sys.argv = orig
    return 0


def selftest() -> int:
    """Диагностика собранного exe: что доехало в сборку, а что нет.

    Пишем и в консоль, и в selftest.log рядом с exe — в релизной сборке
    консоли нет, а результат нужен.
    """
    lines: list[str] = []

    def say(*args) -> None:
        text = " ".join(str(a) for a in args)
        lines.append(text)
        print(text)

    ok = True
    say("version     :", APP_VERSION)
    say("frozen      :", getattr(sys, "frozen", False))
    say("app_dir     :", app_dir())
    say("data        :", data_root())

    try:
        from pkgutil import iter_modules

        import hh_applicant_tool

        ops = Path(hh_applicant_tool.__file__).resolve().parent / "operations"
        names = sorted(m for _, m, _ in iter_modules([str(ops)]))
        say(f"operations  : {len(names)}")
        if "ui" not in names or "authorize" not in names:
            ok = False
            say("  !! команды не зарегистрировались")
    except Exception as exc:
        ok = False
        say("operations  : FAIL", exc)

    try:
        from hh_applicant_tool.ui import TEMPLATES_DIR

        idx = (TEMPLATES_DIR / "index.html").exists()
        say("templates   :", "ok" if idx else "MISSING")
        ok = ok and idx
    except Exception as exc:
        ok = False
        say("templates   : FAIL", exc)

    pages = {}
    for name in ("app.html", "wizard.html", "run.html", "loader.html", "theme.js"):
        pages[name] = (resource_dir() / name).exists()
    try:
        import profiles

        st = profiles.ensure(data_root())
        say("profiles    : ok, активный:", st["active"], "| всего:", len(st["profiles"]))
    except Exception as exc:
        ok = False
        say("profiles    : FAIL", exc)
    for name, exists in pages.items():
        say(f"{name:12}:", "ok" if exists else "MISSING")
        ok = ok and exists
    say("ui dir      :", resource_dir())

    try:
        import hh_patch
        from run_monitor import classify

        hh_patch.apply_patches()
        from hh_applicant_tool.operations import apply_vacancies as _av

        patched = _av.Operation._is_excluded is hh_patch._is_excluded_by_name
        say("hh_patch    :", "ok (фильтр по названию)" if patched else "НЕ ПРИМЕНЁН")
        ok = ok and patched

        import stopwords

        pat = stopwords.to_pattern("продажи, монтаж видео")
        import re as _re

        good = bool(_re.search(pat, "Менеджер по продажам", _re.I)) and \
               not bool(_re.search(pat, "PR-менеджер", _re.I))
        say("stopwords   :", ("ok, " + pat) if good else "ЛОГИКА СЛОМАНА: " + pat)
        ok = ok and good

        cls_ok = classify("📨 Отправили отклик на вакансию https://hh.ru/vacancy/1") == "applied"
        say("run_monitor :", "ok" if cls_ok else "разбор событий сломан")
        ok = ok and cls_ok
    except Exception as exc:
        ok = False
        say("hh_patch    : FAIL", exc)

    try:
        from hh_applicant_tool.storage import utils as st

        schema = (Path(st.__file__).resolve().parent / "queries" / "schema.sql").exists()
        say("schema.sql  :", "ok" if schema else "MISSING")
        ok = ok and schema
    except Exception as exc:
        ok = False
        say("schema.sql  : FAIL", exc)

    try:
        from playwright._impl._driver import compute_driver_executable

        drv = Path(str(compute_driver_executable()[0])).exists()
        say("pw driver   :", "ok" if drv else "MISSING")
        ok = ok and drv
    except Exception as exc:
        ok = False
        say("pw driver   : FAIL", exc)

    try:
        import engine

        info = engine.check()
        say("engine      :", info["version"],
            "ok" if info["ok"] else "НЕСОВМЕСТИМ")
        for problem in info["problems"]:
            say("   !!", problem)
        ok = ok and info["ok"]
    except Exception as exc:
        ok = False
        say("engine      : FAIL", exc)

    try:
        import letters

        lv = letters.validate(letters.DEFAULT_LETTER, "letter")
        pv = letters.validate(letters.DEFAULT_PING, "ping")
        say(f"letters     : письмо {lv['variants']} вариантов,"
            f" пинг {pv['variants']}")
        good = lv["ok"] and pv["ok"] and lv["variants"] > 100 and pv["variants"] > 1000
        if not good:
            say("   !!", lv["errors"], pv["errors"])
        ok = ok and good
    except Exception as exc:
        ok = False
        say("letters     : FAIL", exc)

    try:
        import app_api as _api

        leftovers = [n for n in dir(_api.AppApi)
                     if "ai" == n.lower()[:2] or "_ai" in n.lower()]
        say("без ИИ      :", "ok" if not leftovers else "ОСТАЛОСЬ: " + ", ".join(leftovers))
        ok = ok and not leftovers
    except Exception as exc:
        ok = False
        say("без ИИ      : FAIL", exc)

    try:
        from app_api import chromium_executable

        exe = chromium_executable()
        if exe:
            say("chromium    :", ("ok " if Path(exe).exists() else "НЕТ ФАЙЛА ") + exe)
            ok = ok and Path(exe).exists()
        else:
            say("chromium    : путь не определён")
            ok = False
    except Exception as exc:
        ok = False
        say("app_api     : FAIL", exc)

    try:
        from snowballstemmer import stemmer

        say("stemmer     : ok, продажи ->", stemmer("russian").stemWord("продажи"))
    except Exception as exc:
        ok = False
        say("stemmer     : FAIL", exc)

    try:
        import webview  # noqa: F401

        if os.name == "nt":
            import webview.platforms.edgechromium  # noqa: F401

            say("webview     : ok (edgechromium)")
            say("webview2    :", webview2_version())
        else:
            say("webview     : ok")
    except Exception as exc:
        ok = False
        say("webview     : FAIL", exc)

    try:
        import telebot  # noqa: F401
        import telegram_bot  # noqa: F401

        assert callable(telegram_bot.start_bot_thread)
        say("telegram    : ok")
    except Exception as exc:
        ok = False
        say("telegram    : FAIL", exc)

    try:
        import core_manager

        say("core        :", core_manager.get_installed_version() or "не установлено",
            "|", core_manager.get_core_path())
    except Exception as exc:
        ok = False
        say("core        : FAIL", exc)

    say("RESULT      :", "OK" if ok else "FAILED")
    try:
        (home_dir() / "selftest.log").write_text("\n".join(lines), encoding="utf-8")
    except Exception:
        pass
    return 0 if ok else 1


# ---------------------------------------------------------------------- main


def webview2_version() -> str:
    """Версия WebView2 Runtime из реестра: на корпоративных ПК её замораживают."""
    if sys.platform != "win32":
        return "-"
    import winreg

    client = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    places = (
        (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\" + client),
        (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\" + client),
        (winreg.HKEY_CURRENT_USER, "SOFTWARE\\" + client),
    )
    for root, path in places:
        try:
            with winreg.OpenKey(root, path) as key:
                value, _ = winreg.QueryValueEx(key, "pv")
        except OSError:
            continue
        if value and value != "0.0.0.0":
            return str(value)
    return "не найден"


def _proxy_summary() -> str:
    """Прокси, которые увидит urllib, без логинов и паролей."""
    import urllib.parse
    import urllib.request

    try:
        found = urllib.request.getproxies()
    except Exception as exc:
        return f"не прочитать ({exc})"
    parts = []
    for scheme, url in sorted(found.items()):
        host = urllib.parse.urlsplit(url).hostname if "://" in url else url
        parts.append(f"{scheme}={host}")
    return ", ".join(parts) or "нет"


def loader_log():
    """Журнал первого запуска — loader.log рядом с data.

    Окно первого запуска ничего не показывает, если не поднялся WebView2,
    и раньше от зависания не оставалось следа. Пишем сюда шаги установки,
    ошибки и сообщения pywebview.
    """
    import logging

    log = logging.getLogger("hh_agent.loader")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    log.propagate = False
    try:
        path = home_dir() / "loader.log"
        mode = "w" if path.exists() and path.stat().st_size > 512 * 1024 else "a"
        handler: logging.Handler = logging.FileHandler(path, mode=mode, encoding="utf-8")
    except OSError:
        handler = logging.NullHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    log.addHandler(handler)
    logging.getLogger("pywebview").addHandler(handler)
    return log


class LoaderApi:
    """API экрана первого запуска: скачивание ядра.

    Установка не зависит от окна: её запускает Python, а экран только
    показывает ход. Если мост JS↔Python в WebView2 не поднялся (так висел
    корпоративный ноутбук на «Инициализации»), ядро всё равно встанет и
    приложение перезапустится.

    Публичны только методы для JS. pywebview перед подключением окна обходит
    все публичные поля объекта; раньше здесь было свойство window, и обход
    уходил в объекты WinForms/WebView2, которые можно трогать только из
    потока окна, — тысячи ошибок и задержка подключения окна.
    """

    def __init__(self):
        import queue
        import threading

        self._window = None
        self._lock = threading.Lock()
        self._running = False
        self._state = {"status": "idle", "message": ""}
        self._ui = queue.Queue()
        self._ui_thread = None
        self._log = loader_log()

    def set_window(self, window):
        self._window = window

    def _js(self, fn: str, *args) -> None:
        """Зовём JS с безопасно экранированными аргументами, не дожидаясь окна.

        evaluate_js в pywebview ждёт ответа страницы без таймаута; если мост
        WebView2 не поднялся, вместе с ним зависла бы установка ядра. Поэтому
        вызовы уходят в очередь отдельного потока.
        """
        if not self._window:
            return
        import json
        import threading

        payload = ", ".join(json.dumps(a, ensure_ascii=False) for a in args)
        self._ui.put(f"if(window.{fn})window.{fn}({payload})")
        if self._ui_thread is None:
            self._ui_thread = threading.Thread(target=self._ui_worker, daemon=True)
            self._ui_thread.start()

    def _ui_worker(self) -> None:
        while True:
            script = self._ui.get()
            try:
                self._window.evaluate_js(script)
            except Exception as exc:
                self._log.warning("окно не приняло обновление: %s", exc)

    def _set(self, status: str, message: str) -> None:
        self._state = {"status": status, "message": message}
        if status == "error":
            self._log.error("установка не удалась: %s", message)
            self._js("install_failed", message)
        else:
            self._log.info("%s", message)
            self._js("update_progress", message)

    def loader_state(self) -> dict:
        """Текущее состояние — окно спрашивает его, когда мост поднялся."""
        self._log.info("окно на связи (pywebviewready)")
        return dict(self._state)

    def autostart(self) -> None:
        """Запуск установки из Python, без сигнала от окна (webview.start(func))."""
        window = self._window
        if window is not None and not window.events.shown.wait(15):
            self._log.warning("окно не показалось за 15 с, установка идёт без него")
        self.install_core()

    def install_core(self):
        import threading

        with self._lock:
            if self._running:
                return {"status": "running"}
            if self._state["status"] == "done":
                return {"status": "done"}
            self._running = True

        def _task():
            import core_manager
            import time

            try:
                version = core_manager.install_core(lambda msg: self._set("progress", msg))
                self._log.info("ядро %s установлено, перезапуск", version)
                self._set("done", "Готово! Перезапуск...")
                time.sleep(1)
                restart_app()
            except Exception as exc:
                self._log.exception("ошибка установки ядра")
                self._set("error", f"{exc}")
            finally:
                self._running = False

        threading.Thread(target=_task, daemon=True).start()
        return {"status": "started"}


def main() -> int:
    argv = sys.argv[1:]

    import core_manager

    # Служебные режимы обязаны работать и без установленного ядра:
    # именно селф-тест объясняет, почему ядро не встало.
    if "--install-browser" in argv:
        return install_browser_mode()
    if "--selftest" in argv:
        core_manager.init_core()
        return selftest()

    if not core_manager.is_core_installed():
        import webview

        api = LoaderApi()
        log = loader_log()
        log.info("первый запуск: %s %s, %s, frozen=%s, WebView2 %s, прокси: %s",
                     APP_NAME, APP_VERSION, sys.platform,
                     getattr(sys, "frozen", False), webview2_version(), _proxy_summary())
        loader_url = (resource_dir() / "loader.html").as_uri()
        window = webview.create_window(
            title=f"{APP_NAME} Launcher",
            url=loader_url,
            js_api=api,
            width=520,
            height=480,
            resizable=False,
        )
        api.set_window(window)
        webview.start(api.autostart)
        log.info("окно первого запуска закрыто")
        return 0

    core_manager.init_core()

    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    data_dir = active_profile_dir()

    # Сначала импортируем локальные патчи и логику, так как ядро уже в sys.path
    import hh_applicant_tool.ui as tool_ui

    tool_ui.create_window = create_window

    from hh_applicant_tool.main import main as tool_main

    cmd = ["-c", str(data_dir), "ui"]
    if "--debug" in argv:
        cmd.append("--debug")
    return tool_main(cmd) or 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        import traceback

        with open(home_dir() / "crash.log", "a", encoding="utf-8") as fp:
            traceback.print_exc(file=fp)
        traceback.print_exc()
        raise SystemExit(1)
