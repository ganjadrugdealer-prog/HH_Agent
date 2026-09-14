# -*- coding: utf-8 -*-
"""Профили: несколько аккаунтов hh.ru в одной установке приложения.

Раскладка на диске:

    <папка exe>/data/
        profiles.json          какие профили есть и какой активен
        profiles/<id>/         config.json, cookies.txt, data (SQLite), reports

Переключение профиля = смена активного в profiles.json + перезапуск приложения.
Так надёжнее, чем пересобирать наполовину инициализированный инструмент на лету.
"""
from __future__ import annotations

import json
import re
import shutil
import unicodedata
from pathlib import Path
from typing import Any

STATE_FILE = "profiles.json"
PROFILES_DIR = "profiles"
DEFAULT_ID = "default"

# Файлы, которые имеет смысл тянуть из папки профиля Telegram-бота
IMPORT_FILES = ("config.json", "cookies.txt", "data", "log.txt")


def slugify(name: str) -> str:
    # NFKC, а не NFKD: разложение отрывает от «ё» и «й» диакритику,
    # и она превращалась в лишний дефис («Ёлка» -> «e-lka»).
    text = unicodedata.normalize("NFKC", name or "").strip().lower()
    translit = {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
        "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
        "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
        "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    }
    text = "".join(translit.get(ch, ch) for ch in text)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "profile"


def state_path(data_root: Path) -> Path:
    return Path(data_root) / STATE_FILE


def profiles_root(data_root: Path) -> Path:
    return Path(data_root) / PROFILES_DIR


def profile_dir(data_root: Path, profile_id: str) -> Path:
    return profiles_root(data_root) / profile_id


def _read(data_root: Path) -> dict[str, Any]:
    p = state_path(data_root)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write(data_root: Path, state: dict[str, Any]) -> None:
    state_path(data_root).parent.mkdir(parents=True, exist_ok=True)
    state_path(data_root).write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def ensure(data_root: Path) -> dict[str, Any]:
    """Возвращает состояние, создавая профиль по умолчанию и мигрируя старую раскладку."""
    data_root = Path(data_root)
    data_root.mkdir(parents=True, exist_ok=True)
    state = _read(data_root)

    if not state.get("profiles"):
        state = {"active": DEFAULT_ID,
                 "profiles": [{"id": DEFAULT_ID, "name": "Мой аккаунт"}]}
        target = profile_dir(data_root, DEFAULT_ID)
        target.mkdir(parents=True, exist_ok=True)

        # Миграция: раньше конфиг лежал прямо в data/
        for name in IMPORT_FILES:
            old = data_root / name
            if old.exists() and not (target / name).exists():
                try:
                    shutil.move(str(old), str(target / name))
                except Exception:
                    pass
        _write(data_root, state)

    ids = {p["id"] for p in state["profiles"]}
    if state.get("active") not in ids:
        state["active"] = state["profiles"][0]["id"]
        _write(data_root, state)

    profile_dir(data_root, state["active"]).mkdir(parents=True, exist_ok=True)
    return state


def listing(data_root: Path) -> dict[str, Any]:
    state = ensure(data_root)
    out = []
    for p in state["profiles"]:
        d = profile_dir(data_root, p["id"])
        cfg = d / "config.json"
        authorized = False
        if cfg.exists():
            try:
                authorized = bool(
                    (json.loads(cfg.read_text(encoding="utf-8")).get("token") or {})
                    .get("refresh_token")
                )
            except Exception:
                authorized = False
        out.append({**p, "authorized": authorized, "path": str(d)})
    return {"active": state["active"], "profiles": out}


def add(data_root: Path, name: str) -> str:
    state = ensure(data_root)
    base = slugify(name)
    ids = {p["id"] for p in state["profiles"]}
    pid, n = base, 2
    while pid in ids:
        pid, n = f"{base}-{n}", n + 1
    state["profiles"].append({"id": pid, "name": (name or "").strip() or pid})
    _write(data_root, state)
    profile_dir(data_root, pid).mkdir(parents=True, exist_ok=True)
    return pid


def set_active(data_root: Path, profile_id: str) -> bool:
    state = ensure(data_root)
    if profile_id not in {p["id"] for p in state["profiles"]}:
        return False
    state["active"] = profile_id
    _write(data_root, state)
    return True


def rename(data_root: Path, profile_id: str, name: str) -> bool:
    state = ensure(data_root)
    for p in state["profiles"]:
        if p["id"] == profile_id:
            p["name"] = (name or "").strip() or p["name"]
            _write(data_root, state)
            return True
    return False


def import_from(data_root: Path, name: str, source: str | Path) -> dict[str, Any]:
    """Создаёт профиль и копирует в него данные из папки hh_data Telegram-бота."""
    src = Path(source)
    if not src.exists():
        return {"status": "error", "message": f"Папка не найдена: {src}"}
    if not (src / "config.json").exists():
        # возможно, указали папку профиля, а не hh_data
        if (src / "hh_data" / "config.json").exists():
            src = src / "hh_data"
        else:
            return {"status": "error",
                    "message": "В папке нет config.json — это не профиль hh-applicant-tool"}

    pid = add(data_root, name)
    target = profile_dir(data_root, pid)
    copied = []
    for fname in IMPORT_FILES:
        s = src / fname
        if s.exists() and s.is_file():
            shutil.copy2(s, target / fname)
            copied.append(fname)

    # заодно тянем письмо и стоп-слова, если лежат рядом с hh_data
    parent = src.parent
    for fname in ("letter.txt", "excluded.txt"):
        s = parent / fname
        if s.exists():
            shutil.copy2(s, target / fname)
            copied.append(fname)

    return {"status": "ok", "id": pid, "copied": copied, "path": str(target)}


# Данные, которые можно долить в уже авторизованный профиль,
# не трогая его вход: база, письмо, стоп-слова.
DATA_ONLY_FILES = ("data", "log.txt")
SIDE_FILES = ("letter.txt", "excluded.txt")


def merge_into(data_root: Path, profile_id: str, source: str | Path) -> dict[str, Any]:
    """Доливает в существующий профиль базу и настройки из папки бота.

    config.json и cookies.txt НЕ трогаем — иначе затрём свежий вход.
    """
    src = Path(source)
    if (src / "hh_data").exists():
        parent, src = src, src / "hh_data"
    else:
        parent = src.parent

    target = profile_dir(data_root, profile_id)
    if not target.exists():
        return {"status": "error", "message": f"Профиль {profile_id} не найден"}

    copied, skipped = [], []
    for fname in DATA_ONLY_FILES:
        s = src / fname
        if s.exists() and s.is_file():
            shutil.copy2(s, target / fname)
            copied.append(fname)
    for fname in SIDE_FILES:
        s = parent / fname
        if s.exists():
            shutil.copy2(s, target / fname)
            copied.append(fname)

    for fname in ("config.json", "cookies.txt"):
        if (src / fname).exists():
            skipped.append(fname)

    return {"status": "ok", "copied": copied, "skipped": skipped,
            "path": str(target)}
