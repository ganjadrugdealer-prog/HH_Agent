# -*- coding: utf-8 -*-
"""Живая лента прогона и итоговый отчёт.

Утилита сообщает о ходе рассылки обычными строками — часть через print,
часть через logger. И то и другое сходится в одной точке: Api._send_progress.
Мы её перехватываем, разбираем строку в структурное событие, дотягиваем из
базы название вакансии и зарплату и отдаём в отдельное окно «Прогон».
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("hh_agent")

URL_RE = re.compile(r"https?://\S+")
VACANCY_ID_RE = re.compile(r"/vacancy/(\d+)")

# Тип события -> как показывать. Порядок важен: правила проверяются сверху вниз.
RULES: list[tuple[str, str]] = [
    ("applied_test", "📨 Отправили отклик на вакансию с тестом"),
    ("applied_captcha", "📨 Отправили отклик на вакансию после капчи"),
    ("applied", "📨 Отправили отклик"),
    ("seen_before", "⏩ Вакансия уже отклонена ранее"),
    ("limit", "⛔ Лимит откликов"),
    ("rejected", "⛔ Пришел отказ от"),
    ("email", "📧 Отправлено письмо"),
    ("resume_start", "🚀 Начинаю рассылку откликов для резюме:"),
    ("resume_done", "✅️ Закончили рассылку для резюме:"),
    ("all_done", "📝 Отклики на вакансии разосланы"),
    ("filtered", "Вакансия попала под фильтр:"),
    ("blacklisted", "Вакансия добавлена в черный список:"),
    ("captcha", "Требуется капча"),
    ("redirect", "Игнорирую перенаправление"),
]

# Какие события считаем в сводке и как называем по-русски
COUNTERS = {
    "applied": "Откликов отправлено",
    "filtered": "Отсеяно стоп-словами",
    "seen_before": "Пропущено, уже отклонялись",
    "rejected": "Отказ пришёл сразу",
    "email": "Писем на email",
    "error": "Ошибок",
}


def classify(message: str) -> str:
    for kind, needle in RULES:
        if needle in message:
            return kind
    low = message.lower()
    if "ошибка" in low or "не удалось" in low or "error" in low:
        return "error"
    if "достигли лимита" in low:
        return "limit"
    return "info"


class RunMonitor:
    """Состояние одного прогона + окно, в котором он показывается."""

    def __init__(self, tool, wizard_dir: Path):
        self._tool = tool
        self._dir = wizard_dir
        self._window = None
        self._lock = threading.Lock()
        self.reset()

    # ------------------------------------------------------------------ state

    def reset(self) -> None:
        with self._lock:
            self.events: list[dict[str, Any]] = []
            self.counts: dict[str, int] = {}
            self.resumes: list[dict[str, Any]] = []
            self.started_at: float = time.time()
            self.finished_at: float | None = None
            self.status: str = "running"
            self.params: dict[str, Any] = {}
            self.limit_reached: bool = False

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            end = self.finished_at or time.time()
            return {
                "status": self.status,
                "started_at": datetime.fromtimestamp(self.started_at).strftime("%H:%M:%S"),
                "elapsed": int(end - self.started_at),
                "counts": dict(self.counts),
                "counter_labels": COUNTERS,
                "resumes": list(self.resumes),
                "events": list(self.events),
                "limit_reached": self.limit_reached,
                "params": self.params,
            }

    # ------------------------------------------------------------- база данных

    def _vacancy_info(self, url: str | None) -> dict[str, Any]:
        if not url:
            return {}
        m = VACANCY_ID_RE.search(url)
        if not m:
            return {}
        try:
            row = self._tool.db.execute(
                "SELECT name, salary_from, salary_to, currency, area_name, remote"
                " FROM vacancies WHERE id = ?",
                (int(m.group(1)),),
            ).fetchone()
        except Exception:
            return {}
        if not row:
            return {}
        name, sfrom, sto, cur, area, remote = row
        return {
            "title": name,
            "salary": self._salary(sfrom, sto, cur),
            "area": area,
            "remote": bool(remote),
        }

    @staticmethod
    def _salary(sfrom: int | None, sto: int | None, currency: str | None) -> str:
        cur = {"RUR": "₽", "USD": "$", "EUR": "€"}.get(currency or "RUR", currency or "")
        def fmt(v):
            return f"{int(v):,}".replace(",", " ")
        if sfrom and sto and sfrom != sto:
            return f"{fmt(sfrom)}–{fmt(sto)} {cur}"
        one = sfrom or sto
        return f"от {fmt(one)} {cur}" if one else ""

    # ---------------------------------------------------------------- события

    def feed(self, message: str) -> None:
        message = (message or "").strip()
        if not message:
            return
        kind = classify(message)
        url_m = URL_RE.search(message)
        url = url_m.group(0).rstrip(".,);") if url_m else None

        event: dict[str, Any] = {
            "t": datetime.now().strftime("%H:%M:%S"),
            "kind": kind,
            "text": message,
            "url": url,
        }

        if kind in ("applied", "applied_test", "applied_captcha",
                    "filtered", "blacklisted", "seen_before", "rejected"):
            event.update(self._vacancy_info(url))

        if kind == "captcha":
            self._send_telegram("⚠️ Внимание! HH запрашивает КАПЧУ. Откройте приложение, чтобы пройти её.")

        if kind == "resume_start":
            title = message.split(":", 1)[-1].strip()
            with self._lock:
                self.resumes.append({"title": title, "applied": 0})
        elif kind == "resume_done":
            m = re.search(r"Отправлено:\s*(\d+)", message)
            title = message.split("резюме:", 1)[-1].split(".")[0].strip()
            with self._lock:
                for r in self.resumes:
                    if r["title"] == title:
                        r["applied"] = int(m.group(1)) if m else r["applied"]
                        break
        elif kind == "limit":
            with self._lock:
                self.limit_reached = True

        # счётчики: три вида отклика считаем как один
        counter_key = "applied" if kind.startswith("applied") else kind
        if counter_key in COUNTERS:
            with self._lock:
                self.counts[counter_key] = self.counts.get(counter_key, 0) + 1

        with self._lock:
            self.events.append(event)
            # лента не должна съесть память на многочасовом прогоне
            if len(self.events) > 4000:
                del self.events[:1000]

        self._push("onRunEvent", event, self._counts_copy())

    def _counts_copy(self) -> dict[str, int]:
        with self._lock:
            return dict(self.counts)

    def _push(self, fn: str, *args: Any) -> None:
        win = self._window
        if not win:
            return
        try:
            payload = ", ".join(json.dumps(a, ensure_ascii=False) for a in args)
            win.evaluate_js(f"if(window.{fn})window.{fn}({payload})")
        except Exception:
            pass

    # -------------------------------------------------------------------- окно

    def open_window(self, api, params: dict[str, Any]) -> None:
        self.reset()
        self._send_telegram("🚀 Рассылка откликов начата")
        with self._lock:
            self.params = dict(params or {})
        try:
            import webview

            if self._window is not None:
                try:
                    self._window.load_url((self._dir / "run.html").as_uri())
                    return
                except Exception:
                    self._window = None

            self._window = webview.create_window(
                title="Прогон — HH Agent",
                url=(self._dir / "run.html").as_uri(),
                js_api=api,
                width=920,
                height=720,
                min_size=(700, 480),
                text_select=True,
            )
            self._window.events.closed += self._on_closed
        except Exception:
            logger.exception("Не удалось открыть окно прогона")
            self._window = None

    def is_open(self) -> bool:
        return self._window is not None

    def _on_closed(self) -> None:
        self._window = None

    # ------------------------------------------------------------------ финал

    def _send_telegram(self, text: str) -> None:
        try:
            tg = self._tool.config.get("telegram") or {}
            if not tg.get("enabled") or not tg.get("bot_token") or not tg.get("chat_id"):
                return
            import requests
            requests.post(
                f"https://api.telegram.org/bot{tg['bot_token']}/sendMessage",
                json={"chat_id": tg["chat_id"], "text": text},
                timeout=10,
            )
        except Exception:
            logger.exception("telegram notification error")

    def finish(self, status: str) -> dict[str, Any]:
        with self._lock:
            self.status = status
            self.finished_at = time.time()
        report = self.snapshot()
        self._push("onRunFinished", report)

        counts = report.get("counts", {})
        lines = [f"🏁 Прогон завершен ({status})"]
        if counts.get("applied"): lines.append(f"✅ Отправлено: {counts['applied']}")
        if counts.get("filtered"): lines.append(f"🛑 Отсеяно: {counts['filtered']}")
        if counts.get("rejected"): lines.append(f"⛔ Отказов сразу: {counts['rejected']}")
        if counts.get("error"): lines.append(f"❌ Ошибок: {counts['error']}")
        self._send_telegram("\n".join(lines))

        try:
            self.save_report(report)
        except Exception:
            logger.exception("Не удалось сохранить отчёт")
        return report

    def reports_dir(self) -> Path:
        d = Path(self._tool.config_path) / "reports"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save_report(self, report: dict[str, Any]) -> Path:
        name = datetime.fromtimestamp(self.started_at).strftime("%Y-%m-%d_%H-%M-%S")
        path = self.reports_dir() / f"{name}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        return path

    def list_reports(self, limit: int = 30) -> list[dict[str, Any]]:
        out = []
        for p in sorted(self.reports_dir().glob("*.json"), reverse=True)[:limit]:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            out.append({
                "file": p.name,
                "date": p.stem.replace("_", " "),
                "status": data.get("status"),
                "counts": data.get("counts", {}),
                "elapsed": data.get("elapsed", 0),
            })
        return out

    def load_report(self, file: str) -> dict[str, Any] | None:
        p = self.reports_dir() / Path(file).name
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
