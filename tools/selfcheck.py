# -*- coding: utf-8 -*-
"""Функциональный прогон модулей HH Agent.

Запуск:  python tools/selfcheck.py [путь-к-проекту]

Проверяет то, что ломалось раньше: спинтакс, стоп-слова, разбор событий,
профили, патчи движка, соответствие флагов engine.py реальному ядру,
кодировки интерфейса и наличие всех методов, которые дёргает JS.

Требует установленного ядра hh-applicant-tool (pip install).
В сеть ходит только проверка core_manager_install.
"""
from __future__ import annotations

import io
import os
import re
import sqlite3
import sys
import tempfile
import traceback
from pathlib import Path

BUILD = Path(sys.argv[1] if len(sys.argv) > 1 else
             Path(__file__).resolve().parent.parent).resolve()
sys.path.insert(0, str(BUILD))

RESULTS = []


def check(name, fn):
    try:
        fn()
        RESULTS.append(("PASS", name, ""))
    except AssertionError as exc:
        RESULTS.append(("FAIL", name, str(exc) or "assert"))
    except Exception as exc:
        RESULTS.append(("ERROR", name, f"{type(exc).__name__}: {exc}\n"
                        + traceback.format_exc(limit=3)))


# ------------------------------------------------------------------ stopwords

def t_stopwords_basic():
    import stopwords
    pat = stopwords.to_pattern("продажи, монтаж видео")
    assert pat, "пустой паттерн"
    rx = re.compile(pat, re.I)
    assert rx.search("Менеджер по продажам"), pat
    assert not rx.search("PR-менеджер"), pat
    assert rx.search("Монтаж видео и графика"), pat


def t_stopwords_regex_passthrough():
    import stopwords
    src = r"\bпродаж\w*"
    assert stopwords.to_pattern(src) == src


def t_stopwords_parse():
    import stopwords
    assert stopwords.parse_words("+ а, - б\nв; а") == ["а", "б", "в"]


def t_stopwords_bad_regex():
    """Ломаная регулярка не должна валить to_pattern."""
    import stopwords
    out = stopwords.to_pattern(r"(незакрытая|скобка")
    re.compile(out)  # должно компилироваться


def t_stopwords_latin():
    import stopwords
    pat = stopwords.to_pattern("sales")
    assert re.search(pat, "Sales manager", re.I), pat


# -------------------------------------------------------------------- letters

def t_letters_defaults():
    import letters
    lv = letters.validate(letters.DEFAULT_LETTER, "letter")
    pv = letters.validate(letters.DEFAULT_PING, "ping")
    assert lv["ok"], lv["errors"]
    assert pv["ok"], pv["errors"]
    assert lv["variants"] > 100, lv["variants"]
    assert pv["variants"] > 1000, pv["variants"]


def t_letters_errors():
    import letters
    assert not letters.validate("Привет {а|б", "letter")["ok"]
    assert not letters.validate("Скидка 50% сегодня", "letter")["ok"]
    assert not letters.validate("%(nope)s", "letter")["ok"]
    assert letters.validate("Скидка 50%% сегодня {а|б}", "letter")["ok"]


def t_letters_ping_placeholders():
    import letters
    # resume_url есть в письме, но НЕ в пинге
    assert letters.validate("%(resume_url)s {а|б}", "letter")["ok"]
    assert not letters.validate("%(resume_url)s {а|б}", "ping")["ok"]


def t_letters_samples():
    import letters
    out = letters.samples(letters.DEFAULT_LETTER, 3, "letter", seed=1)
    assert len(out) == 3, out
    assert all("%(" not in s for s in out), out
    assert len(set(out)) == 3


def t_letters_pick_unique():
    import letters
    with tempfile.TemporaryDirectory() as d:
        letters.set_memory_path(Path(d) / "mem.json")
        tpl = "{раз|два|три}"
        seen = set()
        for _ in range(3):
            seen.add(letters._norm(letters.pick_unique(tpl, [], key="chat1")))
        assert len(seen) == 3, seen
        assert (Path(d) / "mem.json").exists()
        letters.set_memory_path(None)


def t_letters_history():
    import letters
    letters.set_memory_path(None)
    hist = ["[12:00] Я: раз", "[12:01] Работодатель: ок"]
    out = letters.pick_unique("{раз|два}", hist)
    assert letters._norm(out) == "два", out


def t_letters_count_variants():
    import letters
    assert letters.count_variants("{а|б}{в|г}") == 4
    assert letters.count_variants("{а|{б|в}}") == 3
    assert letters.count_variants("просто текст") == 1


# --------------------------------------------------------------------- engine

def t_engine_check():
    import engine
    info = engine.check()
    assert info["ok"], info["problems"]


def t_engine_build_argv():
    import engine
    argv = engine.build_argv("apply", {
        "resume_id": "abc", "force_message": True, "skip_tests": False,
        "area": [1, 2], "max_responses": 200.0, "no_magic": True,
        "dry_run": False, "unknown_key": 1,
    })
    assert "--resume-id" in argv and "abc" in argv, argv
    assert "--force-message" in argv, argv
    assert "--no-skip-tests" in argv, argv
    assert "--no-dry-run" in argv, argv
    assert "--no-magic" in argv, argv
    assert argv[argv.index("--area") + 1:argv.index("--area") + 3] == ["1", "2"], argv
    assert "200" in argv and "200.0" not in argv, argv
    assert "unknown_key" not in " ".join(argv)


def t_engine_argv_accepted_by_core():
    """Собранный argv должен реально распарситься движком."""
    import argparse
    import engine
    from importlib import import_module
    for kind in ("apply", "ping", "clear", "bump"):
        mod = import_module(f"hh_applicant_tool.operations.{engine.MODULES[kind]}")
        parser = argparse.ArgumentParser(add_help=False)
        mod.Operation().setup_parser(parser)
        choices = {}
        for act in parser._actions:
            if act.choices:
                for opt in act.option_strings:
                    choices[opt] = sorted(act.choices)[0]
        opts = {}
        for k, (flag, m) in engine.OPTIONS[kind].items():
            if flag in choices:
                opts[k] = [choices[flag]] if m == "list" else choices[flag]
            elif m == "list":
                opts[k] = ["1"]
            elif m in ("bool", "switch"):
                opts[k] = True
            else:
                opts[k] = "1"
        argv = engine.build_argv(kind, opts)
        ns = getattr(mod, "Namespace", argparse.Namespace)
        parser.parse_args(argv, namespace=ns())


def t_engine_apply_params():
    import engine
    p = engine.apply_params({"salary": 100, "bogus": 1})
    assert p["max_responses"] == 200
    assert p["force_message"] is True
    assert "bogus" not in p


def t_engine_run_unknown():
    import engine
    out, code = engine.run(None, "нет-такого")
    assert code == 1, (out, code)


def t_engine_run_typeerror_not_swallowed():
    """TypeError внутри операции не должен приводить к повторному запуску."""
    import engine
    from importlib import import_module
    mod = import_module("hh_applicant_tool.operations.update_resumes")
    calls = []
    orig = mod.Operation.run

    def boom(self, tool, args=None):
        calls.append(1)
        raise TypeError("внутренняя ошибка операции")

    mod.Operation.run = boom
    try:
        out, code = engine.run(object(), "bump", {})
    finally:
        mod.Operation.run = orig
    assert len(calls) == 1, f"операция выполнилась {len(calls)} раз(а)"
    assert code == 1, (out, code)


def t_engine_cancel():
    import engine
    assert engine.cancel_run() is False


# ---------------------------------------------------------------- run_monitor

def t_monitor_classify():
    from run_monitor import classify
    cases = {
        "📨 Отправили отклик на вакансию https://hh.ru/vacancy/1": "applied",
        "📨 Отправили отклик на вакансию с тестом https://hh.ru/v/2": "applied_test",
        "Вакансия попала под фильтр: https://hh.ru/v/3": "filtered",
        "Вакансия добавлена в черный список: x": "blacklisted",
        "⏩ Вакансия уже отклонена ранее x": "seen_before",
        "⛔ Пришел отказ от x": "rejected",
        "🚀 Начинаю рассылку откликов для резюме: Тест": "resume_start",
        "✅️ Закончили рассылку для резюме: Тест. Отправлено: 5": "resume_done",
        "📝 Отклики на вакансии разосланы!": "all_done",
        "Требуется капча": "captcha",
        "Произошла непредвиденная ошибка": "error",
        "просто строка": "info",
    }
    for text, want in cases.items():
        got = classify(text)
        assert got == want, f"{text!r}: ждали {want}, получили {got}"


class _FakeConfig(dict):
    def save(self, **kw):
        self.update(kw)


class _FakeTool:
    def __init__(self, root: Path):
        self.config_path = root
        self.config = _FakeConfig()
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        # Схема повторяет storage/queries/schema.sql ядра.
        self.db.execute(
            "CREATE TABLE vacancies ("
            " id INTEGER PRIMARY KEY, name TEXT NOT NULL, area_id INTEGER,"
            " area_name TEXT, salary_from INTEGER, salary_to INTEGER,"
            " currency VARCHAR(3), gross BOOLEAN, published_at DATETIME,"
            " created_at DATETIME DEFAULT CURRENT_TIMESTAMP,"
            " updated_at DATETIME DEFAULT CURRENT_TIMESTAMP, remote BOOLEAN,"
            " experience TEXT, professional_roles TEXT, alternate_url TEXT)"
        )
        self.db.execute(
            "INSERT INTO vacancies (id, name, area_name, salary_from, salary_to,"
            " currency, published_at, remote, alternate_url)"
            " VALUES (1,'Тест','Москва',100000,200000,'RUR','2026-09-01',1,"
            "'https://hh.ru/vacancy/1')"
        )
        self.db.commit()


def t_monitor_feed_and_report():
    from run_monitor import RunMonitor
    with tempfile.TemporaryDirectory() as d:
        tool = _FakeTool(Path(d))
        m = RunMonitor(tool, Path(d))
        m.feed("🚀 Начинаю рассылку откликов для резюме: Тест")
        m.feed("📨 Отправили отклик на вакансию https://hh.ru/vacancy/1")
        m.feed("Вакансия попала под фильтр: https://hh.ru/vacancy/1")
        m.feed("✅️ Закончили рассылку для резюме: Тест. Отправлено: 1")
        snap = m.snapshot()
        assert snap["counts"].get("applied") == 1, snap["counts"]
        assert snap["counts"].get("filtered") == 1, snap["counts"]
        assert snap["resumes"][0]["applied"] == 1, snap["resumes"]
        ev = [e for e in snap["events"] if e["kind"] == "applied"][0]
        assert ev.get("title") == "Тест", ev
        assert "₽" in ev.get("salary", ""), ev
        m.finish("ok")
        reports = m.list_reports()
        assert reports, "отчёт не сохранился"
        assert m.load_report(reports[0]["file"])["status"] == "ok"


def t_monitor_load_report_traversal():
    from run_monitor import RunMonitor
    with tempfile.TemporaryDirectory() as d:
        tool = _FakeTool(Path(d))
        m = RunMonitor(tool, Path(d))
        assert m.load_report("../../etc/passwd") is None


def t_monitor_salary_fmt():
    from run_monitor import RunMonitor
    assert RunMonitor._salary(100000, 200000, "RUR") == "100 000–200 000 ₽"
    assert RunMonitor._salary(None, None, None) == ""
    assert RunMonitor._salary(50000, None, "USD").startswith("от 50 000")


# -------------------------------------------------------------------- profiles

def t_profiles_lifecycle():
    import profiles
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        st = profiles.ensure(root)
        assert st["active"] == "default"
        pid = profiles.add(root, "Анна Петрова")
        assert pid == "anna-petrova", pid
        assert profiles.set_active(root, pid)
        assert not profiles.set_active(root, "нет-такого")
        lst = profiles.listing(root)
        assert lst["active"] == pid
        assert len(lst["profiles"]) == 2
        assert profiles.rename(root, pid, "Аня")
        assert profiles.add(root, "Анна Петрова") == "anna-petrova-2"


def t_profiles_import():
    import profiles
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "data"
        src = Path(d) / "bot" / "hh_data"
        src.mkdir(parents=True)
        (src / "config.json").write_text('{"token":{}}', encoding="utf-8")
        (src.parent / "letter.txt").write_text("привет", encoding="utf-8")
        res = profiles.import_from(root, "Бот", src.parent)
        assert res["status"] == "ok", res
        assert "config.json" in res["copied"], res
        assert "letter.txt" in res["copied"], res
        bad = profiles.import_from(root, "X", Path(d) / "нет")
        assert bad["status"] == "error"


def t_profiles_slug_nonlatin():
    import profiles
    assert profiles.slugify("") == "profile"
    assert profiles.slugify("!!!") == "profile"
    assert profiles.slugify("Щука Ёж") == "schuka-ezh"


# ------------------------------------------------------------- core_manager

def t_core_manager_paths():
    import core_manager
    p = core_manager.get_core_path()
    assert p.name == "core" and p.parent.name == "HH_Agent", p


def t_core_manager_install(tmpenv=True):
    """Реальная установка ядра с PyPI во временную папку (нужна сеть)."""
    import importlib
    import core_manager
    with tempfile.TemporaryDirectory() as d:
        old = os.environ.get("APPDATA"), os.environ.get("XDG_CONFIG_HOME")
        os.environ["APPDATA"] = d
        try:
            core_manager.get_core_path = lambda: Path(d) / "HH_Agent" / "core"
            core_manager.install_core(target_version="1.8.28")
            core = Path(d) / "HH_Agent" / "core"
            assert (core / "hh_applicant_tool" / "__init__.py").exists(), \
                sorted(p.name for p in core.iterdir())
            assert (core / "version.txt").read_text().strip() == "1.8.28"
            assert core_manager.is_core_installed()
            assert core_manager.get_installed_version() == "1.8.28"
        finally:
            importlib.reload(core_manager)
            if old[0] is not None:
                os.environ["APPDATA"] = old[0]


def t_core_manager_sdist():
    """sdist распаковывается без первого каталога и ничего не теряет."""
    import tarfile
    import core_manager
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        pkg = d / "src" / "pkg-1.0" / "hh_applicant_tool"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("x")
        (d / "src" / "pkg-1.0" / "PKG-INFO").write_text("meta")
        tar = d / "pkg.tar.gz"
        with tarfile.open(tar, "w:gz") as t:
            t.add(d / "src" / "pkg-1.0", arcname="pkg-1.0")
        core = d / "core"
        core.mkdir()
        with tarfile.open(tar, "r:gz") as t:
            core_manager._extract_sdist(t, core)
        assert (core / "hh_applicant_tool" / "__init__.py").exists()
        assert (core / "PKG-INFO").exists(), "файл верхнего уровня потерян"


# -------------------------------------------------------------------- hh_patch

def t_hh_patch_applies():
    import hh_patch
    hh_patch._applied = False
    hh_patch.apply_patches()
    from hh_applicant_tool.operations import apply_vacancies as av
    from hh_applicant_tool.api.client import BaseClient
    from hh_applicant_tool.operations import reply_employers as rm
    from hh_applicant_tool.storage.models.base import BaseModel
    assert av.Operation._is_excluded is hh_patch._is_excluded_by_name
    assert getattr(BaseClient, "_hh_delay_patched", False), "пауза не пропатчена"
    assert getattr(rm, "_hh_ping_patched", False), "пинги не пропатчены"
    assert getattr(BaseModel, "_hh_types_patched", False), "sqlite-типы не пропатчены"


def t_hh_patch_excluded_by_name():
    import hh_patch

    class Op:
        excluded_filter = r"\bпродаж\w*"
    op = Op()
    assert hh_patch._is_excluded_by_name(op, {"name": "Менеджер по продажам"})
    assert not hh_patch._is_excluded_by_name(op, {"name": "PR-менеджер"})
    assert not hh_patch._is_excluded_by_name(op, None)
    op.excluded_filter = "(битая"
    assert hh_patch._is_excluded_by_name(op, {"name": "что угодно"}) is False


def t_hh_patch_sqlite_types():
    from hh_applicant_tool.storage.models.employer import EmployerModel
    m = EmployerModel.from_api({"id": 1, "name": "X", "type": {"id": "company"}})
    row = m.to_db()
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id INTEGER, name TEXT, type TEXT)")
    conn.execute("INSERT INTO t (id,name,type) VALUES (?,?,?)",
                 (row["id"], row["name"], row.get("type")))


def t_hh_patch_delay_signature():
    """Патч паузы вызывает оригинал позиционно — сигнатура должна совпадать."""
    import inspect
    from hh_applicant_tool.api.client import BaseClient
    sig = inspect.signature(BaseClient.request)
    names = list(sig.parameters)
    assert names[:6] == ["self", "method", "endpoint", "params", "delay", "as_json"], names


# ------------------------------------------------------------------- app_api

def t_app_api_import():
    import app_api
    assert hasattr(app_api.AppApi, "apply_vacancies")
    assert hasattr(app_api.AppApi, "start_apply")


def t_app_api_no_dead_code():
    """После return в trigger_core_update не должно быть кода."""
    import ast
    import inspect
    import app_api
    src = inspect.getsource(app_api.AppApi.trigger_core_update)
    tree = ast.parse(src.strip())
    body = tree.body[0].body
    idx = [i for i, n in enumerate(body) if isinstance(n, ast.Return)]
    assert idx, "нет return"
    assert idx[-1] == len(body) - 1, "после return остался мёртвый код"


def t_app_api_window_attr():
    """Класс не должен обращаться к несуществующему self.window."""
    import inspect
    import app_api
    src = inspect.getsource(app_api)
    bad = re.findall(r"self\.window\b", src)
    assert not bad, f"обращений к self.window: {len(bad)} (в базовом Api он _window)"


def t_app_api_single_open_reports():
    import inspect
    import app_api
    src = inspect.getsource(app_api.AppApi)
    n = len(re.findall(r"def open_reports_folder", src))
    assert n == 1, f"open_reports_folder определён {n} раз(а)"


def t_app_api_stopwords_preview():
    import app_api
    fn = app_api.AppApi.stopwords_preview
    out = fn(object(), "продажи")
    assert out["words"] == ["продажи"], out
    assert "Менеджер по продажам" in out["examples"], out


def t_app_api_letter_flow():
    import app_api
    import letters
    with tempfile.TemporaryDirectory() as d:
        api = app_api.AppApi.__new__(app_api.AppApi)
        api._tool = _FakeTool(Path(d))
        st = app_api.AppApi.letter_state(api, "letter")
        assert st["empty"] and not st["ok"], st
        r = app_api.AppApi.letter_save(api, letters.DEFAULT_LETTER, "letter")
        assert r["status"] == "ok", r
        ready = app_api.AppApi.letter_ready(api)
        assert ready["ok"], ready
        bad = app_api.AppApi.letter_save(api, "50% скидка", "letter")
        assert bad["status"] == "error", bad
        app_api.AppApi.ensure_templates(api)
        assert (Path(d) / "ping.txt").exists()



def t_profiles_remove():
    """Удаление аккаунта: из списка, вместе с папкой, и защита последнего."""
    import profiles
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        profiles.ensure(root)
        a = profiles.add(root, "Аня")
        b = profiles.add(root, "Наташа")
        (profiles.profile_dir(root, a) / "config.json").write_text("{}", encoding="utf-8")
        (profiles.profile_dir(root, b) / "config.json").write_text("{}", encoding="utf-8")

        # убрать из списка — файлы на месте
        res = profiles.remove(root, a, purge=False)
        assert res["status"] == "ok" and res["purged"] is False, res
        assert profiles.profile_dir(root, a).exists(), "папку стёрли, хотя не просили"
        assert a not in {p["id"] for p in profiles.listing(root)["profiles"]}

        # удалить вместе с папкой
        res = profiles.remove(root, b, purge=True)
        assert res["status"] == "ok" and res["purged"] is True, res
        assert not profiles.profile_dir(root, b).exists(), "папка осталась"

        # последний профиль не удаляется
        last = profiles.listing(root)["active"]
        res = profiles.remove(root, last)
        assert res["status"] == "error", res
        assert profiles.listing(root)["profiles"], "список профилей опустел"

        # несуществующий
        assert profiles.remove(root, "нет-такого")["status"] == "error"


def t_profiles_remove_active():
    """Удаление активного аккаунта переводит активность на оставшийся."""
    import profiles
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        profiles.ensure(root)
        other = profiles.add(root, "Запасной")
        active = profiles.listing(root)["active"]
        res = profiles.remove(root, active)
        assert res["status"] == "ok" and res["switched"] is True, res
        assert res["active"] == other, res
        assert profiles.listing(root)["active"] == other


RESUMES_RAW = [
    {"id": "aaa", "title": "Редактор / шеф-редактор",
     "alternate_url": "https://hh.ru/resume/aaa",
     "status": {"id": "published", "name": "опубликовано"},
     "updated_at": "2026-09-14 14:31:45", "created_at": "2026-03-22T19:40:48+0300",
     "can_publish_or_update": False,
     "counters": {"total_views": 12, "new_views": 3, "invitations": 1}},
    # у второго нет counters и пустое название — так бывает у черновиков
    {"id": "bbb", "title": "  ", "alternate_url": "", "status": {}},
]


def _api_with_resumes(root, raw=None):
    import app_api
    api = app_api.AppApi.__new__(app_api.AppApi)
    api._tool = _FakeTool(root)
    api.get_resumes = lambda: list(RESUMES_RAW if raw is None else raw)
    return api


def t_app_api_resumes_list():
    import app_api
    with tempfile.TemporaryDirectory() as d:
        api = _api_with_resumes(Path(d))
        api.get_status = lambda: {"authorized": True}
        r = app_api.AppApi.resumes_list(api)
        assert r["authorized"] and not r["stale"], r
        assert [i["id"] for i in r["items"]] == ["aaa", "bbb"], r["items"]
        first = r["items"][0]
        assert first["title"] == "Редактор / шеф-редактор"
        assert first["status"] == "опубликовано" and first["views"] == 12
        second = r["items"][1]
        assert second["title"] == "Без названия", second
        assert second["views"] == 0 and second["status"] == "", second


def t_app_api_select_resume():
    import app_api
    with tempfile.TemporaryDirectory() as d:
        api = _api_with_resumes(Path(d))
        api.get_status = lambda: {"authorized": True}

        res = app_api.AppApi.select_resume(api, "aaa")
        assert res["status"] == "ok", res
        assert api.search_settings()["resume_id"] == "aaa"
        assert app_api.AppApi.resume_choice(api)["title"] == "Редактор / шеф-редактор"

        # выбор несуществующего резюме отклоняем
        bad = app_api.AppApi.select_resume(api, "нет-такого")
        assert bad["status"] == "error", bad
        assert api.search_settings()["resume_id"] == "aaa", "настройку всё-таки затёрли"

        # пусто = все резюме
        res = app_api.AppApi.select_resume(api, "")
        assert res["status"] == "ok", res
        assert api.search_settings()["resume_id"] is None
        assert app_api.AppApi.resume_choice(api)["title"] == "все резюме"


def t_app_api_resume_stale():
    """Резюме удалили на hh.ru — интерфейс должен об этом узнать."""
    import app_api
    with tempfile.TemporaryDirectory() as d:
        api = _api_with_resumes(Path(d))
        api.get_status = lambda: {"authorized": True}
        app_api.AppApi.select_resume(api, "aaa")
        api.get_resumes = lambda: [RESUMES_RAW[1]]
        api._resumes_cache = None
        r = app_api.AppApi.resumes_list(api, force=True)
        assert r["stale"] is True and r["selected"] == "", r
        assert app_api.AppApi.resume_choice(api).get("missing") is True


def t_app_api_start_apply_passes_resume():
    """Выбранное резюме должно доехать до ядра как --resume-id."""
    import app_api
    import engine
    with tempfile.TemporaryDirectory() as d:
        api = _api_with_resumes(Path(d))
        api.get_status = lambda: {"authorized": True}
        app_api.AppApi.select_resume(api, "aaa")
        seen = {}
        api.apply_vacancies = lambda params: seen.update(params) or {"status": "ok"}
        app_api.AppApi.start_apply(api)
        assert seen.get("resume_id") == "aaa", seen
        argv = engine.build_argv("apply", {"resume_id": seen["resume_id"]})
        assert argv == ["--resume-id", "aaa"], argv

        # «все резюме» — флага быть не должно
        app_api.AppApi.select_resume(api, "")
        seen.clear()
        app_api.AppApi.start_apply(api)
        assert "resume_id" not in seen, seen


def t_app_api_resumes_cache():
    """Пустой ответ при сетевом сбое не должен затирать список."""
    import app_api
    with tempfile.TemporaryDirectory() as d:
        api = _api_with_resumes(Path(d))
        first = app_api.AppApi._resumes_raw(api)
        assert len(first) == 2
        api.get_resumes = lambda: []
        api._resumes_cache = (0.0, first)      # делаем кэш «просроченным»
        again = app_api.AppApi._resumes_raw(api)
        assert len(again) == 2, "сетевой сбой обнулил список резюме"
        forced = app_api.AppApi._resumes_raw(api, force=True)
        assert forced == [], "принудительное обновление должно отдавать факт"


def t_ui_theme_button_has_no_label():
    """Кнопка темы — только иконка; подпись живёт в title, а не на кнопке."""
    src = (BUILD / "ui" / "theme.js").read_text(encoding="utf-8")
    body = src[src.find("function updateBtn"):]
    body = body[:body.find("\n    }") + 1]
    assert "innerHTML" not in body, "в кнопку всё ещё пишется разметка с подписью"
    m = re.search(r"btn\.textContent\s*=\s*isDark\s*\?\s*'([^']*)'\s*:\s*'([^']*)'", body)
    assert m, "не нашёл присваивания textContent кнопке темы"
    for label in m.groups():
        assert not re.search(r"[A-Za-zА-Яа-яЁё]", label), f"в кнопке осталась подпись: {label!r}"
    assert "btn.title" in body, "нет подсказки title у кнопки темы"


def t_ui_resumes_tab_present():
    html = (BUILD / "ui" / "app.html").read_text(encoding="utf-8")
    for needle in ('id="s-resumes"', 'data-s="resumes"', 'id="resList"',
                   'id="profList"', 'id="modal"'):
        assert needle in html, f"нет {needle}"
    assert 'id="setProf"' not in html, "остался старый select профилей"


# --------------------------------------------------------------- telegram_bot

def t_telegram_import():
    import telegram_bot
    assert callable(telegram_bot.start_bot_thread)


def t_telegram_escape():
    import telegram_bot
    assert telegram_bot.html_escape("<b>&</b>") == "&lt;b&gt;&amp;&lt;/b&gt;"


def t_telegram_stats_null_area():
    """area_name бывает NULL — форматирование не должно падать."""
    import telegram_bot
    with tempfile.TemporaryDirectory() as d:
        tool = _FakeTool(Path(d))
        tool.db.execute(
            "INSERT INTO vacancies (id, name, area_name, salary_from, currency,"
            " published_at, remote, alternate_url)"
            " VALUES (2,'Вакансия без региона и без ссылки',NULL,50000,'RUR',"
            "NULL,0,NULL)")
        tool.db.commit()

        class S:
            pass
        s = S()
        s.vacancies = type("V", (), {"conn": tool.db})()
        tool.storage = s
        out = telegram_bot.generate_salary_stats(tool)
        assert "Ошибка" not in out, out
        out2 = telegram_bot.generate_fresh_vacs(tool)
        assert "Ошибка" not in out2, out2


def t_telegram_command_names():
    """Имена команд пишем как в Bot API — без слэша (гигиена, не баг:
    сервер ведущий слэш срезает сам)."""
    import inspect
    import telegram_bot
    src = inspect.getsource(telegram_bot)
    bad = re.findall(r"BotCommand\(\s*[\"']/", src)
    assert not bad, f"команды со слэшем: {bad}"


def t_core_manager_no_traversal():
    """Распаковка не должна писать за пределы целевой папки."""
    import tarfile
    import io as _io
    import core_manager
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        tar = d / "evil.tar.gz"
        with tarfile.open(tar, "w:gz") as t_:
            for name in ("pkg-1.0/../../evil.txt", "pkg-1.0/ok.txt"):
                data = b"x"
                info = tarfile.TarInfo(name)
                info.size = len(data)
                t_.addfile(info, _io.BytesIO(data))
        core = d / "core"
        core.mkdir()
        with tarfile.open(tar, "r:gz") as t_:
            core_manager._extract_sdist(t_, core)
        assert not (d.parent / "evil.txt").exists()
        assert not (d / "evil.txt").exists(), "файл записан за пределы папки ядра"


def t_telegram_nontext_message():
    """Фильтр стоп-слов читает message.text безопасно.

    telebot и так пускает в func-фильтр только content_type == "text",
    так что это защита на будущее, а не исправление наблюдаемого бага.
    """
    import inspect
    import telegram_bot
    src = inspect.getsource(telegram_bot)
    assert ".text.startswith" not in src, \
        "message.text используется без проверки на None"
    assert "getattr(m, \"text\", None)" in src or "getattr(m, 'text', None)" in src, \
        "нет безопасного чтения message.text"


def t_telegram_polling_retries():
    import inspect
    import telegram_bot
    src = inspect.getsource(telegram_bot)
    assert "while True" in src, "поллинг не перезапускается после сетевой ошибки"


# ------------------------------------------------------------------ app_main

def t_app_main_import():
    import app_main
    assert app_main.APP_VERSION


def t_app_main_selftest_without_core():
    """--selftest и --install-browser должны работать до установки ядра."""
    import inspect
    import app_main
    src = inspect.getsource(app_main.main)
    i_core = src.find("is_core_installed")
    i_self = src.find("--selftest")
    assert i_self < i_core, "--selftest обрабатывается только после проверки ядра"


def t_no_pyflakes_problems():
    try:
        import pyflakes.api
        import pyflakes.reporter
    except ImportError:
        return  # pyflakes необязателен
    buf = io.StringIO()
    rep = pyflakes.reporter.Reporter(buf, buf)
    for p in sorted(BUILD.glob("*.py")) + sorted(BUILD.glob("tools/*.py")):
        pyflakes.api.checkPath(str(p), rep)
    out = [l for l in buf.getvalue().splitlines()
           if "undefined name" in l or "redefinition" in l]
    assert not out, "\n".join(out)


# ----------------------------------------------------------------------- ui

def t_ui_encoding():
    bad = []
    for p in sorted((BUILD / "ui").glob("*")):
        t = p.read_bytes()
        if t.startswith(b"\xef\xbb\xbf"):
            bad.append(f"{p.name}: BOM")
        text = t.decode("utf-8")
        words = re.findall(r"[Ѐ-ӿ]{3,}", text)
        moji = 0
        for w in words:
            try:
                fixed = w.encode("cp1251").decode("utf-8")
            except Exception:
                continue
            if re.fullmatch(r"[Ѐ-ӿ ]+", fixed):
                moji += 1
        if moji:
            bad.append(f"{p.name}: битая кодировка ({moji} слов)")
    assert not bad, "; ".join(bad)


def t_ui_theme_js_placement():
    """theme.js обращается к document.body — он обязан подключаться в <body>."""
    for p in sorted((BUILD / "ui").glob("*.html")):
        text = p.read_text(encoding="utf-8")
        i_script = text.find("theme.js")
        i_head = text.find("</head>")
        if i_script < 0:
            continue
        assert i_script > i_head, f"{p.name}: theme.js подключён в <head>"


def t_ui_api_methods_exist():
    """Каждый pywebview.api.X из интерфейса должен существовать в AppApi."""
    import app_api
    import app_main
    names = set()
    for p in sorted((BUILD / "ui").glob("*.html")):
        text = p.read_text(encoding="utf-8")
        names |= set(re.findall(r"pywebview\s*\.\s*api\s*\.\s*(\w+)", text))
        names |= set(re.findall(r"\bapi\(\)\s*\.\s*(\w+)", text))
    missing = [n for n in sorted(names)
               if not hasattr(app_api.AppApi, n)
               and not hasattr(app_main.LoaderApi, n)]
    assert not missing, f"нет методов: {missing}"


def t_telegram_message_none_safe():
    """html_escape и форматирование денег переживают None."""
    import telegram_bot
    assert telegram_bot.html_escape(None) == ""
    assert telegram_bot.html_escape(123) == "123"
    assert "не указана" in telegram_bot._money(None)
    assert telegram_bot._money(150000) == "150 000 \u20bd"


def t_core_manager_reinstall_marker():
    """Метка переустановки должна отменять is_core_installed()."""
    import importlib
    import core_manager
    with tempfile.TemporaryDirectory() as d:
        core = Path(d) / "HH_Agent" / "core"
        (core / "hh_applicant_tool").mkdir(parents=True)
        (core / "hh_applicant_tool" / "__init__.py").write_text("")
        core_manager.app_data_path = lambda: Path(d) / "HH_Agent"
        try:
            assert core_manager.is_core_installed()
            core_manager.request_reinstall()
            assert not core_manager.is_core_installed()
            core_manager.clear_reinstall()
            assert core_manager.is_core_installed()
        finally:
            importlib.reload(core_manager)


def t_engine_serialises_runs():
    """Две операции движка одновременно не выполняются."""
    import threading
    import time
    import engine
    from importlib import import_module
    mod = import_module("hh_applicant_tool.operations.update_resumes")
    orig = mod.Operation.run
    overlap = []
    active = []

    def slow(self, tool, args=None):
        active.append(1)
        if len(active) > 1:
            overlap.append(1)
        time.sleep(0.25)
        active.pop()

    mod.Operation.run = slow
    try:
        threads = [threading.Thread(target=engine.run, args=(object(), "bump", {}))
                   for _ in range(3)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
    finally:
        mod.Operation.run = orig
    assert not overlap, "операции движка выполнялись параллельно"


def t_app_api_set_window_prepares_templates():
    """set_window обязан создавать ping.txt и включать память пингов."""
    import inspect
    import app_api
    src = inspect.getsource(app_api.AppApi.set_window)
    assert "ensure_templates" in src, "set_window не готовит шаблоны"
    assert "set_memory_path" in src, "set_window не задаёт память пингов"


def main():
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith("t_") and callable(v)]
    for name, fn in tests:
        check(name[2:], fn)

    width = max(len(n) for _, n, _ in RESULTS) + 2
    bad = 0
    for status, name, detail in RESULTS:
        print(f"{status:5} {name:<{width}}", end="")
        if status != "PASS":
            bad += 1
            print(detail.splitlines()[0] if detail else "")
            for line in detail.splitlines()[1:6]:
                print("        " + line)
        else:
            print()
    print(f"\n{len(RESULTS) - bad}/{len(RESULTS)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
