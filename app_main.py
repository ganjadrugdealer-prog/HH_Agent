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
APP_VERSION = "0.3.0"


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


def resource_dir() -> Path:
    """Наши ресурсы: в сборке это распакованный _MEIPASS."""
    base = getattr(sys, "_MEIPASS", None)
    return (Path(base) if base else Path(__file__).resolve().parent) / "ui"


def data_root() -> Path:
    """Корень данных приложения (общий для всех профилей)."""
    d = app_dir() / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def active_profile_dir() -> Path:
    """Папка активного профиля. Создаёт профиль по умолчанию при первом запуске."""
    import profiles

    root = data_root()
    state = profiles.ensure(root)
    return profiles.profile_dir(root, state["active"])


def restart_app() -> None:
    """Перезапускает приложение (после смены профиля)."""
    try:
        if getattr(sys, "frozen", False):
            cmd = [sys.executable]
        else:
            cmd = [sys.executable, str(Path(__file__).resolve())]
        subprocess.Popen(cmd, cwd=str(app_dir()), close_fds=True)
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
    for name in ("app.html", "wizard.html", "run.html"):
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
        import webview.platforms.edgechromium  # noqa: F401

        say("webview     : ok (edgechromium)")
    except Exception as exc:
        ok = False
        say("webview     : FAIL", exc)

    say("RESULT      :", "OK" if ok else "FAILED")
    try:
        (app_dir() / "selftest.log").write_text("\n".join(lines), encoding="utf-8")
    except Exception:
        pass
    return 0 if ok else 1


# ---------------------------------------------------------------------- main


class LoaderApi:
    def __init__(self):
        self.window = None

    def set_window(self, window):
        self.window = window

    def install_core(self):
        import core_manager
        import threading
        
        def _task():
            try:
                def progress_cb(msg):
                    if self.window:
                        self.window.evaluate_js(f'update_progress("{msg}")')
                
                core_manager.install_core(progress_cb)
                
                if self.window:
                    self.window.evaluate_js('update_progress("Готово! Перезапуск...")')
                import time
                time.sleep(1)
                
                # Restart the app
                restart_app()
            except Exception as e:
                import traceback
                traceback.print_exc()
                if self.window:
                    err_safe = str(e).replace('"', '\\"')
                    self.window.evaluate_js(f'document.getElementById("status-text").innerText = "Ошибка: {err_safe}";')
                    self.window.evaluate_js("document.getElementById('status-text').style.color = 'red';")
                    self.window.evaluate_js("document.querySelector('.spinner').style.display = 'none';")
        
        threading.Thread(target=_task, daemon=True).start()
        return {'status': 'started'}

def main() -> int:
    argv = sys.argv[1:]
    
    import core_manager
    if not core_manager.is_core_installed():
        import webview
        api = LoaderApi()
        loader_url = (resource_dir() / "loader.html").as_uri()
        window = webview.create_window(
            title=f"{APP_NAME} Launcher",
            url=loader_url,
            js_api=api,
            width=500,
            height=400,
            resizable=False
        )
        api.set_window(window)
        webview.start()
        return 0
        
    core_manager.init_core()
    
    if "--selftest" in argv:
        return selftest()
    if "--install-browser" in argv:
        return install_browser_mode()

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

        with open(app_dir() / "crash.log", "a", encoding="utf-8") as fp:
            traceback.print_exc(file=fp)
        traceback.print_exc()
        raise SystemExit(1)
