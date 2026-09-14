# -*- coding: utf-8 -*-
"""Установка и подключение консольного ядра hh-applicant-tool.

Ядро не входит в сборку: приложение скачивает его с PyPI при первом запуске
и кладёт в пользовательскую папку. Версия берётся из remote_config.json в
репозитории — так можно откатить всех пользователей на рабочую версию ядра,
не пересобирая exe.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path

REMOTE_CONFIG_URL = (
    "https://raw.githubusercontent.com/ganjadrugdealer-prog/HH_Agent"
    "/main/remote_config.json"
)
PYPI_URL = "https://pypi.org/pypi/hh-applicant-tool/json"
PACKAGE_DIR = "hh_applicant_tool"
REINSTALL_MARKER = "reinstall.flag"


def app_data_path() -> Path:
    if sys.platform == "win32":
        base_dir = Path(os.environ.get("APPDATA", os.path.expanduser("~")))
    elif sys.platform == "darwin":
        base_dir = Path(os.path.expanduser("~/Library/Application Support"))
    else:
        base_dir = Path(os.path.expanduser("~/.config"))
    return base_dir / "HH_Agent"


def get_core_path() -> Path:
    return app_data_path() / "core"


def marker_path() -> Path:
    return app_data_path() / REINSTALL_MARKER


def request_reinstall() -> None:
    """Пометить ядро к переустановке при следующем запуске.

    Удалять папку ядра на ходу не стоит: её модули уже загружены, а
    неполное удаление оставит битое ядро, которое is_core_installed()
    сочтёт исправным. Метку видит следующий запуск до импорта ядра.
    """
    p = marker_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("reinstall", encoding="utf-8")


def clear_reinstall() -> None:
    try:
        marker_path().unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def get_installed_version() -> str | None:
    version_file = get_core_path() / "version.txt"
    if version_file.exists():
        try:
            return version_file.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None
    return None


def is_core_installed() -> bool:
    if marker_path().exists():
        return False
    core_path = get_core_path()
    return (core_path / PACKAGE_DIR / "__init__.py").exists()


def get_approved_version() -> str | None:
    """Одобренная версия ядра из репозитория. None — если недоступна."""
    try:
        req = urllib.request.Request(
            REMOTE_CONFIG_URL, headers={"User-Agent": "HH-Agent"}
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        version = data.get("approved_core_version")
        return str(version) if version else None
    except Exception:
        return None


def _fetch_pypi() -> dict:
    req = urllib.request.Request(PYPI_URL, headers={"User-Agent": "HH-Agent"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _pick_artifact(files: list[dict]) -> tuple[str, bool]:
    for f in files:
        if f.get("filename", "").endswith(".whl"):
            return f["url"], True
    for f in files:
        if f.get("filename", "").endswith(".tar.gz"):
            return f["url"], False
    raise RuntimeError("На PyPI нет подходящего архива с ядром.")


def _safe_members(tar: tarfile.TarFile, dest: Path):
    """Члены архива без первого каталога и без выхода за пределы dest."""
    dest = dest.resolve()
    for member in tar.getmembers():
        name = member.name.replace("\\", "/")
        parts = [p for p in Path(name).parts if p not in ("", ".")]
        if any(p == ".." for p in parts) or name.startswith("/"):
            continue
        if len(parts) > 1:
            parts = parts[1:]          # срезаем pkg-1.2.3/
        elif member.isdir():
            continue                    # сам корневой каталог не нужен
        member.name = str(Path(*parts))
        target = (dest / member.name).resolve()
        if not str(target).startswith(str(dest)):
            continue
        yield member


def _extract_sdist(tar: tarfile.TarFile, dest: Path) -> None:
    """Распаковка sdist без первого каталога.

    Логика та же, что была, плюс три вещи, которых не хватало: явная
    проверка, что распакованный путь не вылезает за dest; пропуск
    симлинков и спецфайлов; и распаковка через extractfile вместо
    tar.extract — в Python 3.14 у extract меняется поведение по умолчанию.
    """
    for member in _safe_members(tar, dest):
        if member.isdir():
            (dest / member.name).mkdir(parents=True, exist_ok=True)
            continue
        if not (member.isfile() or member.isreg()):
            continue                    # симлинки и устройства не распаковываем
        target = dest / member.name
        target.parent.mkdir(parents=True, exist_ok=True)
        src = tar.extractfile(member)
        if src is None:
            continue
        with src, open(target, "wb") as fp:
            shutil.copyfileobj(src, fp)


def _extract_wheel(archive: Path, dest: Path) -> None:
    with zipfile.ZipFile(archive, "r") as z:
        dest_res = dest.resolve()
        for info in z.infolist():
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or ".." in Path(name).parts:
                continue
            target = (dest / name).resolve()
            if not str(target).startswith(str(dest_res)):
                continue
            z.extract(info, dest)


def _rmtree_retry(path: Path, attempts: int = 5) -> None:
    for i in range(attempts):
        if not path.exists():
            return
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        time.sleep(0.4 * (i + 1))
    if path.exists():
        raise RuntimeError(
            f"Не удалось удалить старое ядро: {path}. Закройте приложение и повторите."
        )


def install_core(progress_callback=None, target_version: str | None = None) -> str:
    """Скачивает и распаковывает ядро. Возвращает установленную версию."""

    def say(msg: str) -> None:
        if progress_callback:
            try:
                progress_callback(msg)
            except Exception:
                pass

    core_path = get_core_path()
    staging = core_path.with_name("core.new")

    if target_version is None:
        say("Проверка удалённого манифеста (GitHub)...")
        target_version = get_approved_version()

    say("Получение информации из PyPI...")
    data = _fetch_pypi()

    if not target_version:
        target_version = data["info"]["version"]
    if target_version not in data.get("releases", {}):
        raise RuntimeError(f"Версия {target_version} не найдена на PyPI.")

    download_url, is_wheel = _pick_artifact(data["releases"][target_version])

    say(f"Скачивание ядра v{target_version}...")
    _rmtree_retry(staging)
    staging.mkdir(parents=True, exist_ok=True)
    archive_path = staging / "downloaded_archive"
    try:
        req = urllib.request.Request(
            download_url, headers={"User-Agent": "HH-Agent"}
        )
        with urllib.request.urlopen(req, timeout=60) as resp, \
                open(archive_path, "wb") as fp:
            shutil.copyfileobj(resp, fp)

        say("Распаковка...")
        if is_wheel:
            _extract_wheel(archive_path, staging)
        else:
            with tarfile.open(archive_path, "r:gz") as t:
                _extract_sdist(t, staging)
    finally:
        if archive_path.exists():
            try:
                archive_path.unlink()
            except OSError:
                pass

    if not (staging / PACKAGE_DIR / "__init__.py").exists():
        _rmtree_retry(staging)
        raise RuntimeError(
            "В архиве не оказалось пакета hh_applicant_tool — установка отменена."
        )

    (staging / "version.txt").write_text(target_version, encoding="utf-8")

    say("Подключение ядра...")
    _rmtree_retry(core_path)
    staging.replace(core_path)
    clear_reinstall()

    say("Ядро успешно установлено!")
    return target_version


def init_core() -> None:
    """Добавляет папку ядра в sys.path, чтобы hh_applicant_tool импортировался."""
    core_path = get_core_path()
    path_str = str(core_path.resolve())
    if path_str not in sys.path:
        sys.path.insert(0, path_str)
