# -*- coding: utf-8 -*-
"""API мастера первого запуска.

Наследуемся от штатного Api утилиты, поэтому одна и та же связка обслуживает
и мастер, и основной интерфейс: get_status / start_login / save_config уже
реализованы автором, дублировать их незачем.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import requests

from hh_applicant_tool.ui.api import Api

import engine
import letters
import stopwords
from run_monitor import RunMonitor

logger = logging.getLogger("hh_agent")

SETUP_KEY = "app_setup"
SETUP_VERSION = 1

# Шаблоны живут в папке профиля: у каждого аккаунта своё письмо.
LETTER_FILE = "letter.txt"   # имя историческое, его же ждёт --letter-file
PING_FILE = "ping.txt"

def browsers_dir() -> Path:
    custom = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if custom:
        return Path(custom)
    local = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(local) / "ms-playwright"


def chromium_executable() -> str | None:
    """Путь к chromium ИМЕННО ТОЙ версии, которую ждёт установленный playwright."""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            return pw.chromium.executable_path
    except Exception:
        return None


def chromium_installed() -> bool:
    """Быстрая проверка без запуска драйвера playwright.

    Драйвер поднимать здесь нельзя: функция вызывается до создания окна,
    и sync_playwright() в этот момент роняет приложение.
    """
    d = browsers_dir()
    if not d.exists():
        return False
    for item in d.glob("chromium-*"):
        if (item / "chrome-win64" / "chrome.exe").exists():
            return True
        if any(item.rglob("chrome.exe")):
            return True
    return False


class AppApi(Api):
    """Штатный Api + всё, что нужно мастеру первого запуска."""

    def __init__(self, tool, main_url: str, resource_dir=None,
                 data_root=None, restart=None):
        super().__init__(tool)
        self._main_url = main_url
        self._install_running = False
        self._res_dir = Path(resource_dir) if resource_dir else Path(__file__).parent / "ui"
        self._data_root = Path(data_root) if data_root else None
        self._restart = restart
        self.monitor = RunMonitor(tool, self._res_dir)
        
        threading.Thread(target=self._check_core_update, daemon=True).start()

    def _check_core_update(self):
        import core_manager
        approved = core_manager.get_approved_version()
        installed = core_manager.get_installed_version()
        if approved and installed and approved != installed:
            import time
            time.sleep(2)  # Даем UI загрузиться
            if self.window:
                # Показываем системный JS confirm. Если да - запускаем апдейт.
                code = f"""
                if (confirm('Доступна новая версия ядра ({approved}). Установлена ({installed}). Обновить сейчас?')) {{
                    pywebview.api.trigger_core_update();
                }}
                """
                self.window.evaluate_js(code)

    def trigger_core_update(self):
        import core_manager
        import shutil
        shutil.rmtree(core_manager.get_core_path(), ignore_errors=True)
        if self._restart:
            self._restart()
        return True
        try:
            letters.set_memory_path(self.profile_dir() / "ping_memory.json")
            self.ensure_templates()
        except Exception:
            logger.exception("подготовка шаблонов")

    # ------------------------------------------------------- лента и отчёт

    def _send_progress(self, current: int, total: int, message: str = "") -> None:
        """Единая точка, куда утилита сливает весь свой вывод.

        Отдаём как было в основное окно (там прогресс-бар автора) и
        параллельно разбираем в структурные события для окна прогона.
        """
        super()._send_progress(current, total, message)
        try:
            self.monitor.feed(message)
        except Exception:
            logger.exception("monitor.feed")

    def apply_vacancies(self, params: dict[str, Any]) -> dict[str, Any]:
        params = dict(params or {})

        # Без сопроводительного письма рассылку не запускаем: иначе движок
        # подставит свою заглушку из apply_vacancies.py, и работодатели
        # получат безликий текст «Прошу рассмотреть мою кандидатуру».
        ready = self.letter_ready()
        if not ready.get("ok"):
            return {"status": "error", "message": ready.get("message")}
        params["letter_file"] = ready["path"]
        params["force_message"] = True

        # Поле стоп-слов в интерфейсе человеческое: список через запятую.
        # Утилита ждёт регулярку — переводим.
        raw = params.get("excluded_filter")
        if raw:
            pattern = stopwords.to_pattern(str(raw))
            if pattern:
                params["excluded_filter"] = pattern
                logger.info("стоп-фильтр: %s", pattern)

        # Текстовый поиск отключён намеренно: с ним движок уходит с
        # /similar_vacancies на обычный поиск и рекомендации HH перестают
        # работать. Все фильтры (зарплата, регион, формат) работают и без него.
        if params.pop("search", None):
            logger.info("поисковая строка отброшена: работаем по рекомендациям")

        # Пауза между запросами теперь случайная (hh_patch), фиксированную
        # из пресетов не берём.
        params.pop("api_delay", None)

        params = engine.apply_params(params)

        try:
            self.monitor.open_window(self, params)
        except Exception:
            logger.exception("не смог открыть окно прогона")

        result = super().apply_vacancies(params)

        try:
            self.monitor.finish(result.get("status", "ok"))
        except Exception:
            logger.exception("monitor.finish")
        return result

    # ============================================ сопроводительное и пинги

    def profile_dir(self) -> Path:
        return Path(self._tool.config_path)

    def template_path(self, kind: str = "letter") -> Path:
        return self.profile_dir() / (PING_FILE if kind == "ping" else LETTER_FILE)

    def ensure_templates(self) -> None:
        """Пинг даём готовым, письмо человек обязан написать сам."""
        p = self.template_path("ping")
        if not p.exists() or not p.read_text(encoding="utf-8").strip():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(letters.DEFAULT_PING, encoding="utf-8")

    def _read_template(self, kind: str) -> str:
        p = self.template_path(kind)
        try:
            return p.read_text(encoding="utf-8") if p.exists() else ""
        except Exception:
            logger.exception("чтение шаблона %s", kind)
            return ""

    def letter_state(self, kind: str = "letter") -> dict[str, Any]:
        kind = "ping" if kind == "ping" else "letter"
        text = self._read_template(kind)
        info = letters.validate(text, kind) if text.strip() else {
            "ok": False, "errors": [], "warnings": [], "variants": 0,
            "groups": 0, "length": 0,
        }
        return {
            "kind": kind,
            "text": text,
            "path": str(self.template_path(kind)),
            "empty": not text.strip(),
            "placeholders": list(
                letters.PING_PLACEHOLDERS if kind == "ping"
                else letters.LETTER_PLACEHOLDERS
            ),
            "demo": dict(letters.DEMO_VALUES),
            **info,
        }

    def letter_check(self, text: str, kind: str = "letter") -> dict[str, Any]:
        kind = "ping" if kind == "ping" else "letter"
        info = letters.validate(text or "", kind)
        info["samples"] = (
            letters.samples(text or "", 3, kind) if info.get("ok") else []
        )
        return info

    def letter_samples(self, text: str, kind: str = "letter",
                       count: int = 3) -> list[str]:
        kind = "ping" if kind == "ping" else "letter"
        try:
            return letters.samples(text or "", int(count or 3), kind)
        except Exception as exc:
            return [f"Не получилось развернуть шаблон: {exc}"]

    def letter_save(self, text: str, kind: str = "letter") -> dict[str, Any]:
        kind = "ping" if kind == "ping" else "letter"
        info = letters.validate(text or "", kind)
        if not info["ok"]:
            return {"status": "error", "message": "; ".join(info["errors"]),
                    **info}
        try:
            p = self.template_path(kind)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        except Exception as exc:
            logger.exception("сохранение шаблона")
            return {"status": "error", "message": str(exc)}
        return {"status": "ok", **info}

    def letter_default(self, kind: str = "letter") -> str:
        return letters.DEFAULT_PING if kind == "ping" else letters.DEFAULT_LETTER

    def letter_ready(self) -> dict[str, Any]:
        """Можно ли запускать рассылку. Ответ идёт на кнопку в интерфейсе."""
        path = self.template_path("letter")
        text = self._read_template("letter")
        if not text.strip():
            return {"ok": False, "path": str(path),
                    "message": "Добавьте сопроводительное письмо"}
        info = letters.validate(text, "letter")
        if not info["ok"]:
            return {"ok": False, "path": str(path),
                    "message": "Письмо с ошибкой: " + "; ".join(info["errors"])}
        return {"ok": True, "path": str(path), "message": "",
                "variants": info["variants"]}

    def run_state(self) -> dict[str, Any]:
        return self.monitor.snapshot()

    def list_reports(self) -> list[dict[str, Any]]:
        return self.monitor.list_reports()

    def load_report(self, file: str) -> dict[str, Any] | None:
        return self.monitor.load_report(file)

    def open_reports_folder(self) -> dict[str, str]:
        try:
            import subprocess

            path = self.monitor.reports_dir()
            subprocess.Popen(["explorer", str(path)])
            return {"status": "ok"}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    # ------------------------------------------------------------------ utils

    def _emit(self, fn: str, *args: Any) -> None:
        if not self._window:
            return
        try:
            payload = ", ".join(json.dumps(a, ensure_ascii=False) for a in args)
            self._window.evaluate_js(f"{fn}({payload})")
        except Exception:
            pass

    def ui_ready(self, page: str, info: str = "") -> dict[str, str]:
        """Страница сообщает, что отрисовалась. Нужно для диагностики сборки."""
        try:
            log = Path(self._tool.config_path) / "ui.log"
            with open(log, "a", encoding="utf-8") as fp:
                fp.write(f"{page}\t{info}\n")
        except Exception:
            pass
        logger.info("ui_ready: %s %s", page, info)
        return {"status": "ok"}

    # ------------------------------------------------------------------ state

    def wizard_state(self) -> dict[str, Any]:
        cfg = self._tool.config
        tg = cfg.get("telegram") or {}
        status = self.get_status()
        letter = self.letter_ready()
        return {
            "browser": chromium_installed(),
            "authorized": bool(status.get("authorized")),
            "user": status.get("user"),
            "letter": bool(letter.get("ok")),
            "letter_message": letter.get("message") or "",
            "telegram": bool(tg.get("bot_token") and tg.get("chat_id")),
            "completed": bool((cfg.get(SETUP_KEY) or {}).get("completed")),
        }

    # ---------------------------------------------------------------- browser

    def install_browser(self) -> dict[str, str]:
        if self._install_running:
            return {"status": "error", "message": "Установка уже идёт"}
        if chromium_installed():
            self._emit("onInstall", "done", "Браузер уже установлен")
            return {"status": "ok"}

        self._install_running = True

        def worker() -> None:
            event, message = "error", "Не удалось установить браузер"
            try:
                if getattr(sys, "frozen", False):
                    cmd = [sys.executable, "--install-browser"]
                else:
                    cmd = [sys.executable, str(Path(__file__).with_name("app_main.py")),
                           "--install-browser"]
                env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
                flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    env=env, creationflags=flags,
                )
                for line in proc.stdout:
                    line = line.strip()
                    if line:
                        self._emit("onInstall", "progress", line[:200])
                proc.wait()
                if chromium_installed():
                    event, message = "done", "Браузер установлен"
                else:
                    message = f"Установка завершилась с кодом {proc.returncode}"
            except Exception as exc:
                logger.exception("install_browser")
                message = f"Ошибка установки: {exc}"
            finally:
                self._install_running = False
                self._emit("onInstall", event, message)

        threading.Thread(target=worker, daemon=True).start()
        self._emit("onInstall", "started", "Скачиваю браузер, это разовая операция...")
        return {"status": "started"}

    # --------------------------------------------------------------- Telegram

    def check_telegram(self, bot_token: str, chat_id: str) -> dict[str, Any]:
        bot_token = (bot_token or "").strip()
        chat_id = (chat_id or "").strip()
        if not bot_token:
            return {"status": "error", "message": "Нужен токен бота"}
        try:
            me = requests.get(
                f"https://api.telegram.org/bot{bot_token}/getMe", timeout=20
            ).json()
            if not me.get("ok"):
                return {"status": "error",
                        "message": me.get("description") or "Токен не принят"}
            username = me["result"].get("username", "?")
            if not chat_id:
                return {"status": "ok",
                        "message": f"Бот @{username} найден. Теперь напиши ему в Telegram "
                                   f"любое сообщение и нажми «Определить мой ID»."}
            sent = requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": "HH Agent подключён. Проверка связи."},
                timeout=20,
            ).json()
            if sent.get("ok"):
                return {"status": "ok",
                        "message": f"Бот @{username}: тестовое сообщение доставлено"}
            return {"status": "error",
                    "message": sent.get("description") or "Сообщение не доставлено"}
        except Exception as exc:
            return {"status": "error", "message": f"Нет связи: {exc}"[:300]}

    def detect_chat_id(self, bot_token: str) -> dict[str, Any]:
        """Берёт chat_id из последнего сообщения, отправленного боту."""
        bot_token = (bot_token or "").strip()
        if not bot_token:
            return {"status": "error", "message": "Нужен токен бота"}
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{bot_token}/getUpdates",
                params={"limit": 10}, timeout=20,
            ).json()
            if not r.get("ok"):
                return {"status": "error",
                        "message": r.get("description") or "Не удалось получить обновления"}
            for upd in reversed(r.get("result", [])):
                msg = upd.get("message") or upd.get("edited_message") or {}
                chat = msg.get("chat") or {}
                if chat.get("id"):
                    who = chat.get("username") or chat.get("first_name") or ""
                    return {"status": "ok", "chat_id": str(chat["id"]), "who": who}
            return {"status": "error",
                    "message": "Не вижу сообщений. Напиши боту в Telegram и нажми ещё раз."}
        except Exception as exc:
            return {"status": "error", "message": f"Нет связи: {exc}"[:300]}

    def save_telegram(self, bot_token: str, chat_id: str) -> dict[str, str]:
        try:
            self._tool.config.save(
                telegram={"bot_token": (bot_token or "").strip(),
                          "chat_id": (chat_id or "").strip(),
                          "enabled": bool(bot_token and chat_id)}
            )
            return {"status": "ok"}
        except Exception as exc:
            logger.exception("save_telegram")
            return {"status": "error", "message": str(exc)}

    def skip_telegram(self) -> dict[str, str]:
        try:
            self._tool.config.save(telegram={"enabled": False})
            return {"status": "ok"}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    # ------------------------------------------------------------------ finish

    def finish_setup(self) -> dict[str, str]:
        try:
            self._tool.config.save(**{SETUP_KEY: {"completed": True,
                                                  "version": SETUP_VERSION}})
        except Exception:
            logger.exception("finish_setup")
        if self._window:
            try:
                self._window.load_url(self._main_url)
            except Exception:
                logger.exception("load main url")
        return {"status": "ok"}

    def open_external(self, url: str) -> dict[str, str]:
        """Открыть ссылку в системном браузере (за ключами AI, за токеном бота)."""
        try:
            import webbrowser

            if str(url).startswith(("http://", "https://")):
                webbrowser.open(url)
                return {"status": "ok"}
            return {"status": "error", "message": "Недопустимая ссылка"}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    # ================================================= состояние приложения

    SEARCH_KEY = "app_search"

    SEARCH_DEFAULTS: dict[str, Any] = {
        "stop_words": "",
        "salary": None,
        "area": None,
        "experience": None,
        "period": None,
        "remote": False,
        "order_by": None,
        "max_responses": None,
        "total_pages": None,
        "per_page": None,
        "only_with_salary": False,
        "skip_tests": True,
    }

    def search_settings(self) -> dict[str, Any]:
        saved = self._tool.config.get(self.SEARCH_KEY) or {}
        out = dict(self.SEARCH_DEFAULTS)
        out.update({k: v for k, v in saved.items() if k in self.SEARCH_DEFAULTS})
        if not out.get("max_responses"):
            out["max_responses"] = engine.DAILY_DEFAULTS["max_responses"]
        return out

    def save_search(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            clean = {k: v for k, v in (params or {}).items()
                     if k in self.SEARCH_DEFAULTS}
            self._tool.config.save(**{self.SEARCH_KEY: clean})
            return {"status": "ok", "message": "Настройки сохранены."}
        except Exception as exc:
            logger.exception("save_search")
            return {"status": "error", "message": str(exc)}

    def app_state(self) -> dict[str, Any]:
        """Всё, что нужно интерфейсу за один вызов."""
        from app_main import APP_VERSION

        state: dict[str, Any] = {"version": APP_VERSION}
        try:
            state["status"] = self.get_status()
        except Exception as exc:
            state["status"] = {"authorized": False, "reason": "error",
                               "error": str(exc)}
        try:
            state["resumes"] = self.get_resumes()
        except Exception:
            state["resumes"] = []
        for key, fn in (
            ("profiles", self.profiles_state),
            ("letter", self.letter_ready),
            ("engine", self.engine_state),
            ("search", self.search_settings),
            ("reports", self.list_reports),
        ):
            try:
                state[key] = fn()
            except Exception:
                logger.exception("app_state.%s", key)
                state[key] = {} if key != "reports" else []
        tg = self._tool.config.get("telegram") or {}
        state["telegram"] = {"bot_token": tg.get("bot_token") or "",
                             "chat_id": tg.get("chat_id") or "",
                             "enabled": bool(tg.get("enabled"))}
        state["data_dir"] = str(self.profile_dir())
        return state

    # ------------------------------------------------------------- запуск

    def start_apply(self, dry: bool = False) -> dict[str, Any]:
        """Рассылка по сохранённым настройкам поиска."""
        s = self.search_settings()
        params: dict[str, Any] = {
            "excluded_filter": s.get("stop_words") or None,
            "salary": s.get("salary") or None,
            "experience": s.get("experience") or None,
            "period": s.get("period") or None,
            "order_by": s.get("order_by") or None,
            "max_responses": s.get("max_responses") or None,
            "total_pages": s.get("total_pages") or None,
            "per_page": s.get("per_page") or None,
            "only_with_salary": bool(s.get("only_with_salary")),
            "skip_tests": bool(s.get("skip_tests", True)),
            "dry_run": bool(dry),
        }
        if s.get("area"):
            params["area"] = [str(s["area"])]
        if s.get("remote"):
            params["work_format"] = ["REMOTE"]
        params = {k: v for k, v in params.items() if v is not None}
        return self.apply_vacancies(params)

    # -------------------------------------------------------- справочники

    def areas_list(self) -> list[dict[str, Any]]:
        cached = getattr(self, "_areas_cache", None)
        if cached:
            return cached
        try:
            data = self.get_areas()
        except Exception:
            logger.exception("areas_list")
            data = []
        self._areas_cache = data
        return data

    def stopwords_preview(self, raw: str) -> dict[str, Any]:
        """Показываем человеку, что именно поймает его список стоп-слов."""
        try:
            words = stopwords.parse_words(raw or "")
            if not words:
                return {"words": [], "examples": []}
            pattern = stopwords.to_pattern(raw or "")
            examples: list[str] = []
            probes = [
                "Менеджер по продажам", "PR-менеджер", "Стажёр отдела маркетинга",
                "Шеф-редактор", "Бухгалтер на первичку", "Контент-редактор",
            ]
            rx = re.compile(pattern, re.IGNORECASE) if pattern else None
            for probe in probes:
                if rx and rx.search(probe):
                    examples.append(probe)
            return {"words": words, "pattern": pattern, "examples": examples[:3]}
        except Exception as exc:
            return {"words": [], "examples": [], "error": str(exc)}

    # ------------------------------------------------------------ отклики

    def sync_negotiations(self) -> dict[str, Any]:
        """Тянем отклики с hh.ru ВМЕСТЕ с вакансиями и работодателями.

        Штатный refresh_negotiations сохраняет только сам отклик, а название
        вакансии и имя компании лежат во вложенных объектах и теряются —
        поэтому список откликов выглядел пустым.
        """
        storage = self._tool.storage
        saved = 0
        problems: list[str] = []
        for status in ("active", "archived"):
            try:
                for item in self._tool.get_negotiations(status):
                    vacancy = item.get("vacancy") or {}
                    employer = vacancy.get("employer") or {}
                    if employer.get("id") and employer.get("name"):
                        try:
                            storage.employers.save(employer)
                        except Exception:
                            logger.debug("employer save", exc_info=True)
                    if vacancy.get("id") and vacancy.get("name"):
                        try:
                            storage.vacancies.save(vacancy)
                        except Exception:
                            logger.debug("vacancy save", exc_info=True)
                    storage.negotiations.save(item)
                    saved += 1
            except Exception as exc:
                problems.append(f"{status}: {exc}")
        if not saved and problems:
            return {"status": "error", "message": "; ".join(problems)[:400]}
        message = f"Загружено откликов: {saved}."
        if problems:
            message += " Часть не забралась: " + "; ".join(problems)[:200]
        return {"status": "ok", "message": message}

    def negotiations(self, limit: int = 500) -> list[dict[str, Any]]:
        return self.get_negotiations_from_db()[: int(limit or 500)]

    def stats(self) -> dict[str, Any]:
        """Своя статистика: без date(), потому что HH отдаёт время как
        2026-09-09T12:00:00+0300, а sqlite на таком формате возвращает null."""
        out = {"total": 0, "invitations": 0, "discards": 0, "skipped": 0,
               "by_state": {}, "daily": {}}
        try:
            conn = self._tool.storage.negotiations.conn
            for state, count in conn.execute(
                "SELECT state, count(*) FROM negotiations GROUP BY state"
            ):
                state = state or "unknown"
                out["by_state"][state] = count
                out["total"] += count
                if state.startswith("invitation"):
                    out["invitations"] += count
                elif state.startswith("discard"):
                    out["discards"] += count
            for day, count in conn.execute(
                "SELECT substr(created_at, 1, 10) AS d, count(*)"
                " FROM negotiations WHERE d >= date('now', '-30 days')"
                " GROUP BY d ORDER BY d"
            ):
                if day:
                    out["daily"][day] = count
            row = conn.execute(
                "SELECT count(*) FROM skipped_vacancies"
            ).fetchone()
            out["skipped"] = row[0] if row else 0
        except Exception:
            logger.exception("stats")
        return out

    def _open_folder(self, path: str | Path) -> None:
        import sys
        path = str(path)
        if sys.platform == "win32":
            subprocess.Popen(["explorer", path])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    def open_reports_folder(self) -> dict[str, str]:
        try:
            path = self.monitor.reports_dir()
            self._open_folder(path)
            return {"status": "ok"}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    def open_data_folder(self) -> dict[str, str]:
        try:
            self._open_folder(self.profile_dir())
            return {"status": "ok"}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    # =================================================== профили и действия

    def _capture_run(self, kind: str, options: dict[str, Any] | None = None
                     ) -> tuple[str, int]:
        """Запуск операции движка. Всё знание о флагах живёт в engine.py."""
        return engine.run(self._tool, kind, options or {},
                          on_line=self._action_line)

    def _action_line(self, line: str) -> None:
        """Вывод быстрых действий тоже уходит в ленту, если она открыта."""
        try:
            if self.monitor.is_open():
                self.monitor.feed(line)
        except Exception:
            pass

    def bump_resumes(self) -> dict[str, str]:
        """Поднять резюме в поиске (раз в 4 часа)."""
        text, code = self._capture_run("bump")
        if "Не могу обновить" in text:
            return {"status": "ok",
                    "message": "Ещё не прошло 4 часа с прошлого поднятия."}
        if code or "Ошибка" in text:
            return {"status": "error", "message": text[-400:] or "Не получилось"}
        return {"status": "ok", "message": "Резюме подняты в поиске."}

    def clear_rejections(self) -> dict[str, str]:
        """Убрать отказы из активных откликов."""
        text, code = self._capture_run("clear", {"delete_chat": True})
        n = text.count("Отменили отклик")
        if code and not n:
            return {"status": "error", "message": text[-400:] or "Не получилось"}
        return {"status": "ok",
                "message": f"Убрано отказов: {n}." if n else "Отказов не было."}

    def send_pings(self, message: str = "") -> dict[str, str]:
        """Разослать напоминание по активным чатам.

        Текст берём из шаблона профиля. Разворачивает его сам движок, но
        через нашу обёртку (hh_patch): в один чат дважды один текст не уйдёт.
        """
        text = (message or "").strip() or self._read_template("ping")
        if not text.strip():
            text = letters.DEFAULT_PING
        info = letters.validate(text, "ping")
        if not info["ok"]:
            return {"status": "error",
                    "message": "Шаблон пинга с ошибкой: " + "; ".join(info["errors"])}

        out, code = self._capture_run("ping", {"message": text})
        n = out.count("Отправлено для")
        if code and not n:
            return {"status": "error", "message": out[-400:] or "Не получилось"}
        return {"status": "ok",
                "message": f"Отправлено сообщений: {n}." if n
                           else "Активных чатов не нашлось."}

    def engine_state(self) -> dict[str, Any]:
        """Совместимость движка — показываем в настройках."""
        try:
            return engine.check()
        except Exception as exc:
            return {"ok": False, "version": "?", "problems": [str(exc)]}

    # ------------------------------------------------------------- профили

    def profiles_state(self) -> dict[str, Any]:
        import profiles

        if not self._data_root:
            return {"active": "", "profiles": []}
        return profiles.listing(self._data_root)

    def switch_profile(self, profile_id: str) -> dict[str, str]:
        import profiles

        if not profiles.set_active(self._data_root, profile_id):
            return {"status": "error", "message": "Такого профиля нет"}
        self._restart_later()
        return {"status": "ok"}

    def add_profile(self, name: str) -> dict[str, Any]:
        import profiles

        try:
            pid = profiles.add(self._data_root, name)
            profiles.set_active(self._data_root, pid)
            self._restart_later()
            return {"status": "ok", "id": pid}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def import_profile(self, name: str, source: str) -> dict[str, Any]:
        import profiles

        res = profiles.import_from(self._data_root, name, source)
        if res.get("status") == "ok":
            profiles.set_active(self._data_root, res["id"])
            self._restart_later()
        return res

    def _restart_later(self, delay: float = 1.2) -> None:
        if not self._restart:
            return
        import threading as _threading

        _threading.Timer(delay, self._restart).start()
